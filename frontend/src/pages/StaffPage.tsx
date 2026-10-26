import { FormEvent, Fragment, useCallback, useEffect, useMemo, useState } from "react";
import TraceLedger from "../components/TraceLedger";
import WeekCalendar from "../components/WeekCalendar";
import { ApiError, api } from "../lib/api";
import { addDays, formatDay, formatTime, isoDate, mondayOf, outcomeLabel, parseLocal } from "../lib/format";
import type { CallDetail, CallSummary, Config, NotificationRow, Provider, ScenarioRun, StaffAppointment, Stats, WaitlistRow } from "../lib/types";

type Tab = "calendar" | "appointments" | "messages" | "waitlist" | "calls" | "scenarios";

export default function StaffPage({ config }: { config: Config | null }) {
  const [user, setUser] = useState<{ username: string; role: string } | null | undefined>(undefined);

  useEffect(() => {
    api.me().then(setUser).catch(() => setUser(null));
  }, []);

  if (user === undefined) return <p className="empty">Checking your session…</p>;
  if (user === null) return <Login config={config} onDone={setUser} />;
  return <Dashboard user={user} config={config} onSignOut={() => setUser(null)} />;
}

function Login({ config, onDone }: { config: Config | null; onDone: (u: { username: string; role: string }) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onDone(await api.login(username, password));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="login">
      <h1>Staff sign in</h1>
      <p className="muted">See the schedule, cancel appointments and review how every call was handled.</p>
      <form onSubmit={submit}>
        <div className="field">
          <label htmlFor="u">Username</label>
          <input id="u" className="input" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
        </div>
        <div className="field">
          <label htmlFor="p">Password</label>
          <input id="p" className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
        </div>
        {error && <p className="notice error" role="alert">{error}</p>}
        <button className="btn primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
      {config?.demo_staff && (
        <p className="notice info">
          Demo login: <b>{config.demo_staff.username}</b> / <b>{config.demo_staff.password}</b>{" "}
          <button className="chip-btn" type="button" onClick={() => { setUsername(config.demo_staff!.username); setPassword(config.demo_staff!.password); }}>Fill it in</button>
        </p>
      )}
    </section>
  );
}

function Dashboard({ user, config, onSignOut }: { user: { username: string; role: string }; config: Config | null; onSignOut: () => void }) {
  const [tab, setTab] = useState<Tab>("calendar");
  const [stats, setStats] = useState<Stats | null>(null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [error, setError] = useState<string | null>(null);

  const handle = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return onSignOut();
      setError((e as Error).message);
    },
    [onSignOut],
  );

  const loadStats = useCallback(() => api.stats().then(setStats).catch(handle), [handle]);
  useEffect(() => {
    void loadStats();
    api.providers().then(setProviders).catch(handle);
  }, [loadStats, handle]);

  const signOut = async () => {
    try {
      await api.logout();
    } finally {
      onSignOut();
    }
  };
  const reset = async () => {
    if (!window.confirm("Reset all demo data? This deletes every appointment and call log.")) return;
    try {
      await api.resetDemo();
      window.location.reload();
    } catch (e) {
      handle(e);
    }
  };

  const now = stats ? parseLocal(stats.now) : null;
  return (
    <>
      <div className="toolbar">
        <h1 style={{ fontSize: 22, letterSpacing: "-0.02em" }}>Front desk</h1>
        <span className="spacer" />
        <span className="muted">Signed in as {user.username}</span>
        {config?.demo_mode && user.role === "admin" && <button className="btn small" onClick={reset}>Reset demo data</button>}
        <button className="btn small" onClick={signOut}>Sign out</button>
      </div>
      {stats && (
        <dl className="stats" style={{ margin: "4px 0 18px" }}>
          <Stat v={stats.upcoming_appointments} k="upcoming appointments" />
          <Stat v={stats.calls_total} k="calls handled" />
          <Stat v={stats.waitlist_waiting} k="on the waitlist" />
          <Stat v={stats.waitlist_filled} k="slots recovered" />
          <Stat v={`${stats.resolved_without_human}%`} k="resolved without staff" />
          <Stat v={stats.avg_turns} k="turns per call" />
        </dl>
      )}
      {error && <p className="notice error" role="alert">{error}</p>}
      <div className="tabbar" role="tablist">
        {(["calendar", "appointments", "messages", "waitlist", "calls", "scenarios"] as Tab[]).map((t) => (
          <button key={t} role="tab" className="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
            {{ calendar: "Calendar", appointments: "Appointments", messages: "Messages", waitlist: "Waitlist", calls: "Call log", scenarios: "Scenarios" }[t]}
          </button>
        ))}
      </div>
      {tab === "calendar" && <CalendarTab providers={providers} now={now} onChange={loadStats} onError={handle} />}
      {tab === "appointments" && <AppointmentsTab now={now} onChange={loadStats} onError={handle} />}
      {tab === "messages" && <MessagesTab onChange={loadStats} onError={handle} isAdmin={user.role === "admin"} />}
      {tab === "waitlist" && <WaitlistTab onChange={loadStats} onError={handle} />}
      {tab === "calls" && <CallsTab onError={handle} />}
      {tab === "scenarios" && <ScenariosTab onError={handle} />}
    </>
  );
}

function Stat({ v, k }: { v: string | number; k: string }) {
  return (
    <div className="stat">
      <dd className="v num" style={{ margin: 0 }}>{v}</dd>
      <dt className="k">{k}</dt>
    </div>
  );
}

function useAppointments(start: Date, days: number, onError: (e: unknown) => void) {
  const [appts, setAppts] = useState<StaffAppointment[]>([]);
  const [loading, setLoading] = useState(true);
  const load = useCallback(() => {
    setLoading(true);
    api
      .appointments(isoDate(start), isoDate(addDays(start, days)))
      .then(setAppts)
      .catch(onError)
      .finally(() => setLoading(false));
  }, [start, days, onError]);
  useEffect(load, [load]);
  return { appts, loading, reload: load };
}

function lateBadge(a: StaffAppointment) {
  return a.reschedule_count > 0 ? <span className="badge">moved {a.reschedule_count}×</span> : null;
}

function CalendarTab({ providers, now, onChange, onError }: { providers: Provider[]; now: Date | null; onChange: () => void; onError: (e: unknown) => void }) {
  const [weekStart, setWeekStart] = useState(() => mondayOf(new Date()));
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<StaffAppointment | null>(null);
  const [confirming, setConfirming] = useState(false);
  const { appts, loading, reload } = useAppointments(weekStart, 7, onError);

  // The server clock decides "today" so the demo makes sense whatever the viewer's timezone.
  useEffect(() => {
    if (now) setWeekStart(mondayOf(now));
  }, [now]);

  const visible = useMemo(() => appts.filter((a) => !hidden.has(a.provider)), [appts, hidden]);
  const toggle = (k: string) => setHidden((h) => { const n = new Set(h); if (n.has(k)) n.delete(k); else n.add(k); return n; });

  const cancel = async () => {
    if (!selected) return;
    try {
      await api.cancelAppointment(selected.id, "cancelled by front desk");
      setSelected(null);
      setConfirming(false);
      reload();
      onChange();
    } catch (e) {
      onError(e);
    }
  };

  const past = selected && now ? parseLocal(selected.start) < now : false;
  return (
    <>
      <div className="toolbar">
        <button className="btn small" onClick={() => setWeekStart(addDays(weekStart, -7))} aria-label="Previous week">←</button>
        <button className="btn small" onClick={() => setWeekStart(mondayOf(now ?? new Date()))}>This week</button>
        <button className="btn small" onClick={() => setWeekStart(addDays(weekStart, 7))} aria-label="Next week">→</button>
        <strong className="num">{formatDay(weekStart)} – {formatDay(addDays(weekStart, 6))}</strong>
        {loading && <span className="muted">Loading…</span>}
        <span className="spacer" />
        <div className="legend" aria-label="Filter by provider">
          {providers.map((p) => (
            <button key={p.key} aria-pressed={!hidden.has(p.key)} onClick={() => toggle(p.key)}>
              <i style={{ background: p.color }} />{p.name}
            </button>
          ))}
        </div>
      </div>
      <WeekCalendar weekStart={weekStart} appointments={visible} now={now} selectedId={selected?.id ?? null} onSelect={(a) => { setSelected(a); setConfirming(false); }} />
      {selected ? (
        <div className="panel detail">
          <dl>
            <div><dt>Patient</dt><dd>{selected.patient}</dd></div>
            <div><dt>Visit</dt><dd>{selected.service_name}</dd></div>
            <div><dt>With</dt><dd>{selected.provider_name}</dd></div>
            <div><dt>When</dt><dd>{formatDay(parseLocal(selected.start))}, {formatTime(parseLocal(selected.start))}</dd></div>
            <div><dt>Booked by</dt><dd>{selected.created_via === "voice" ? "Voice assistant" : "Staff"} {lateBadge(selected)}</dd></div>
            <div><dt>Reminder</dt><dd>{selected.reminder_sent ? "Sent" : "Not sent yet"}</dd></div>
            <div><dt>Patient confirmed</dt><dd>{selected.confirmed ? "Yes" : "Not yet"}</dd></div>
          </dl>
          {past ? (
            <span className="muted">Past visit</span>
          ) : confirming ? (
            <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
              Cancel this appointment?
              <button className="btn small danger" onClick={cancel}>Yes, cancel it</button>
              <button className="btn small" onClick={() => setConfirming(false)}>Keep it</button>
            </span>
          ) : (
            <button className="btn small danger" onClick={() => setConfirming(true)}>Cancel appointment</button>
          )}
        </div>
      ) : (
        <p className="muted" style={{ marginTop: 10 }}>Select an appointment to see details or cancel it.</p>
      )}
    </>
  );
}

function AppointmentsTab({ now, onChange, onError }: { now: Date | null; onChange: () => void; onError: (e: unknown) => void }) {
  const start = useMemo(() => mondayOf(now ?? new Date()), [now]);
  const { appts, loading, reload } = useAppointments(start, 21, onError);
  const [confirmId, setConfirmId] = useState<number | null>(null);

  const cancel = async (id: number) => {
    try {
      await api.cancelAppointment(id, "cancelled by front desk");
      setConfirmId(null);
      reload();
      onChange();
    } catch (e) {
      onError(e);
    }
  };

  if (loading && appts.length === 0) return <p className="empty" role="status">Loading appointments…</p>;
  if (appts.length === 0) return <p className="empty">No appointments in the next three weeks.</p>;
  return (
    <table className="list">
      <thead>
        <tr><th>When</th><th>Patient</th><th>Visit</th><th>With</th><th>Booked by</th><th><span className="sr-only">Actions</span></th></tr>
      </thead>
      <tbody>
        {appts.map((a) => {
          const s = parseLocal(a.start);
          const isPast = now ? s < now : false;
          return (
            <tr key={a.id}>
              <td className="num">{formatDay(s)}, {formatTime(s)}</td>
              <td>{a.patient}</td>
              <td>{a.service_name}</td>
              <td><span style={{ borderLeft: `4px solid ${a.color}`, paddingLeft: 8 }}>{a.provider_name}</span></td>
              <td>{a.created_via === "voice" ? <span className="badge">Voice</span> : a.created_via === "waitlist" ? <span className="badge ok">Waitlist</span> : "Staff"} {lateBadge(a)} {a.confirmed && <span className="badge ok">Confirmed</span>}</td>
              <td style={{ textAlign: "right" }}>
                {isPast ? <span className="muted">Past</span> : confirmId === a.id ? (
                  <span style={{ display: "inline-flex", gap: 6 }}>
                    <button className="btn small danger" onClick={() => cancel(a.id)}>Confirm cancel</button>
                    <button className="btn small" onClick={() => setConfirmId(null)}>Keep</button>
                  </span>
                ) : (
                  <button className="btn small" onClick={() => setConfirmId(a.id)}>Cancel</button>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

const outcomeTone = (o: string) => (["booked", "rescheduled", "faq", "listed"].includes(o) ? "ok" : ["handoff", "emergency", "cancelled"].includes(o) ? "warn" : "");

function CallsTab({ onError }: { onError: (e: unknown) => void }) {
  const [calls, setCalls] = useState<CallSummary[] | null>(null);
  const [detail, setDetail] = useState<CallDetail | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);

  useEffect(() => {
    api.calls().then(setCalls).catch(onError);
  }, [onError]);

  const open = (id: string) => {
    setActiveId(id);
    api.call(id).then(setDetail).catch(onError);
  };

  if (calls === null) return <p className="empty">Loading calls…</p>;
  if (calls.length === 0) return <p className="empty">No calls yet. Start one from “Call the assistant”.</p>;
  return (
    <div className="calls">
      <div className="panel">
        <ul className="calllist">
          {calls.map((c) => (
            <li key={c.id}>
              <button aria-current={activeId === c.id} onClick={() => open(c.id)}>
                <span className="num">{formatDay(parseLocal(c.started_at))} {formatTime(parseLocal(c.started_at))}</span>
                <span>
                  <span className={`badge ${outcomeTone(c.outcome)}`}>{outcomeLabel[c.outcome] ?? c.outcome}</span>{" "}
                  <span className="muted">{c.turns} turns</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>
      <div className="panel">
        {!detail || detail.id !== activeId ? (
          <p className="empty">Choose a call to see the transcript and every decision.</p>
        ) : (
          <>
            {detail.handoff_summary && <p className="notice info" style={{ margin: 12 }}><b>Handoff summary:</b> {detail.handoff_summary}</p>}
            <div className="transcript-static">
              {detail.turns.map((t) => (
                <div key={t.idx} style={{ display: "grid", gap: 4 }}>
                  {t.user !== null && <div className="msg user" style={{ maxWidth: "100%" }}><span className="who">Caller</span>{t.user || "(silence)"}</div>}
                  <div className="msg bot" style={{ maxWidth: "100%" }}><span className="who">Assistant</span>{t.reply}</div>
                </div>
              ))}
            </div>
            <TraceLedger traces={detail.turns.map((t) => t.trace)} tall />
          </>
        )}
      </div>
    </div>
  );
}

function ScenariosTab({ onError }: { onError: (e: unknown) => void }) {
  const [run, setRun] = useState<ScenarioRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);

  const go = async () => {
    setBusy(true);
    try {
      setRun(await api.runScenarios());
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="toolbar">
        <button className="btn primary" onClick={go} disabled={busy}>{busy ? "Running…" : run ? "Run again" : "Run all scenarios"}</button>
        <span className="muted">Replays scripted calls on a throw-away database. Live data is never touched.</span>
      </div>
      {!run ? (
        <p className="empty">Each scenario is a short scripted call (corrections, wrong identity, silence, late cancels…). Run them to see how the assistant scores.</p>
      ) : (
        <>
          <dl className="stats" style={{ margin: "4px 0 16px" }}>
            <Stat v={`${run.passed}/${run.total}`} k="scenarios passed" />
            <Stat v={`${run.completion_rate}%`} k="task completion" />
            <Stat v={run.intent_accuracy === null ? "n/a" : `${run.intent_accuracy}%`} k="intent accuracy" />
            <Stat v={run.avg_turns} k="turns per call" />
          </dl>
          <table className="list">
            <thead><tr><th>Scenario</th><th>Result</th><th>Turns</th><th><span className="sr-only">Transcript</span></th></tr></thead>
            <tbody>
              {run.results.map((r) => (
                <Fragment key={r.id}>
                  <tr>
                    <td>{r.title}<div className="muted">{r.tags.join(", ")}</div></td>
                    <td><span className={`badge ${r.passed ? "ok" : "bad"}`}>{r.passed ? "Pass" : "Fail"}</span></td>
                    <td className="num">{r.turns}</td>
                    <td style={{ textAlign: "right" }}>
                      <button className="btn small" aria-expanded={openId === r.id} onClick={() => setOpenId(openId === r.id ? null : r.id)}>{openId === r.id ? "Hide" : "Show"}</button>
                    </td>
                  </tr>
                  {openId === r.id && (
                    <tr>
                      <td colSpan={4}>
                        <div style={{ display: "grid", gap: 6 }}>
                          {r.transcript.map((t, i) => (
                            <div key={i} style={{ display: "grid", gap: 2 }}>
                              <div className="msg user" style={{ maxWidth: "100%" }}><span className="who">Caller</span>{t.say || "(silence)"}</div>
                              <div className="msg bot" style={{ maxWidth: "100%" }}><span className="who">Assistant</span>{t.reply}</div>
                              {t.problems.map((p) => <p key={p} className="notice error">{p}</p>)}
                            </div>
                          ))}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  );
}

const statusTone = (s: string) => (["confirmed", "accepted", "booked"].includes(s) ? "ok" : ["cancelled", "declined", "expired", "failed"].includes(s) ? "bad" : ["awaiting_confirm", "offered"].includes(s) ? "warn" : "");

function MessagesTab({ onChange, onError, isAdmin }: { onChange: () => void; onError: (e: unknown) => void; isAdmin: boolean }) {
  const [rows, setRows] = useState<NotificationRow[] | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [text, setText] = useState<Record<number, string>>({});
  const load = useCallback(() => api.notifications().then(setRows).catch(onError), [onError]);
  useEffect(() => void load(), [load]);

  const reply = async (id: number, body: string) => {
    if (!body.trim()) return;
    try {
      const r = await api.replyAs(id, body);
      setText((t) => ({ ...t, [id]: "" }));
      await load(); // refresh the table first, so the confirmation never appears before the new status does
      setNote(r.reply ?? "No reply.");
      onChange();
    } catch (e) {
      onError(e);
    }
  };
  const sendDue = async () => {
    try {
      const r = await api.runReminders(24 * 14);
      setNote(`Sent ${r.sent} reminder(s); ${r.expired} waitlist item(s) expired.`);
      await load();
    } catch (e) {
      onError(e);
    }
  };

  if (rows === null) return <p className="empty">Loading messages…</p>;
  return (
    <>
      <div className="toolbar">
        {isAdmin && <button className="btn small" onClick={sendDue}>Send due reminders now (next 14 days)</button>}
        <span className="muted">Messages are recorded here. In production an SMS/WhatsApp adapter delivers them; replies arrive on the webhook.</span>
      </div>
      {note && <p className="notice info" role="status" dir="auto" lang={/[\u0600-\u06FF]/.test(note) ? "ur" : undefined}>Patient receives: {note}</p>}
      {rows.length === 0 ? (
        <p className="empty">No messages yet. Use “Send due reminders now”, or cancel an appointment that someone is waiting for.</p>
      ) : (
        <table className="list">
          <thead><tr><th>When</th><th>Patient</th><th>Message</th><th>Status</th><th>Reply as the patient</th></tr></thead>
          <tbody>
            {rows.map((n) => {
              const open = n.kind !== "reply" && ["sent", "awaiting_confirm"].includes(n.status);
              return (
                <tr key={n.id}>
                  <td className="num">{formatDay(parseLocal(n.created_at))} {formatTime(parseLocal(n.created_at))}</td>
                  <td>{n.patient}<div className="muted">···{n.phone_last4} · {n.lang === "ur" ? "اردو" : "English"}</div></td>
                  <td style={{ maxWidth: 420 }}>
                    <span className="badge">{n.kind}</span>{" "}
                    <span dir="auto" lang={n.lang === "ur" ? "ur" : undefined}>{n.body}</span>
                    {n.reply_text && <div className="muted">Patient replied: <b dir="auto">{n.reply_text}</b></div>}
                  </td>
                  <td><span className={`badge ${statusTone(n.status)}`}>{n.status.replace("_", " ")}</span></td>
                  <td>
                    {open ? (
                      <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
                        <button className="btn small" onClick={() => reply(n.id, "1")}>1 · {n.kind === "offer" ? "Book" : "Confirm"}</button>
                        <button className="btn small" onClick={() => reply(n.id, "2")}>2 · {n.kind === "offer" ? "Skip" : "Cancel"}</button>
                        <input className="input" style={{ width: 130, minHeight: 30 }} dir="auto" aria-label={`Type a reply from ${n.patient}`} placeholder="or type…" value={text[n.id] ?? ""} onChange={(e) => setText((t) => ({ ...t, [n.id]: e.target.value }))} onKeyDown={(e) => { if (e.key === "Enter") void reply(n.id, text[n.id] ?? ""); }} />
                      </span>
                    ) : <span className="muted">—</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </>
  );
}

function WaitlistTab({ onChange, onError }: { onChange: () => void; onError: (e: unknown) => void }) {
  const [rows, setRows] = useState<WaitlistRow[] | null>(null);
  const load = useCallback(() => api.waitlist().then(setRows).catch(onError), [onError]);
  useEffect(() => void load(), [load]);
  const cancel = async (id: number) => {
    try {
      await api.cancelWaitlist(id);
      await load();
      onChange();
    } catch (e) {
      onError(e);
    }
  };
  if (rows === null) return <p className="empty">Loading the waitlist…</p>;
  if (rows.length === 0) return <p className="empty">Nobody is waiting. When a caller can’t get a slot they can join from the phone assistant, and a cancellation offers the slot to the first match.</p>;
  return (
    <table className="list">
      <thead><tr><th>Patient</th><th>Wants</th><th>Dates</th><th>Status</th><th>Offer</th><th><span className="sr-only">Actions</span></th></tr></thead>
      <tbody>
        {rows.map((e) => (
          <tr key={e.id}>
            <td>{e.patient}<div className="muted">{e.lang === "ur" ? "اردو" : "English"}</div></td>
            <td>{e.service}{e.provider ? ` · ${e.provider}` : ""}{e.time_from ? ` · ${e.time_from}–${e.time_to}` : ""}</td>
            <td className="num">{e.date_from === e.date_to ? e.date_from : `${e.date_from} → ${e.date_to}`}</td>
            <td><span className={`badge ${statusTone(e.status)}`}>{e.status}</span></td>
            <td className="num">{e.offered_start ? `${formatDay(parseLocal(e.offered_start))} ${formatTime(parseLocal(e.offered_start))}` : "—"}{e.offer_expires_at && <div className="muted">expires {formatTime(parseLocal(e.offer_expires_at))}</div>}</td>
            <td style={{ textAlign: "right" }}>{["waiting", "offered"].includes(e.status) && <button className="btn small" onClick={() => cancel(e.id)}>Remove</button>}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
