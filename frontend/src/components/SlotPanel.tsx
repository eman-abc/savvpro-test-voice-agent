import { useEffect, useRef, useState } from "react";
import type { SlotView } from "../lib/types";

/** What the assistant has understood so far. A slot flashes when its value changes (e.g. a correction). */
export default function SlotPanel({ slots }: { slots: SlotView[] }) {
  const prev = useRef<Record<string, string | null>>({});
  const [changed, setChanged] = useState<Set<string>>(new Set());

  useEffect(() => {
    const next = new Set<string>();
    for (const s of slots) {
      if (prev.current[s.label] !== undefined && prev.current[s.label] !== s.value && s.value) next.add(s.label);
    }
    prev.current = Object.fromEntries(slots.map((s) => [s.label, s.value]));
    setChanged(next);
    if (next.size) {
      const t = setTimeout(() => setChanged(new Set()), 1500);
      return () => clearTimeout(t);
    }
  }, [slots]);

  return (
    <section className="slots" aria-label="What the assistant has so far" aria-live="polite">
      <h3>What I have so far</h3>
      {slots.length === 0 ? (
        <p className="slots-empty">Nothing yet. Ask for an appointment or a question.</p>
      ) : (
        <div className="slotlist">
          {slots.map((s) => (
            <div key={s.label} className={`slot ${s.value ? "filled" : ""} ${changed.has(s.label) ? "changed" : ""}`}>
              <div className="k">{s.label}</div>
              <div className="v">{s.value ?? "—"}</div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
