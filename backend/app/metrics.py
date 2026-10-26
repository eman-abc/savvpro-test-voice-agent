"""Tiny Prometheus-format metrics (no extra dependency). Labels are kept low-cardinality on purpose."""

from __future__ import annotations

import threading
from collections import defaultdict

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)
HELP = {
    "http_requests_total": "HTTP requests by method, route template and status",
    "http_request_seconds": "HTTP request latency",
    "voice_turns_total": "Conversation turns by detected intent and decision",
    "voice_turn_seconds": "Time the brain took per turn (including database work)",
    "voice_calls_ended_total": "Finished calls by outcome",
    "voice_misses_total": "Turns the assistant did not understand",
}


def _labels(d: dict[str, str]) -> str:
    return "{" + ",".join(f'{k}="{str(v).replace(chr(34), "")}"' for k, v in sorted(d.items())) + "}" if d else ""


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple], float] = defaultdict(float)
        self._hist: dict[tuple[str, tuple], list[float]] = {}

    def inc(self, name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
        with self._lock:
            self._counters[(name, tuple(sorted((labels or {}).items())))] += value

    def observe(self, name: str, seconds: float, labels: dict[str, str] | None = None) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            h = self._hist.setdefault(key, [0.0] * (len(BUCKETS) + 2))  # buckets..., +Inf, sum
            for i, b in enumerate(BUCKETS):
                if seconds <= b:
                    h[i] += 1
            h[len(BUCKETS)] += 1
            h[len(BUCKETS) + 1] += seconds

    def render(self) -> str:
        out: list[str] = []
        with self._lock:
            for name in sorted({k[0] for k in self._counters}):
                out += [f"# HELP {name} {HELP.get(name, name)}", f"# TYPE {name} counter"]
                for (n, lab), v in sorted(self._counters.items()):
                    if n == name:
                        out.append(f"{n}{_labels(dict(lab))} {v:g}")
            for name in sorted({k[0] for k in self._hist}):
                out += [f"# HELP {name} {HELP.get(name, name)}", f"# TYPE {name} histogram"]
                for (n, lab), h in sorted(self._hist.items()):
                    if n != name:
                        continue
                    base = dict(lab)
                    for i, b in enumerate(BUCKETS):
                        out.append(f"{n}_bucket{_labels({**base, 'le': str(b)})} {h[i]:g}")
                    out.append(f"{n}_bucket{_labels({**base, 'le': '+Inf'})} {h[len(BUCKETS)]:g}")
                    out.append(f"{n}_sum{_labels(base)} {h[len(BUCKETS) + 1]:.6f}")
                    out.append(f"{n}_count{_labels(base)} {h[len(BUCKETS)]:g}")
        return "\n".join(out) + "\n"
