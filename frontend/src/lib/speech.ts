/** Thin wrappers over the browser's Web Speech API. Everything degrades to typing. */
import { useCallback, useEffect, useRef, useState } from "react";

interface Recognition {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((e: any) => void) | null;
  onerror: ((e: any) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  abort: () => void;
}

function getCtor(): (new () => Recognition) | null {
  const w = window as any;
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

export const speechInputSupported = () => typeof window !== "undefined" && getCtor() !== null;
export const speechOutputSupported = () => typeof window !== "undefined" && "speechSynthesis" in window;

const ERRORS: Record<string, string> = {
  "not-allowed": "Microphone access is blocked. Allow it in your browser, or type instead.",
  "service-not-allowed": "Microphone access is blocked. Allow it in your browser, or type instead.",
  "no-speech": "I didn't hear anything. Try again, or type instead.",
  "audio-capture": "No microphone was found. You can type instead.",
  network: "Speech recognition needs an internet connection. You can type instead.",
};

export type Lang = "en" | "ur";
export const SPEECH_LOCALE: Record<Lang, string> = { en: "en-US", ur: "ur-PK" };

export function useRecognizer(onFinal: (text: string) => void, lang: Lang = "en") {
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const [error, setError] = useState<string | null>(null);
  const recRef = useRef<Recognition | null>(null);
  const finalRef = useRef(onFinal);
  finalRef.current = onFinal;

  const stop = useCallback(() => {
    recRef.current?.abort();
    recRef.current = null;
    setListening(false);
    setInterim("");
  }, []);

  const start = useCallback(() => {
    const Ctor = getCtor();
    if (!Ctor) {
      setError("This browser can't do voice input. Chrome and Edge can. You can type instead.");
      return;
    }
    recRef.current?.abort();
    const rec = new Ctor();
    rec.lang = SPEECH_LOCALE[lang];
    rec.interimResults = true;
    rec.continuous = false;
    let finalText = "";
    rec.onresult = (e: any) => {
      let interimText = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) finalText += r[0].transcript;
        else interimText += r[0].transcript;
      }
      setInterim(finalText || interimText);
    };
    rec.onerror = (e: any) => {
      if (e.error !== "aborted") setError(ERRORS[e.error] ?? "Voice input stopped. You can type instead.");
    };
    rec.onend = () => {
      setListening(false);
      setInterim("");
      recRef.current = null;
      if (finalText.trim()) finalRef.current(finalText.trim());
    };
    setError(null);
    recRef.current = rec;
    setListening(true);
    try {
      rec.start();
    } catch {
      setListening(false);
    }
  }, [lang]);

  useEffect(() => () => recRef.current?.abort(), []);
  return { listening, interim, error, start, stop, clearError: () => setError(null) };
}

/** Speak text; resolves when finished (or immediately if unsupported or cancelled). */
/** True when this device has a voice for the language (Urdu voices depend on the OS and browser). */
export function hasVoice(lang: Lang): boolean {
  if (!speechOutputSupported()) return false;
  return window.speechSynthesis.getVoices().some((v) => v.lang.toLowerCase().startsWith(lang));
}

export function speak(text: string, lang: Lang = "en"): Promise<void> {
  return new Promise((resolve) => {
    if (!speechOutputSupported()) return resolve();
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = SPEECH_LOCALE[lang];
    u.rate = 1.02;
    const voices = window.speechSynthesis.getVoices();
    const pick =
      voices.find((v) => v.lang.toLowerCase().startsWith(lang) && /natural|google|uzma|asad|samantha|aria|jenny/i.test(v.name)) ??
      voices.find((v) => v.lang.toLowerCase().startsWith(lang));
    if (lang === "ur" && !pick) return resolve(); // no Urdu voice here: the text reply is still shown
    if (pick) u.voice = pick;
    u.onend = () => resolve();
    u.onerror = () => resolve();
    window.speechSynthesis.speak(u);
  });
}

export function stopSpeaking() {
  if (speechOutputSupported()) window.speechSynthesis.cancel();
}
