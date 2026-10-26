export const pad = (n: number) => String(n).padStart(2, "0");

/** Parse a naive clinic-local ISO string ("2026-11-03T09:15:00") without timezone shifting. */
export function parseLocal(iso: string): Date {
  const [d, t = "00:00:00"] = iso.split("T");
  const [y, mo, da] = d.split("-").map(Number);
  const [h, mi] = t.split(":").map(Number);
  return new Date(y, mo - 1, da, h, mi);
}

export const isoDate = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

export function formatTime(d: Date): string {
  const h = d.getHours() % 12 || 12;
  const m = d.getMinutes();
  return `${h}${m ? ":" + pad(m) : ""} ${d.getHours() < 12 ? "AM" : "PM"}`;
}

export const formatDay = (d: Date) => d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });

export function mondayOf(d: Date): Date {
  const out = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  out.setDate(out.getDate() - ((out.getDay() + 6) % 7));
  return out;
}

export function addDays(d: Date, n: number): Date {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
}

/** Vertical position and height of a calendar block, in pixels. */
export function blockStyle(start: Date, end: Date, firstHour: number, pxPerHour: number) {
  const top = (start.getHours() + start.getMinutes() / 60 - firstHour) * pxPerHour;
  const height = Math.max(((end.getTime() - start.getTime()) / 3_600_000) * pxPerHour, 18);
  return { top, height };
}

const escapeIcs = (s: string) => s.replace(/[\\;,]/g, (c) => "\\" + c).replace(/\n/g, "\\n");
const icsStamp = (d: Date) =>
  `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}T${pad(d.getHours())}${pad(d.getMinutes())}00`;

/** A floating-time .ics event (shows the same wall-clock time wherever it is opened). */
export function buildIcs(opts: {
  uid: string;
  start: Date;
  end: Date;
  title: string;
  location: string;
  description: string;
}): string {
  return [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//Clinic Voice Assistant//EN",
    "BEGIN:VEVENT",
    `UID:${opts.uid}@clinic-voice-agent`,
    `DTSTAMP:${icsStamp(new Date())}`,
    `DTSTART:${icsStamp(opts.start)}`,
    `DTEND:${icsStamp(opts.end)}`,
    `SUMMARY:${escapeIcs(opts.title)}`,
    `LOCATION:${escapeIcs(opts.location)}`,
    `DESCRIPTION:${escapeIcs(opts.description)}`,
    "END:VEVENT",
    "END:VCALENDAR",
  ].join("\r\n");
}

export const outcomeLabel: Record<string, string> = {
  booked: "Booked",
  rescheduled: "Rescheduled",
  cancelled: "Cancelled",
  listed: "Looked up",
  faq: "Answered a question",
  handoff: "Handed to staff",
  emergency: "Emergency advice",
  no_action: "No action",
  in_progress: "In progress",
  timeout: "Timed out",
  waitlisted: "Joined waitlist",
  no_response: "No response",
};

/**
 * Side-by-side lanes for appointments that overlap in time, so none hides another.
 * Each cluster of overlapping items shares its width equally.
 */
export function layoutLanes<T extends { start: Date; end: Date }>(items: T[]): { item: T; lane: number; lanes: number }[] {
  const sorted = [...items].sort((a, b) => a.start.getTime() - b.start.getTime() || a.end.getTime() - b.end.getTime());
  const out: { item: T; lane: number; lanes: number }[] = [];
  let cluster: { item: T; lane: number }[] = [];
  let laneEnds: number[] = [];
  let clusterEnd = -Infinity;
  const flush = () => {
    const lanes = laneEnds.length;
    cluster.forEach((c) => out.push({ ...c, lanes }));
    cluster = [];
    laneEnds = [];
    clusterEnd = -Infinity;
  };
  for (const item of sorted) {
    const s = item.start.getTime();
    const e = item.end.getTime();
    if (cluster.length && s >= clusterEnd) flush();
    let lane = laneEnds.findIndex((end) => end <= s);
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(e);
    } else {
      laneEnds[lane] = e;
    }
    cluster.push({ item, lane });
    clusterEnd = Math.max(clusterEnd, e);
  }
  flush();
  return out;
}
