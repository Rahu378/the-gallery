"""Does the director actually catch the moments?

The point of this project is that a pass which happens off screen is lost. So
the measure that matters is not latency or uptime — it is what fraction of the
overtakes in a race the feed was actually watching when they happened.

This replays a full Grand Prix under several cut policies and counts. No model
calls: the policies compared here are the deterministic ones, because they are
what decides *where the camera is*. The Gemini director chooses which of the
candidate battles is the better story; the candidate set, and therefore the
capture rate, comes from the tension scorer and the cut guard. Measuring those
in isolation keeps the number reproducible and free.

Usage:  .venv/bin/python -m eval.capture_rate [--race monza] [--policies ...]
"""
from __future__ import annotations

import argparse
import random
from dataclasses import dataclass

from backend.agents.cutguard import CutGuard
from backend.agents.tension import TensionScorer
from backend.data.source import build_source


@dataclass
class Result:
    policy: str
    overtakes: int
    captured: int
    cuts: int

    @property
    def rate(self) -> float:
        return self.captured / self.overtakes if self.overtakes else 0.0


def collect_overtakes(race: str, speed: float = 40.0, tick_hz: float = 10.0) -> list[tuple]:
    """Every position change in the race, as (tick, carA, carB).

    Used to compute the ceiling: with one camera and a minimum hold, some
    overtakes are simply not catchable — two passes seconds apart at opposite
    ends of the field cannot both be shown. Reporting a raw percentage without
    that context understates the result.
    """
    src = build_source(race)
    dt = 1.0 / tick_hz
    out, last = [], {}
    for i, frame in enumerate(src.frames(dt * speed)):
        now = {c.num: c.pos for c in frame.cars}
        for num, pos in now.items():
            was = last.get(num)
            if was is None or pos >= was:
                continue
            lost = [n for n, p in now.items() if last.get(n) == pos and p == was]
            if lost:
                out.append((i, frame.t, num, lost[0]))
        last = now
    return out


def oracle_ceiling(events: list[tuple], min_hold: float = 4.0) -> tuple[int, int]:
    """Most overtakes any single feed could have caught, knowing the future.

    Greedy over time: take an overtake, then skip anything inside the minimum
    hold that does not involve the same cars. This is an upper bound no real
    director can reach, since it requires foreknowledge.
    """
    caught = 0
    last_t = -1e9
    held: set[str] = set()
    for _, t, a, b in events:
        if t - last_t >= min_hold:
            caught += 1
            last_t = t
            held = {a, b}
        elif a in held or b in held:
            caught += 1
    return caught, len(events)


def run(race: str, policy: str, speed: float = 40.0, tick_hz: float = 10.0) -> Result:
    src = build_source(race)
    scorer = TensionScorer()
    guard = CutGuard(min_hold=4.0, max_hold=25.0)
    rng = random.Random(11)

    dt = 1.0 / tick_hz
    race_dt = dt * speed
    last_pos: dict[str, int] = {}
    overtakes = captured = cuts = 0
    on_air: str | None = None

    for frame in src.frames(race_dt):
        battles = scorer.score_frame(frame)

        # --- did a position change hands, and were we watching it? ---
        now = {c.num: c.pos for c in frame.cars}
        for num, pos in now.items():
            was = last_pos.get(num)
            if was is None or pos >= was:
                continue
            lost = [n for n, p in now.items()
                    if last_pos.get(n) == pos and p == was]
            if not lost:
                continue
            overtakes += 1
            if on_air in (num, lost[0]):
                captured += 1
        last_pos = now

        # --- choose a shot ---
        if policy == "leader":
            # What a lazy broadcast does: stay on the front of the race.
            want = frame.cars[0].num
        elif policy == "random":
            want = rng.choice(frame.cars).num
        elif policy == "tension":
            cand = next((b for b in battles if b.score >= 0.55), None)
            live = next((b for b in battles if b.ahead_num == guard.current), None)
            guard.current_score = live.score if live else 0.0
            verdict = guard.evaluate(frame.t, cand, guard.current_score)
            want = cand.ahead_num if (verdict.allowed and cand) else guard.current
        else:
            raise SystemExit(f"unknown policy {policy}")

        if want and want != on_air:
            # Leader and random still respect a minimum hold; without it they
            # would flicker every tick and the comparison would be unfair.
            if policy == "tension" or (frame.t - guard.since) >= 4.0:
                on_air = want
                guard.commit(want, frame.t, 0.0)
                cuts += 1

    return Result(policy, overtakes, captured, cuts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--race", default="monza")
    ap.add_argument("--policies", nargs="+",
                    default=["tension", "leader", "random"])
    args = ap.parse_args()

    events = collect_overtakes(args.race)
    ceil_n, total = oracle_ceiling(events)
    ceiling = ceil_n / total if total else 0.0

    print(f"\n  {args.race} — full race, one pass per policy")
    print(f"  {total} position changes · one camera · 4s minimum hold\n")
    print(f"  {'policy':10s} {'captured':>9s} {'rate':>7s} {'of ceiling':>11s} {'cuts':>6s}")
    print("  " + "-" * 50)
    for pol in args.policies:
        r = run(args.race, pol)
        share = r.rate / ceiling if ceiling else 0.0
        print(f"  {r.policy:10s} {r.captured:>9d} {r.rate*100:>6.1f}% "
              f"{share*100:>10.1f}% {r.cuts:>6d}")
    print(f"  {'oracle':10s} {ceil_n:>9d} {ceiling*100:>6.1f}% {100.0:>10.1f}%       —")
    print("\n  oracle knows every pass in advance; no live director can reach it.\n")


if __name__ == "__main__":
    main()
