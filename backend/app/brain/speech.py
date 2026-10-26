"""Formatting helpers: how the assistant says dates, times and lists out loud."""

from __future__ import annotations

from datetime import date, datetime

from app.brain.text import ordinal_suffix


def fmt_time(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{h} {suffix}" if dt.minute == 0 else f"{h}:{dt.minute:02d} {suffix}"


def fmt_date(d: date, today: date | None = None, with_weekday: bool = True) -> str:
    if today:
        if d == today:
            return "today"
        if (d - today).days == 1:
            return "tomorrow"
    day = f"{d.strftime('%B')} {d.day}{ordinal_suffix(d.day)}"
    return f"{d.strftime('%A')}, {day}" if with_weekday else day


def join_or(items: list[str], conj: str = "or") -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])}, {conj} {items[-1]}" if len(items) > 2 else f"{items[0]} {conj} {items[1]}"


def weekday_names(days: list[int]) -> str:
    names = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"]
    return join_or([names[d] for d in sorted(days)], "and")


def on_date(d: date, today: date | None = None) -> str:
    """ "today", "tomorrow" or "on Tuesday, November 3rd" - reads naturally after a verb."""
    pretty = fmt_date(d, today)
    return pretty if pretty in ("today", "tomorrow") else f"on {pretty}"


def cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def a_an(phrase: str) -> str:
    return ("an " if phrase[:1].lower() in "aeiou" else "a ") + phrase
