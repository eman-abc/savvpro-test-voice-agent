export interface TraceStep {
  kind: "rule" | "check" | "correction" | "switch" | "note" | "warn";
  label: string;
  detail: string;
}
export interface SlotView {
  label: string;
  value: string | null;
}
export interface IntentInfo {
  name: string;
  decision: "act" | "clarify" | "none";
  confidence: number;
  alternative: string | null;
  ranked: { name: string; score: number }[];
  faq_match: string | null;
}
export interface Trace {
  turn: number;
  heard: string;
  normalized: string;
  intent: IntentInfo | null;
  entities: { type: string; value: string; raw: string }[];
  steps: TraceStep[];
  slots: SlotView[];
  state: { from: string; to: string; flow: string | null };
  action: { name: string; result: string } | null;
  reply: string;
}
export interface Progress {
  steps: string[];
  current: number;
  verified: boolean;
}
export interface AppointmentInfo {
  id: number;
  action: "booked" | "rescheduled" | "cancelled";
  description: string;
  start: string;
  end: string;
  service_name: string;
  provider_name: string;
}
export interface TurnOut {
  call_id: string;
  reply: string;
  trace: Trace;
  progress: Progress;
  ended: boolean;
  lang: "en" | "ur";
  outcome: string | null;
  appointment: AppointmentInfo | null;
}
export interface Config {
  clinic: { name: string; phone: string; address: string; timezone: string };
  demo_mode: boolean;
  services: { key: string; name: string; minutes: number }[];
  policy: { cancel_notice_hours: number };
  demo_patients?: { name: string; dob: string; note: string }[];
  demo_staff?: { username: string; password: string };
}
export interface StaffAppointment {
  id: number;
  patient: string;
  patient_phone_last4: string | null;
  provider: string;
  provider_name: string;
  color: string;
  service: string;
  service_name: string;
  start: string;
  end: string;
  status: string;
  late_change: boolean;
  reschedule_count: number;
  created_via: string;
  cancel_reason: string | null;
  reminder_sent: boolean;
  confirmed: boolean;
}
export interface Provider {
  key: string;
  name: string;
  specialty: string;
  color: string;
}
export interface CallSummary {
  id: string;
  started_at: string;
  turns: number;
  outcome: string;
  handoff_summary: string | null;
}
export interface CallDetail {
  id: string;
  started_at: string;
  outcome: string | null;
  handoff_summary: string | null;
  turns: { idx: number; user: string | null; reply: string; trace: Trace }[];
}
export interface Stats {
  now: string;
  calls_total: number;
  outcomes: Record<string, number>;
  avg_turns: number;
  resolved_without_human: number;
  upcoming_appointments: number;
  waitlist_waiting: number;
  waitlist_filled: number;
}
export interface ScenarioStep {
  say: string;
  reply: string;
  ok: boolean;
  problems: string[];
}
export interface ScenarioResult {
  id: string;
  title: string;
  tags: string[];
  passed: boolean;
  turns: number;
  failures: string[];
  transcript: ScenarioStep[];
}
export interface ScenarioRun {
  total: number;
  passed: number;
  completion_rate: number;
  avg_turns: number;
  intent_accuracy: number | null;
  results: ScenarioResult[];
}
export interface NotificationRow {
  id: number;
  created_at: string;
  patient: string;
  kind: "reminder" | "offer" | "reply";
  body: string;
  status: string;
  lang: string;
  reply_text: string | null;
  phone_last4: string;
}
export interface WaitlistRow {
  id: number;
  patient: string;
  service: string;
  provider: string | null;
  date_from: string;
  date_to: string;
  time_from: string | null;
  time_to: string | null;
  status: string;
  lang: string;
  offered_start: string | null;
  offer_expires_at: string | null;
}
