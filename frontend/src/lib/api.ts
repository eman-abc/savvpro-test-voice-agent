import type {
  CallDetail,
  CallSummary,
  Config,
  NotificationRow,
  Provider,
  ScenarioRun,
  StaffAppointment,
  Stats,
  TurnOut,
  WaitlistRow,
} from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function csrfToken(): string {
  const m = document.cookie.match(/(?:^|;\s*)clinic_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (init.body) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken();
  let res: Response;
  try {
    res = await fetch(path, { credentials: "same-origin", ...init, headers });
  } catch {
    throw new ApiError(0, "Can't reach the server. Check your connection and try again.");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
}

export const api = {
  config: () => request<Config>("/api/config"),
  startCall: (lang: "en" | "ur" = "en") => post<TurnOut>("/api/calls", { lang }),
  // `seq` = turns already seen; it makes a retried request safe (the server replays instead of re-processing).
  turn: (callId: string, text: string, seq: number) => post<TurnOut>(`/api/calls/${callId}/turn`, { text, seq }),
  login: (username: string, password: string) => post<{ username: string; role: string }>("/api/auth/login", { username, password }),
  logout: () => post<{ ok: boolean }>("/api/auth/logout"),
  me: () => request<{ username: string; role: string }>("/api/auth/me"),
  providers: () => request<Provider[]>("/api/staff/providers"),
  appointments: (start: string, end: string) =>
    request<StaffAppointment[]>(`/api/staff/appointments?start=${start}&end=${end}`),
  cancelAppointment: (id: number, reason: string) => post<StaffAppointment>(`/api/staff/appointments/${id}/cancel`, { reason }),
  calls: () => request<CallSummary[]>("/api/staff/calls?limit=50"),
  call: (id: string) => request<CallDetail>(`/api/staff/calls/${id}`),
  stats: () => request<Stats>("/api/staff/stats"),
  resetDemo: () => post<{ ok: boolean }>("/api/staff/demo/reset"),
  runScenarios: () => post<ScenarioRun>("/api/staff/scenarios/run"),
  notifications: () => request<NotificationRow[]>("/api/staff/notifications?limit=100"),
  replyAs: (id: number, text: string) => post<{ reply: string | null }>(`/api/staff/notifications/${id}/reply`, { text }),
  runReminders: (hours: number) => post<{ sent: number; expired: number }>("/api/staff/reminders/run", { hours }),
  waitlist: () => request<WaitlistRow[]>("/api/staff/waitlist"),
  cancelWaitlist: (id: number) => post<{ id: number; status: string }>(`/api/staff/waitlist/${id}/cancel`),
};
