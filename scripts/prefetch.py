"""Warm the FastF1 cache at image build time.

A cold Cloud Run container has no cache, so without this every cold start
re-downloads a full race from the F1 timing API — 15-30 seconds of startup,
and a hard runtime dependency on a third-party service being up and not
rate-limiting during judging. Baking the race into the image removes both.

Failure here is deliberately non-fatal: the image still builds, and the app
falls back to its synthetic race source at runtime and says so in the header.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "cache"


def main() -> int:
    year = int(os.getenv("RACE_YEAR", "2023"))
    event = os.getenv("RACE_EVENT", "Monza")
    session_id = os.getenv("RACE_SESSION", "R")

    try:
        import fastf1
    except ImportError:
        print("prefetch: fastf1 not installed, skipping", file=sys.stderr)
        return 0

    CACHE.mkdir(exist_ok=True)
    fastf1.Cache.enable_cache(str(CACHE))

    started = time.time()
    print(f"prefetch: loading {year} {event} {session_id} …", flush=True)
    try:
        ses = fastf1.get_session(year, event, session_id)
        ses.load(laps=True, telemetry=True, weather=False, messages=False)
    except Exception as exc:  # noqa: BLE001
        print(f"prefetch: FAILED ({exc}) — image will fall back to the "
              f"synthetic source at runtime", file=sys.stderr)
        return 0

    size = sum(f.stat().st_size for f in CACHE.rglob("*") if f.is_file())
    print(f"prefetch: cached {ses.event['EventName']} "
          f"({size / 1e6:.0f} MB) in {time.time() - started:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
