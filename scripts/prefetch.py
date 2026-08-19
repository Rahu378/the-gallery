"""Warm the FastF1 cache at image build time.

A cold container has no cache, so without this every start re-downloads a full
race — slow, and a hard runtime dependency on a third-party API being up. Worse
on Cloud Build, whose egress cannot reach the F1 timing API at all: position
data comes back empty there, the build still succeeds, and the container
quietly serves the synthetic race instead. So the cache is warmed locally and
shipped in the image; this script only fills gaps.

Failure is deliberately non-fatal for the same reason.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "cache"

# Races baked into the image, chosen for contrast: Monza is flat-out with two
# DRS zones and a slipstream train, Spa is the longest lap on the calendar,
# Silverstone is fast and flowing, Barcelona is a conventional permanent
# circuit that makes the others look as distinctive as they are.
#
# Full event names on purpose. Passing "Spa" to FastF1 fuzzy-matches to the
# *Spanish* Grand Prix, which loads happily and is simply the wrong circuit.
RACES = [
    (2023, "Italian Grand Prix", "R"),
    (2023, "Belgian Grand Prix", "R"),
    (2023, "British Grand Prix", "R"),
    (2023, "Spanish Grand Prix", "R"),
]


def load_one(fastf1, year: int, event: str, session_id: str) -> bool:
    started = time.time()
    print(f"prefetch: {year} {event} {session_id} …", flush=True)
    try:
        ses = fastf1.get_session(year, event, session_id)
        ses.load(laps=True, telemetry=True, weather=False, messages=False)
    except Exception as exc:  # noqa: BLE001
        print(f"prefetch: FAILED {event} ({exc})", file=sys.stderr)
        return False
    print(f"prefetch: cached {ses.event['EventName']} in {time.time()-started:.0f}s", flush=True)
    return True


def main() -> int:
    try:
        import fastf1
    except ImportError:
        print("prefetch: fastf1 not installed, skipping", file=sys.stderr)
        return 0

    CACHE.mkdir(exist_ok=True)
    fastf1.Cache.enable_cache(str(CACHE))

    only = os.getenv("RACE_EVENT")
    races = [r for r in RACES if r[1] == only] or RACES if only else RACES

    ok = sum(load_one(fastf1, *r) for r in races)
    size = sum(f.stat().st_size for f in CACHE.rglob("*") if f.is_file())
    print(f"prefetch: {ok}/{len(races)} races cached, {size/1e6:.0f} MB", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
