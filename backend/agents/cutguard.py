"""Cut guard — the constraint layer between the director and the output feed.

A director that chases the highest score every tick produces unwatchable
television. This enforces the grammar a human director works to: hold a shot
long enough to read, don't abandon a developing story, and force a change when
a shot has gone stale.
"""
from __future__ import annotations

from dataclasses import dataclass

from .tension import Battle


@dataclass
class CutDecision:
    allowed: bool
    reason: str
    forced: bool = False


class CutGuard:
    def __init__(self, min_hold: float = 4.0, max_hold: float = 25.0,
                 switch_margin: float = 0.12) -> None:
        self.min_hold = min_hold
        self.max_hold = max_hold
        self.switch_margin = switch_margin
        self.current: str | None = None      # driver number currently on air
        self.since: float = 0.0
        self.current_score: float = 0.0

    def evaluate(self, now: float, candidate: Battle | None,
                 current_score: float) -> CutDecision:
        held = now - self.since

        if self.current is None:
            return CutDecision(True, "no shot on air")

        if held >= self.max_hold and candidate is not None:
            return CutDecision(True, f"shot stale after {held:.0f}s", forced=True)

        if held < self.min_hold:
            return CutDecision(False, f"min hold {held:.1f}/{self.min_hold:.0f}s")

        if candidate is None:
            return CutDecision(False, "no candidate above threshold")

        if candidate.ahead_num == self.current:
            return CutDecision(False, "already on this battle")

        # Only leave a live story for a meaningfully better one.
        if candidate.score < current_score + self.switch_margin:
            return CutDecision(
                False,
                f"candidate {candidate.score:.2f} < on-air {current_score:.2f} + margin",
            )

        return CutDecision(True, f"better story ({candidate.score:.2f} vs {current_score:.2f})")

    def commit(self, driver_num: str, now: float, score: float) -> None:
        self.current = driver_num
        self.since = now
        self.current_score = score
