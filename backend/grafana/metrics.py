"""Prometheus exposition for Grafana to scrape.

Kept dependency-free — the format is three lines of text per series and adding
a client library for it would be more code than writing it.
"""
from __future__ import annotations

import threading


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._help: dict[str, tuple[str, str]] = {}

    def describe(self, name: str, kind: str, help_text: str) -> None:
        self._help[name] = (kind, help_text)

    def gauge(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._gauges[(name, tuple(sorted(labels.items())))] = float(value)

    def inc(self, name: str, amount: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + amount

    def clear_gauges(self, prefix: str) -> None:
        with self._lock:
            for k in [k for k in self._gauges if k[0].startswith(prefix)]:
                del self._gauges[k]

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            merged = [("gauge", self._gauges), ("counter", self._counters)]
            seen: set[str] = set()
            for default_kind, store in merged:
                for (name, labels), value in sorted(store.items()):
                    if name not in seen:
                        kind, help_text = self._help.get(name, (default_kind, name))
                        lines.append(f"# HELP {name} {help_text}")
                        lines.append(f"# TYPE {name} {kind}")
                        seen.add(name)
                    if labels:
                        rendered = ",".join(f'{k}="{_esc(v)}"' for k, v in labels)
                        lines.append(f"{name}{{{rendered}}} {value}")
                    else:
                        lines.append(f"{name} {value}")
        return "\n".join(lines) + "\n"


metrics = Metrics()

metrics.describe("gallery_battle_tension", "gauge",
                 "Battle score for a contested track position (0-1)")
metrics.describe("gallery_battle_gap_seconds", "gauge",
                 "Gap between the two cars contesting a position")
metrics.describe("gallery_on_air_score", "gauge",
                 "Battle score of the shot currently on the world feed")
metrics.describe("gallery_cuts_total", "counter",
                 "World feed cuts committed, by decision tier")
metrics.describe("gallery_director_latency_ms", "gauge",
                 "Latency of the most recent director decision")
metrics.describe("gallery_pairings_scored_total", "counter",
                 "Adjacent pairings scored by the tension agent")
metrics.describe("gallery_race_lap", "gauge", "Current lap of the replayed race")
