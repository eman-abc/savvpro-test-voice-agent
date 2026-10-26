import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import SlotPanel from "../components/SlotPanel";
import Stepper from "../components/Stepper";
import TraceLedger from "../components/TraceLedger";
import { ApiError, api } from "../lib/api";
import { buildIcs, outcomeLabel, parseLocal } from "../lib/format";
import { type Lang, hasVoice, speak, speechInputSupported, speechOutputSupported, stopSpeaking, useRecognizer } from "../lib/speech";
import type { AppointmentInfo, Config, Progress, SlotView, Trace, TurnOut } from "../lib/types";

interface Msg {
  who: "bot" | "user";
  text: string;
}

const SUGGESTIONS_UR: Record<string, string[]> = {
  ROUTE: ["اپائنٹمنٹ بک کرنی ہے", "میری اپائنٹمنٹ منسوخ کریں", "آپ کے اوقات کیا ہیں؟", "مجھے فلو کا ٹیکہ چاہیے"],
  CLARIFY: ["دوبارہ طے کریں", "منسوخ کریں"],
  COLLECT: ["کل صبح 10 بجے", "اگلے منگل دوپہر", "جلد سے جلد", "کوئی بھی ڈاکٹر"],
  OFFER: ["پہلا والا", "دوسرا والا", "دن میں کچھ دیر بعد"],
  CONFIRM: ["جی ہاں", "نہیں، وقت بدلیں"],
  SELECT: ["پہلی والی", "دوسری والی"],
  MORE: ["نہیں، بس شکریہ", "ایک اور بک کرنی ہے"],
};
const STEPPER_UR = ["درخواست", "شناخت", "تفصیل", "تصدیق", "مکمل"];

const SUGGESTIONS: Record<string, string[]> = {
  ROUTE: ["Book an appointment", "Cancel my appointment", "What are your hours?", "I need a flu shot"],
  CLARIFY: ["Reschedule it", "Cancel it"],
  COLLECT: ["Tomorrow at 10 AM", "Next Tuesday afternoon", "Earliest available", "Any provider"],
  OFFER: ["The first one", "The second one", "Something later in the day"],
  CONFIRM: ["Yes", "No, change the time"],
  SELECT: ["The first one", "The second one"],
  MORE: ["No, that's all", "I'd like to book another"],
};

const IDLE_MS = Number(new URLSearchParams(window.location.search).get("idle")) || 20000;
const langOf = (t: string): string | undefined => (/[\u0600-\u06FF]/.test(t) ? "ur" : undefined);

/** A dropped connection is retried with the same sequence number, so the server can never act twice. */
async function turnWithRetry(id: string, text: string, seq: number): Promise<TurnOut> {
  for (let attempt = 0; ; attempt++) {
    try {
      return await api.turn(id, text, seq);
    } catch (e) {
      if (!(e instanceof ApiError) || e.status !== 0 || attempt >= 2) throw e;
      await new Promise((r) => setTimeout(r, 600 * (attempt + 1)));
    }
  }
}

export default function CallPage({ config }: { config: Config | null }) {
  const [callId, setCallId] = useState<string | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [traces, setTraces] = useState<Trace[]>([]);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [slots, setSlots] = useState<SlotView[]>([]);
  const [ended, setEnded] = useState(false);
  const [outcome, setOutcome] = useState<string | null>(null);
  const [appt, setAppt] = useState<AppointmentInfo | null>(null);
  const [step, setStep] = useState("ROUTE");
  const [speaking, setSpeaking] = useState(false);
  const [lang, setLang] = useState<Lang>("en");
  const langRef = useRef<Lang>("en");
  langRef.current = lang;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [voiceReplies, setVoiceReplies] = useState(speechOutputSupported());
  const [handsFree, setHandsFree] = useState(false);
  const [tab, setTab] = useState<"call" | "decisions">("call");
  const scroller = useRef<HTMLDivElement>(null);
  const callRef = useRef<string | null>(null);
  const seqRef = useRef(0); // turns the server has recorded for this call, as far as we know
  const handsFreeRef = useRef(handsFree);
  const voiceRef = useRef(voiceReplies);
  handsFreeRef.current = handsFree;
  voiceRef.current = voiceReplies;

  const apply = useCallback(async (out: TurnOut) => {
    setMsgs((m) => [...m, { who: "bot", text: out.reply }]);
    setTraces((t) => [...t, out.trace]);
    seqRef.current += 1;
    setProgress(out.progress);
    setSlots(out.trace.slots);
    setStep(out.trace.state.to);
    setEnded(out.ended);
    setOutcome(out.outcome);
    if (out.appointment) setAppt(out.appointment);
    setLang(out.lang);
    langRef.current = out.lang;
    if (voiceRef.current) {
      setSpeaking(true);
      await speak(out.reply, out.lang);
      setSpeaking(false);
    }
    return out;
  }, []);

  const recognizer = useRecognizer((text) => void send(text), lang);

  const send = useCallback(
    async (raw: string) => {
      const text = raw.trim();
      const id = callRef.current;
      if (!id) return;
      setError(null);
      setInput("");
      if (text) setMsgs((m) => [...m, { who: "user", text }]); // an empty turn is a silence, not something the caller said
      setBusy(true);
      try {
        const out = await turnWithRetry(id, text, seqRef.current);
        setBusy(false);
        await apply(out);
        if (handsFreeRef.current && !out.ended) recognizerStartRef.current();
      } catch (e) {
        setBusy(false);
        const err = e as ApiError;
        setError(err.message);
        if (err.status === 409 || err.status === 410) setEnded(true);
      }
    },
    [apply],
  );

  const recognizerStartRef = useRef(recognizer.start);
  recognizerStartRef.current = recognizer.start;

  const startCall = async () => {
    stopSpeaking();
    setError(null);
    setBusy(true);
    setMsgs([]);
    setTraces([]);
    setAppt(null);
    setOutcome(null);
    setEnded(false);
    setTab("call");
    try {
      const out = await api.startCall(langRef.current);
      callRef.current = out.call_id;
      seqRef.current = 0;
      setCallId(out.call_id);
      setBusy(false);
      await apply(out);
      if (handsFreeRef.current) recognizerStartRef.current();
    } catch (e) {
      setBusy(false);
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [msgs, recognizer.interim, busy]);
  useEffect(() => () => stopSpeaking(), []);

  // After a pause the assistant asks "are you still there?" (an empty turn); a second pause ends the call.
  useEffect(() => {
    if (!callId || ended || busy || speaking || recognizer.listening || input) return;
    const t = setTimeout(() => void send(""), IDLE_MS);
    return () => clearTimeout(t);
  }, [callId, ended, busy, speaking, recognizer.listening, input, msgs.length, send]);

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!busy && !ended && input.trim()) void send(input);
  };

  const toggleMic = () => {
    if (recognizer.listening) return recognizer.stop();
    stopSpeaking();
    recognizer.start();
  };

  const downloadIcs = () => {
    if (!appt || !config) return;
    const ics = buildIcs({
      uid: String(appt.id),
      start: parseLocal(appt.start),
      end: parseLocal(appt.end),
      title: `${appt.service_name} with ${appt.provider_name}`,
      location: `${config.clinic.name}, ${config.clinic.address}`,
      description: `Please arrive early with photo ID and insurance card. Changes need ${config.policy.cancel_notice_hours} hours' notice.`,
    });
    const url = URL.createObjectURL(new Blob([ics], { type: "text/calendar" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: "appointment.ics" });
    a.click();
    URL.revokeObjectURL(url);
  };

  // ---------------------------------------------------------------- start screen ---
  if (!callId) {
    return (
      <section className="start">
        <h1>Call the clinic’s voice assistant</h1>
        <p>
          It books, reschedules and cancels appointments and answers common questions. Every decision it makes is shown beside the
          conversation, and none of them come from an AI model: it’s all rules you can read.
        </p>
        <div className="langpick" role="radiogroup" aria-label="Call language">
          <button role="radio" aria-checked={lang === "en"} className={`btn ${lang === "en" ? "primary" : ""}`} onClick={() => setLang("en")}>English</button>
          <button role="radio" aria-checked={lang === "ur"} className={`btn ${lang === "ur" ? "primary" : ""}`} onClick={() => setLang("ur")} lang="ur" dir="rtl">اردو</button>
        </div>
        {lang === "ur" && speechOutputSupported() && !hasVoice("ur") && (
          <p className="notice info" role="status">This device has no Urdu voice, so replies will be shown as text. You can still speak or type in Urdu.</p>
        )}
        <div className="start-actions">
          <button className="btn primary" onClick={startCall} disabled={busy || !config}>
            {busy ? "Connecting…" : "Start a call"}
          </button>
          <span className="muted">
            {speechInputSupported() ? "Voice works in this browser. You can also type." : "Voice input needs Chrome or Edge. You can type instead."}
          </span>
        </div>
        {error && <p className="notice error" role="alert">{error}</p>}
        <DemoPatients config={config} onUse={() => undefined} />
      </section>
    );
  }

  // ------------------------------------------------------------------ call screen ---
  const chips = ended ? [] : (lang === "ur" ? SUGGESTIONS_UR : SUGGESTIONS)[step] ?? [];
  return (
    <>
      {progress && <Stepper progress={lang === "ur" ? { ...progress, steps: STEPPER_UR } : progress} ended={ended} />}
      <div className="tabs-mobile" role="tablist">
        <button role="tab" aria-selected={tab === "call"} className={`btn ${tab === "call" ? "primary" : ""}`} onClick={() => setTab("call")}>Call</button>
        <button role="tab" aria-selected={tab === "decisions"} className={`btn ${tab === "decisions" ? "primary" : ""}`} onClick={() => setTab("decisions")}>Decisions</button>
      </div>
      <div className="callgrid">
        <section className={`panel ${tab === "decisions" ? "hide-mobile" : ""}`} aria-label="Conversation">
          <div className="panel-head">
            <h2>Conversation</h2>
            <span className="spacer" />
            {ended && <span className="badge">{outcomeLabel[outcome ?? "no_action"] ?? outcome}</span>}
          </div>
          <div className="transcript" ref={scroller} role="log" aria-live="polite" aria-label="Conversation transcript" tabIndex={0}>
            {msgs.map((m, i) => (
              <div key={i} className={`msg ${m.who}`} dir="auto" lang={langOf(m.text)}>
                <span className="who">{m.who === "bot" ? "Assistant" : "You"}</span>
                {m.text}
              </div>
            ))}
            {recognizer.interim && <div className="msg user interim" dir="auto">{recognizer.interim}</div>}
            {busy && <div className="typing">Thinking…</div>}
          </div>
          {appt && (
            <div className={`appt-card ${appt.action === "cancelled" ? "cancelled" : ""}`}>
              <strong>{appt.action === "booked" ? "Booked" : appt.action === "rescheduled" ? "Rescheduled" : "Cancelled"}</strong>
              <span>{appt.description}</span>
              {appt.action !== "cancelled" && <button className="btn small" onClick={downloadIcs}>Add to my calendar</button>}
            </div>
          )}
          {error && <p className="notice error" role="alert" style={{ margin: "0 16px 12px" }}>{error}</p>}
          {recognizer.error && <p className="notice info" role="status" style={{ margin: "0 16px 12px" }}>{recognizer.error}</p>}
          {ended ? (
            <div className="composer">
              <p className="muted">The call has ended.</p>
              <div className="composer-row"><button className="btn primary" onClick={startCall}>Start a new call</button></div>
            </div>
          ) : (
            <div className="composer">
              {chips.length > 0 && (
                <div className="suggest" aria-label="Quick replies">
                  {chips.map((c) => (
                    <button key={c} className="chip-btn" disabled={busy} onClick={() => void send(c)}>{c}</button>
                  ))}
                </div>
              )}
              <form className="composer-row" onSubmit={onSubmit}>
                <button
                  type="button"
                  className={`mic ${recognizer.listening ? "on" : ""}`}
                  onClick={toggleMic}
                  disabled={busy || !speechInputSupported()}
                  aria-pressed={recognizer.listening}
                  aria-label={recognizer.listening ? "Stop listening" : "Speak to the assistant"}
                  title={speechInputSupported() ? "Speak" : "Voice input isn't supported in this browser"}
                >
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                    <rect x="9" y="3" width="6" height="12" rx="3" />
                    <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
                  </svg>
                </button>
                <label className="sr-only" htmlFor="say">Type what you would say</label>
                <input id="say" className="input" dir="auto" value={input} onChange={(e) => setInput(e.target.value)} placeholder={recognizer.listening ? (lang === "ur" ? "سن رہا ہوں…" : "Listening…") : lang === "ur" ? "یا یہاں لکھیں" : "Or type what you would say"} autoComplete="off" maxLength={500} disabled={busy} />
                <button className="btn primary" type="submit" disabled={busy || !input.trim()}>Send</button>
              </form>
              <div className="toggles">
                <label><input type="checkbox" checked={voiceReplies} disabled={!speechOutputSupported()} onChange={(e) => { setVoiceReplies(e.target.checked); if (!e.target.checked) stopSpeaking(); }} /> Speak replies aloud</label>
                <label><input type="checkbox" checked={handsFree} disabled={!speechInputSupported()} onChange={(e) => setHandsFree(e.target.checked)} /> Hands-free (listen after each reply)</label>
              </div>
            </div>
          )}
          <DemoPatients config={config} onUse={(t) => setInput(t)} />
        </section>

        <section className={`panel ${tab === "call" ? "hide-mobile" : ""}`} aria-label="How the assistant decides">
          <div className="panel-head"><h2>How it decides</h2><span className="spacer" /><span className="muted">rules, not an AI model</span></div>
          <SlotPanel slots={slots} />
          <TraceLedger traces={traces} />
        </section>
      </div>
    </>
  );
}

function DemoPatients({ config, onUse }: { config: Config | null; onUse: (text: string) => void }) {
  if (!config?.demo_mode || !config.demo_patients) return null;
  return (
    <details className="demo-box">
      <summary>Callers to try (fake data)</summary>
      <ul className="demo-list">
        {config.demo_patients.map((p) => (
          <li key={p.name}>
            <span className="who">{p.name}</span>
            <span className="muted">born {p.dob} · {p.note}</span>
            <button className="chip-btn" type="button" onClick={() => onUse(`${p.name}, ${p.dob}`)}>Use</button>
          </li>
        ))}
      </ul>
    </details>
  );
}
