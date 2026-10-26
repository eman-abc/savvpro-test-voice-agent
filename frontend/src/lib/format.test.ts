import { describe, expect, it } from "vitest";
import { addDays, blockStyle, buildIcs, formatTime, isoDate, layoutLanes, mondayOf, parseLocal } from "./format";

describe("format helpers", () => {
  it("parses naive ISO strings without shifting the clock", () => {
    const d = parseLocal("2026-11-03T14:30:00");
    expect([d.getFullYear(), d.getMonth(), d.getDate(), d.getHours(), d.getMinutes()]).toEqual([2026, 10, 3, 14, 30]);
  });

  it("formats times the way the assistant says them", () => {
    expect(formatTime(parseLocal("2026-11-03T09:00:00"))).toBe("9 AM");
    expect(formatTime(parseLocal("2026-11-03T12:15:00"))).toBe("12:15 PM");
    expect(formatTime(parseLocal("2026-11-03T00:05:00"))).toBe("12:05 AM");
  });

  it("finds the Monday of a week, including for a Sunday", () => {
    expect(isoDate(mondayOf(parseLocal("2026-10-28T10:00:00")))).toBe("2026-10-26");
    expect(isoDate(mondayOf(parseLocal("2026-11-01T10:00:00")))).toBe("2026-10-26");
    expect(isoDate(addDays(parseLocal("2026-10-30T00:00:00"), 3))).toBe("2026-11-02");
  });

  it("positions calendar blocks from the first visible hour", () => {
    const { top, height } = blockStyle(parseLocal("2026-11-03T09:30:00"), parseLocal("2026-11-03T10:10:00"), 8, 60);
    expect(top).toBe(90);
    expect(Math.round(height)).toBe(40);
    expect(blockStyle(parseLocal("2026-11-03T09:00:00"), parseLocal("2026-11-03T09:05:00"), 8, 60).height).toBe(18);
  });

  it("builds an ICS event with escaped text", () => {
    const ics = buildIcs({
      uid: "7",
      start: parseLocal("2026-11-03T09:00:00"),
      end: parseLocal("2026-11-03T09:30:00"),
      title: "Visit, with Dr. Patel",
      location: "100 Example Street, Springfield",
      description: "Bring ID; insurance card",
    });
    expect(ics).toContain("DTSTART:20261103T090000");
    expect(ics).toContain("SUMMARY:Visit\\, with Dr. Patel");
    expect(ics).toContain("DESCRIPTION:Bring ID\\; insurance card");
    expect(ics.startsWith("BEGIN:VCALENDAR")).toBe(true);
  });

  it("puts overlapping appointments side by side instead of on top of each other", () => {
    const at = (h: number, m: number, len: number) => ({ start: parseLocal(`2026-11-03T${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:00`), end: parseLocal(`2026-11-03T${String(h).padStart(2, "0")}:${String(m + len).padStart(2, "0")}:00`) });
    const three = layoutLanes([at(10, 0, 30), at(10, 0, 15), at(10, 10, 20)]);
    expect(three.map((l) => l.lanes)).toEqual([3, 3, 3]);
    expect(new Set(three.map((l) => l.lane)).size).toBe(3);
    const separate = layoutLanes([at(9, 0, 15), at(11, 0, 15)]);
    expect(separate.map((l) => [l.lane, l.lanes])).toEqual([[0, 1], [0, 1]]);
    const reuse = layoutLanes([at(10, 0, 15), at(10, 0, 45), at(10, 20, 15)]); // third reuses lane 0 after the first ends
    expect(reuse.map((l) => l.lanes)).toEqual([2, 2, 2]);
    expect(reuse.find((l) => l.item.end.getMinutes() === 35)?.lane).toBe(0);
  });
});
