import type { Trace } from "../lib/types";

function Turn({ t, open, old }: { t: Trace; open: boolean; old: boolean }) {
  const intent = t.intent;
  const conf = intent ? Math.round(intent.confidence * 100) : 0;
  const stateText = t.state.from === t.state.to ? t.state.to : `${t.state.from} → ${t.state.to}`;
  return (
    <li className={`turn ${old ? "old" : ""}`}>
      <details open={open}>
        <summary>
          <span className="n">{t.turn === 0 ? "Call starts" : `Turn ${t.turn}`}</span>
          {t.heard && <span className="heard">“{t.heard}”</span>}
          <span className="statechip">{stateText}</span>
        </summary>
        <div className="turn-body">
          {intent && intent.name !== "none" && (
            <div className="row">
              <span className="k">Intent</span>
              <div>
                <div className="conf">
                  <b>{intent.name}</b>
                  <span className={`bar ${conf < 55 ? "low" : ""}`} role="img" aria-label={`${conf}% confidence`}>
                    <i style={{ width: `${conf}%` }} />
                  </span>
                  <span className="num muted">{conf}%</span>
                  <span className="muted">{intent.decision === "act" ? "acted on it" : intent.decision === "clarify" ? `asked: ${intent.name} or ${intent.alternative}?` : "not sure"}</span>
                </div>
                {intent.ranked.length > 1 && (
                  <div className="muted">
                    also scored: {intent.ranked.slice(1).map((r) => `${r.name} ${r.score}`).join(", ")}
                  </div>
                )}
              </div>
            </div>
          )}
          {t.entities.length > 0 && (
            <div className="row">
              <span className="k">Picked up</span>
              <div className="chips">
                {t.entities.map((e, i) => (
                  <span className="chip" key={i} title={e.raw ? `from “${e.raw}”` : undefined}>
                    {e.type.replace(/_/g, " ")}: <b>{e.value}</b>
                  </span>
                ))}
              </div>
            </div>
          )}
          {t.steps.length > 0 && (
            <div className="row">
              <span className="k">Reasoning</span>
              <ul className="steps">
                {t.steps.map((s, i) => (
                  <li key={i} className={s.kind}>
                    <i aria-hidden="true" />
                    <span>
                      <span className="t">{s.label}</span>
                      {s.detail && <span className="d"> · {s.detail.replace(/ -> /g, " → ").replace(/->/g, "→")}</span>}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {t.action && (
            <div className="row">
              <span className="k">Did</span>
              <span className="action">{t.action.name.replace(/_/g, " ")}: {t.action.result}</span>
            </div>
          )}
          <div className="row">
            <span className="k">Said</span>
            <p className="said">{t.reply}</p>
          </div>
        </div>
      </details>
    </li>
  );
}

/** Newest decision first, expanded; older turns collapse but stay one click away. */
export default function TraceLedger({ traces, tall = false }: { traces: Trace[]; tall?: boolean }) {
  if (traces.length === 0) return <p className="empty">Decisions appear here as the call goes on.</p>;
  const ordered = [...traces].reverse();
  return (
    <ol className={`ledger ${tall ? "tall" : ""}`} aria-label="Decision ledger, newest first" style={{ margin: 0 }} tabIndex={tall ? undefined : 0}>
      {ordered.map((t, i) => (
        <Turn key={t.turn} t={t} open={i === 0} old={i !== 0} />
      ))}
    </ol>
  );
}
