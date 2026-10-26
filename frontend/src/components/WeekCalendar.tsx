import { addDays, blockStyle, formatDay, formatTime, isoDate, layoutLanes, parseLocal } from "../lib/format";
import type { StaffAppointment } from "../lib/types";

const FIRST_HOUR = 8;
const LAST_HOUR = 18;
const PX = 56;

interface Props {
  weekStart: Date;
  appointments: StaffAppointment[];
  now: Date | null;
  selectedId: number | null;
  onSelect: (a: StaffAppointment) => void;
}

export default function WeekCalendar({ weekStart, appointments, now, selectedId, onSelect }: Props) {
  const hasSunday = appointments.some((a) => parseLocal(a.start).getDay() === 0);
  const days = Array.from({ length: hasSunday ? 7 : 6 }, (_, i) => addDays(weekStart, i));
  const hours = Array.from({ length: LAST_HOUR - FIRST_HOUR }, (_, i) => FIRST_HOUR + i);
  const todayIso = now ? isoDate(now) : "";
  const height = (LAST_HOUR - FIRST_HOUR) * PX;

  return (
    <div className="cal">
      <div className="cal-grid" style={{ gridTemplateColumns: `56px repeat(${days.length}, minmax(110px, 1fr))` }} role="group" aria-label="Week calendar">
        <div className="cal-head">
          <div />
          {days.map((d) => (
            <div key={isoDate(d)} className={isoDate(d) === todayIso ? "today" : ""}>{formatDay(d)}</div>
          ))}
        </div>
        <div className="cal-hours" style={{ height }}>
          {hours.map((h) => (
            <span key={h} className="cal-hour" style={{ top: (h - FIRST_HOUR) * PX }}>{formatTime(new Date(2000, 0, 1, h))}</span>
          ))}
        </div>
        {days.map((d) => {
          const iso = isoDate(d);
          const dayAppts = appointments.filter((a) => a.start.startsWith(iso));
          return (
            <div key={iso} className={`cal-col ${iso === todayIso ? "today" : ""}`} style={{ height, backgroundSize: `100% ${PX}px` }}>
              {now && iso === todayIso && now.getHours() >= FIRST_HOUR && now.getHours() < LAST_HOUR && (
                <div role="img" className="cal-now" style={{ top: (now.getHours() + now.getMinutes() / 60 - FIRST_HOUR) * PX }} aria-label="Current time" />
              )}
              {layoutLanes(dayAppts.map((a) => ({ a, start: parseLocal(a.start), end: parseLocal(a.end) }))).map(({ item, lane, lanes }) => {
                const { a, start: s, end: e } = item;
                const { top, height: h } = blockStyle(s, e, FIRST_HOUR, PX);
                const width = 100 / lanes;
                return (
                  <button
                    key={a.id}
                    className="block"
                    style={{ top, height: h, left: `calc(${lane * width}% + 3px)`, width: `calc(${width}% - 6px)`, right: "auto", borderLeftColor: a.color }}
                    aria-pressed={selectedId === a.id}
                    onClick={() => onSelect(a)}
                    aria-label={`${formatTime(s)} ${a.patient}, ${a.service_name} with ${a.provider_name}`}
                  >
                    <div className="bt">{a.confirmed ? "✓ " : ""}{lanes > 1 ? a.patient.split(" ")[0] : `${formatTime(s)} · ${a.patient}`}</div>
                    {h > 34 && lanes < 3 && <div className="bs">{lanes > 1 ? formatTime(s) : a.service_name}</div>}
                  </button>
                );
              })}
            </div>
          );
        })}
      </div>
    </div>
  );
}
