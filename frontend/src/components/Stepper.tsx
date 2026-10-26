import type { Progress } from "../lib/types";

export default function Stepper({ progress, ended }: { progress: Progress; ended: boolean }) {
  return (
    <ol className="stepper" aria-label="Call progress" style={{ listStyle: "none", padding: 0 }}>
      {progress.steps.map((label, i) => {
        const done = i < progress.current || (ended && i === progress.current) || (label === "Verify" && progress.verified);
        const current = i === progress.current && !ended;
        const skipped = !done && !current && label === "Verify" && progress.current > i;
        return (
          <li key={label} className={`step ${done ? "done" : ""} ${current ? "current" : ""} ${skipped ? "skipped" : ""}`} aria-current={current ? "step" : undefined}>
            <span className="dot" aria-hidden="true">{done ? "✓" : i + 1}</span>
            <span className="lbl">{label}</span>
          </li>
        );
      })}
    </ol>
  );
}
