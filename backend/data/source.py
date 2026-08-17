"""Race sources.

`FastF1Source` replays a real Grand Prix from official timing + position
telemetry. `SyntheticSource` is a deterministic fallback so the pipeline runs
with no network and no FastF1 cache — the agent stack is identical either way.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterator

import numpy as np

from ..config import CACHE_DIR, settings
from .geometry import Centerline, synthetic_circuit, unwrap_progress

log = logging.getLogger("gallery.data")

# Fallback identity colours when a source has none (kept distinct, not team-accurate).
_PALETTE = [
    "#00E676", "#A24BFF", "#FFD93D", "#E8112D", "#3AA6FF",
    "#FF7A3D", "#00D5C8", "#FF5CA8", "#9AE66E", "#C9A227",
]


@dataclass
class Car:
    num: str
    code: str
    team: str
    color: str
    x: float = 0.0
    y: float = 0.0
    progress: float = 0.0
    pos: int = 0
    gap_ahead: float = 0.0      # seconds to the car in front
    closing: float = 0.0        # seconds/second — positive means catching
    tyre: str = "MEDIUM"
    tyre_age: int = 0
    speed: float = 0.0


@dataclass
class Frame:
    t: float
    lap: int
    cars: list[Car]
    total_laps: int = 0


@dataclass
class RaceMeta:
    name: str = "Synthetic Circuit"
    year: int = 0
    session: str = "R"
    outline: list = field(default_factory=list)
    total_laps: int = 0
    source: str = "synthetic"


class SyntheticSource:
    """Deterministic 20-car race with organic convergence and overtakes."""

    def __init__(self, seed: int = 7, total_laps: int = 53):
        self.rng = np.random.default_rng(seed)
        self.cl = Centerline(synthetic_circuit())
        self.total_laps = total_laps
        self.meta = RaceMeta(
            name="Synthetic Circuit",
            outline=self.cl.outline(),
            total_laps=total_laps,
            source="synthetic",
        )
        nums = ["1", "11", "16", "55", "44", "63", "4", "81", "14", "18",
                "10", "31", "23", "22", "77", "24", "20", "27", "2", "3"]
        codes = ["VER", "PER", "LEC", "SAI", "HAM", "RUS", "NOR", "PIA", "ALO", "STR",
                 "GAS", "OCO", "ALB", "TSU", "BOT", "ZHO", "MAG", "HUL", "SAR", "RIC"]
        teams = ["Alpha", "Alpha", "Rossa", "Rossa", "Silver", "Silver", "Papaya", "Papaya",
                 "Verde", "Verde", "Bleu", "Bleu", "Navy", "Navy", "Cinza", "Cinza",
                 "Aco", "Aco", "Navy", "Bleu"]

        self.cars: list[Car] = []
        self.base_pace: list[float] = []
        lap_time = 82.0
        prog = 0.0
        for i in range(20):
            prog -= self.rng.uniform(0.55, 2.6) / lap_time  # realistic starting gaps
            c = Car(
                num=nums[i], code=codes[i], team=teams[i],
                color=_PALETTE[i % len(_PALETTE)],
                progress=prog,
                tyre=["SOFT", "MEDIUM", "HARD"][i % 3],
                tyre_age=int(self.rng.integers(4, 20)),
            )
            self.cars.append(c)
            # Slight pace spread — this is what makes battles form on their own.
            self.base_pace.append(lap_time + self.rng.normal(0.0, 0.45))
        self.phase = self.rng.uniform(0, 6.28, 20)
        self.t = 0.0

    def frames(self, dt: float) -> Iterator[Frame]:
        L = self.cl.length
        while True:
            self.t += dt
            for i, c in enumerate(self.cars):
                deg = 0.010 * max(0, c.tyre_age + self.t / 90.0 - 12)
                lap_time = self.base_pace[i] + deg + 0.35 * np.sin(self.t / 26.0 + self.phase[i])
                c.progress += dt / lap_time
                c.speed = float(L / lap_time * 3.6 * (0.72 + 0.28 * np.sin(c.progress * 6.283 * 3)))
            yield self._assemble()

    def _assemble(self) -> Frame:
        order = sorted(self.cars, key=lambda c: -c.progress)
        lap = max(1, int(order[0].progress) + 1) if order[0].progress > 0 else 1
        for i, c in enumerate(order):
            c.pos = i + 1
            sx, sy = self.cl.point_at((c.progress % 1.0) * self.cl.length)
            c.x, c.y = sx, sy
            if i == 0:
                c.gap_ahead = 0.0
            else:
                ahead = order[i - 1]
                pace = self.base_pace[self.cars.index(c)]
                c.gap_ahead = max(0.0, (ahead.progress - c.progress) * pace)
        return Frame(t=self.t, lap=min(lap, self.total_laps),
                     cars=list(order), total_laps=self.total_laps)


class FastF1Source:
    """Replays a real Grand Prix from official position + timing telemetry."""

    def __init__(self, year: int, event: str, session_id: str = "R", step: float = 0.5):
        import fastf1

        fastf1.Cache.enable_cache(str(CACHE_DIR))
        log.info("loading %s %s %s from FastF1…", year, event, session_id)
        ses = fastf1.get_session(year, event, session_id)
        ses.load(laps=True, telemetry=True, weather=False, messages=False)
        self.step = step

        pos = ses.pos_data
        results = ses.results
        drivers = [d for d in pos.keys() if d in set(results["DriverNumber"].astype(str))]
        if len(drivers) < 6:
            raise RuntimeError("insufficient position telemetry")

        # --- circuit centerline ---
        # This has to be exactly one clean lap. A raw slice of position data
        # spans in-laps, out-laps and the pit lane, which produces a centerline
        # that doubles back on itself — and since race order and every gap are
        # derived from arc-length along this line, a bad centerline corrupts
        # the entire pipeline. The fastest lap's telemetry is one clean circuit.
        self.cl = self._build_centerline(ses, pos, drivers)

        # --- common time grid ---
        starts, ends = [], []
        for d in drivers:
            df = pos[d]
            starts.append(df["SessionTime"].iloc[0].total_seconds())
            ends.append(df["SessionTime"].iloc[-1].total_seconds())
        t0, t1 = max(starts), min(ends)

        laps = ses.laps
        # Session telemetry begins well before the race does. Everything up to
        # lights-out is the grid and the formation lap — the whole field sits at
        # the same track position, every gap reads 0.00s and the tension scorer
        # sees twenty simultaneous photo finishes. Start at lights-out instead.
        try:
            race_start = float(laps["LapStartTime"].min().total_seconds())
            if t0 < race_start < t1:
                t0 = race_start
        except Exception:  # noqa: BLE001
            pass

        self.grid = np.arange(t0, t1, step)

        med = laps["LapTime"].dt.total_seconds().median()
        self.median_lap = float(med) if med == med else 90.0

        self.cars: list[Car] = []
        self.prog: dict[str, np.ndarray] = {}
        for i, d in enumerate(drivers):
            df = pos[d]
            ts = df["SessionTime"].dt.total_seconds().to_numpy()
            gx = np.interp(self.grid, ts, df["X"].to_numpy())
            gy = np.interp(self.grid, ts, df["Y"].to_numpy())
            s = self.cl.project(gx, gy)
            self.prog[d] = unwrap_progress(s, self.cl.length)

            row = results[results["DriverNumber"].astype(str) == d]
            code = str(row["Abbreviation"].iloc[0]) if len(row) else d
            team = str(row["TeamName"].iloc[0]) if len(row) else "—"
            tc = str(row["TeamColor"].iloc[0]) if len(row) else ""
            color = f"#{tc}" if tc and not tc.startswith("#") else (tc or _PALETTE[i % len(_PALETTE)])
            self.cars.append(Car(num=d, code=code, team=team, color=color))

        # --- tyre state per driver per lap ---
        self.tyres: dict[str, list[tuple[float, str, int]]] = {}
        for d in drivers:
            dl = laps[laps["DriverNumber"].astype(str) == d]
            seq = []
            for _, r in dl.iterrows():
                st = r["LapStartTime"]
                if st != st:
                    continue
                comp = str(r.get("Compound", "") or "UNKNOWN")
                age = r.get("TyreLife", 0)
                seq.append((st.total_seconds(), comp, int(age) if age == age else 0))
            self.tyres[d] = seq

        total_laps = int(laps["LapNumber"].max()) if len(laps) else 0
        self.meta = RaceMeta(
            name=str(ses.event["EventName"]),
            year=year,
            session=session_id,
            outline=self.cl.outline(),
            total_laps=total_laps,
            source="fastf1",
        )
        self.i = 0
        log.info("loaded %s — %d drivers, %d frames", self.meta.name, len(self.cars), len(self.grid))

    @staticmethod
    def _build_centerline(ses, pos, drivers) -> Centerline:
        """One clean lap of circuit geometry."""
        try:
            fastest = ses.laps.pick_fastest()
            if fastest is not None:
                tel = fastest.get_pos_data()
                xy = tel[["X", "Y"]].to_numpy()
                if len(xy) > 120:
                    log.info("centerline from fastest lap (%d samples)", len(xy))
                    return Centerline(xy)
        except Exception as exc:  # noqa: BLE001
            log.warning("fastest-lap centerline failed (%s)", exc)

        # Fallback: one mid-race lap of a driver who ran the full distance.
        for d in sorted(drivers, key=lambda k: -len(pos[k])):
            try:
                dl = ses.laps[ses.laps["DriverNumber"].astype(str) == d]
                mid = dl.iloc[len(dl) // 2]
                xy = mid.get_pos_data()[["X", "Y"]].to_numpy()
                if len(xy) > 120:
                    log.info("centerline from car %s mid-race lap", d)
                    return Centerline(xy)
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError("could not build a circuit centerline")

    def _tyre_at(self, d: str, t: float) -> tuple[str, int]:
        seq = self.tyres.get(d, [])
        cur = ("MEDIUM", 0)
        for st, comp, age in seq:
            if st <= t:
                cur = (comp, age)
            else:
                break
        return cur

    def reset(self) -> None:
        """Rewind to lights-out so the replay can loop."""
        self.i = 0

    def frames(self, dt: float) -> Iterator[Frame]:
        stride = max(1, int(round(dt / self.step)))
        while self.i < len(self.grid):
            t = float(self.grid[self.i])
            back = max(0, self.i - stride)
            span = max(1e-6, (self.i - back) * self.step)
            for c in self.cars:
                p = float(self.prog[c.num][self.i])
                # Speed straight off the progress derivative — used to tell a
                # racing car from one parked in the pits or on the grid.
                c.speed = float(
                    (p - float(self.prog[c.num][back])) / span * self.cl.length * 3.6
                )
                c.progress = p
                sx, sy = self.cl.point_at((p % 1.0) * self.cl.length)
                c.x, c.y = sx, sy
                c.tyre, c.tyre_age = self._tyre_at(c.num, t)
            order = sorted(self.cars, key=lambda c: -c.progress)
            for k, c in enumerate(order):
                c.pos = k + 1
                c.gap_ahead = 0.0 if k == 0 else max(
                    0.0, (order[k - 1].progress - c.progress) * self.median_lap
                )
            lap = max(1, int(order[0].progress) + 1)
            yield Frame(t=t, lap=min(lap, self.meta.total_laps or lap),
                        cars=list(order), total_laps=self.meta.total_laps)
            self.i += stride


def build_source():
    """Real race if we can get it, deterministic synthetic if we can't."""
    try:
        src = FastF1Source(settings.race_year, settings.race_event, settings.race_session)
        log.info("source: FastF1 — %s", src.meta.name)
        return src
    except Exception as exc:  # noqa: BLE001 — any failure must degrade, not crash
        log.warning("FastF1 unavailable (%s); using synthetic source", exc)
        return SyntheticSource()
