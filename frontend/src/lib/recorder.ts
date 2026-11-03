/** Records a short clip from the microphone so a server-side speech model can transcribe it. */
import { useCallback, useEffect, useRef, useState } from "react";

const MIME_CANDIDATES = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];

export const recorderSupported = () =>
  typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== "undefined";

export function useRecorder(onAudio: (blob: Blob) => void, maxMs = 12000) {
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const cancelled = useRef(false);
  const cb = useRef(onAudio);
  cb.current = onAudio;

  const release = useCallback(() => {
    window.clearTimeout(timer.current);
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    recRef.current = null;
    setRecording(false);
  }, []);

  const stop = useCallback(() => {
    if (recRef.current && recRef.current.state !== "inactive") recRef.current.stop();
  }, []);

  const cancel = useCallback(() => {
    cancelled.current = true;
    stop();
    release();
  }, [release, stop]);

  const start = useCallback(async () => {
    setError(null);
    cancelled.current = false;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const mimeType = MIME_CANDIDATES.find((m) => MediaRecorder.isTypeSupported(m));
      const rec = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      const chunks: Blob[] = [];
      rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
      rec.onstop = () => {
        const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
        release();
        if (!cancelled.current && blob.size > 0) cb.current(blob);
      };
      recRef.current = rec;
      rec.start();
      setRecording(true);
      timer.current = window.setTimeout(stop, maxMs); // never record forever
    } catch (e) {
      release();
      const name = (e as DOMException).name;
      setError(
        name === "NotAllowedError"
          ? "Microphone access is blocked. Allow it in your browser, or type instead."
          : "No microphone could be started. You can type instead.",
      );
    }
  }, [maxMs, release, stop]);

  useEffect(() => cancel, [cancel]);
  return { recording, error, start, stop, cancel };
}
