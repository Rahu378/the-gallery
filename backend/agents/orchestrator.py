"""The broadcast loop.

  tick (10 Hz)
    ├─ advance the race source
    ├─ TensionScorer   → score all adjacent pairings          [deterministic]
    ├─ push metrics    → Prometheus → Grafana
    ├─ CutGuard        → is a change permitted right now?      [deterministic]
    ├─ DirectorAgent   → which story, and why                  [Gemini / ADK]
    └─ on commit       → Grafana annotation on the race timeline

The director runs off the hot path: it is dispatched as a task and applied when
it resolves, so a slow model call never stalls the feed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from ..config import settings
from ..data.source import Frame, build_source, catalogue
from ..grafana.client import grafana
from ..grafana.metrics import metrics
from ..grafana.remote_write import RemoteWriter
from .cutguard import CutGuard
from .director import DirectorAgent, Decision
from .tension import Battle, TensionScorer

log = logging.getLogger("gallery.orchestrator")


@dataclass
class LogEntry:
    t: float
    kind: str        # cut | hold | release | alert | system
    text: str
    tier: str = ""


@dataclass
class State:
    lap: int = 0
    total_laps: int = 0
    race_t: float = 0.0
    circuit: str = ""
    source: str = ""
    cars: list = field(default_factory=list)
    battles: list = field(default_factory=list)
    on_air: dict | None = None
    top_score: float = 0.0
    log: list = field(default_factory=list)
    director_tier: str = "heuristic"
    director_latency: int = 0
    grafana_live: bool = False
    pairings_scored: int = 0


class Orchestrator:
    def __init__(self) -> None:
        self.source = build_source()
        self.scorer = TensionScorer()
        self.guard = CutGuard(settings.min_hold_s, settings.max_hold_s)
        self.director = DirectorAgent()
        self.state = State(
            circuit=self.source.meta.name,
            source=self.source.meta.source,
            total_laps=self.source.meta.total_laps,
            director_tier=self.director.tier,
        )
        self.outline = self.source.meta.outline
        self._log: list[LogEntry] = []
        self._pending: asyncio.Task | None = None
        self._last_call = 0.0
        self._cut_count = 0
        self._subs: set[asyncio.Queue] = set()
        self.writer = RemoteWriter(
            settings.prom_push_url, settings.prom_user, settings.prom_token,
            extra_labels={"job": "the-gallery", "circuit": self.source.meta.name},
        )
        self._push_batch: list[tuple[str, dict, float]] = []
        self._running = False
        self.paused = False
        self.speed = settings.replay_speed
        self.race_id = None
        self._restart = False
        self.circuit_rev = 0
        self.audit_count = 0

    # ------------------------------------------------------------------ log
    def _emit(self, kind: str, text: str, tier: str = "") -> None:
        self._log.insert(0, LogEntry(self.state.race_t, kind, text, tier))
        del self._log[24:]

    # ------------------------------------------------------------ subscribe
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _publish(self, payload: dict) -> None:
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    # --------------------------------------------------------------- metrics
    def _push_metrics(self, frame: Frame, battles: list[Battle]) -> None:
        metrics.clear_gauges("gallery_battle_")
        for b in battles[:8]:
            labels = {"position": str(b.position), "ahead": b.ahead, "behind": b.behind}
            metrics.gauge("gallery_battle_tension", b.score, **labels)
            metrics.gauge("gallery_battle_gap_seconds", b.gap, **labels)
        metrics.gauge("gallery_race_lap", frame.lap)
        metrics.gauge("gallery_on_air_score", self.guard.current_score)

        batch: list[tuple[str, dict, float]] = [
            ("gallery_race_lap", {}, float(frame.lap)),
            ("gallery_on_air_score", {}, float(self.guard.current_score)),
            ("gallery_director_latency_ms", {}, float(self.state.director_latency)),
            ("gallery_cuts_total", {"tier": self.state.director_tier}, float(self._cut_count)),
        ]
        for b in battles[:8]:
            lb = {"position": str(b.position), "ahead": b.ahead, "behind": b.behind}
            batch.append(("gallery_battle_tension", lb, b.score))
            batch.append(("gallery_battle_gap_seconds", lb, b.gap))
        self._push_batch = batch
        metrics.inc("gallery_pairings_scored_total", max(0, len(frame.cars) - 1))
        self.state.pairings_scored += max(0, len(frame.cars) - 1)

    # ------------------------------------------------------------- transport
    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self._emit("system", "replay paused" if paused else "replay resumed")

    def set_speed(self, speed: float) -> None:
        self.speed = max(0.5, min(40.0, float(speed)))
        self._emit("system", f"replay speed {self.speed:g}x")

    async def switch_race(self, race_id: str) -> bool:
        """Load a different circuit without dropping connected viewers.

        Building the source is CPU-bound for a second or two off a warm cache,
        so it runs in a worker thread — doing it inline stalls the event loop
        and every open WebSocket stutters.
        """
        ids = [r["id"] for r in catalogue()]
        if race_id not in ids:
            return False
        self._emit("system", f"loading {race_id}…")
        was_paused = self.paused
        self.paused = True
        try:
            src = await asyncio.to_thread(build_source, race_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("race switch failed: %s", exc)
            self._emit("system", f"could not load {race_id}")
            self.paused = was_paused
            return False

        self.source = src
        self.outline = src.meta.outline
        self.race_id = race_id
        self.scorer = TensionScorer()
        self.guard.current = None
        self.state.on_air = None
        self.state.circuit = src.meta.name
        self.state.source = src.meta.source
        self.state.total_laps = src.meta.total_laps
        self._restart = True
        # Clients hold the outline from connect time. Bumping this tells them to
        # refetch geometry rather than shipping 900 points on every tick.
        self.circuit_rev += 1
        self.paused = was_paused
        self._emit("system", f"circuit: {src.meta.name}")
        log.info("switched to %s", src.meta.name)
        return True

    def seek_lap(self, lap: int) -> bool:
        """Jump the replay to the start of a lap."""
        seek = getattr(self.source, "seek_lap", None)
        if seek is None:
            return False
        ok = seek(int(lap))
        if ok:
            # Gap history describes the old position on track; carrying it over
            # makes the scorer read a jump as twenty cars converging at once.
            self.scorer = TensionScorer()
            self.guard.current = None
            self.state.on_air = None
            self._emit("system", f"jumped to lap {lap}")
        return ok

    # ------------------------------------------------------------ the loop
    async def run(self) -> None:
        self._running = True
        dt = 1.0 / settings.tick_hz

        metrics.describe("gallery_cuts_total", "counter", "World feed cuts, by tier")
        self._emit("system", f"source: {self.source.meta.source} · {self.source.meta.name}")
        self._emit("system", f"director: {self.director.tier} · {settings.director_model}")

        asyncio.create_task(self._provision_grafana())
        asyncio.create_task(self.director.warmup())
        asyncio.create_task(self._audit_loop())
        asyncio.create_task(self._push_loop())

        while self._running:
            await self._replay(dt)
            if not self._running:
                break
            if self._restart:
                self._restart = False
                continue
            # A real race is finite. Left alone the loop returns here, the feed
            # goes silent and anyone opening the page later sees a frozen
            # screen — so rewind to lights-out and run it again. Gap history is
            # dropped with it, or closing rates would be computed across the
            # seam between the last lap and the first.
            reset = getattr(self.source, "reset", None)
            if reset is None:
                break
            reset()
            self.scorer = TensionScorer()
            self.guard.current = None
            self.state.on_air = None
            self._emit("system", "race complete — replaying from lights-out")
            log.info("replay exhausted, looping")

    async def _replay(self, dt: float) -> None:
        # Speed is read through a callable so the transport can change it
        # mid-race without restarting the generator.
        source = self.source
        for frame in source.frames(lambda: dt * self.speed):
            if not self._running or self._restart or source is not self.source:
                return
            # Hold here while paused rather than skipping frames, so resuming
            # continues from the same moment instead of jumping ahead.
            while self.paused and self._running:
                self._publish(self.snapshot())
                await asyncio.sleep(0.2)
            started = time.perf_counter()

            battles = self.scorer.score_frame(frame)
            self._push_metrics(frame, battles)

            self.state.lap = frame.lap
            self.state.race_t = frame.t
            self.state.total_laps = frame.total_laps or self.state.total_laps
            self.state.cars = [
                {"num": c.num, "code": c.code, "team": c.team, "color": c.color,
                 "x": round(c.x, 5), "y": round(c.y, 5), "z": round(c.z, 5),
                 "pos": c.pos,
                 "gap": round(c.gap_ahead, 3), "closing": round(c.closing, 4),
                 "tyre": c.tyre, "age": c.tyre_age,
                 "speed": round(c.speed, 1),
                 # Gap to the car behind, so a driver card can show both sides
                 # of the sandwich without the client re-deriving race order.
                 "gap_behind": round(frame.cars[i + 1].gap_ahead, 3)
                 if i + 1 < len(frame.cars) else 0.0}
                for i, c in enumerate(frame.cars)
            ]
            self.state.battles = [b.as_dict() for b in battles[:8]]
            self.state.top_score = battles[0].score if battles else 0.0

            if self.guard.current:
                self.director.note_airtime(self.guard.current, dt * self.speed)

            await self._maybe_direct(frame, battles)
            self._publish(self.snapshot())

            spent = time.perf_counter() - started
            await asyncio.sleep(max(0.0, dt - spent))

    async def _maybe_direct(self, frame: Frame, battles: list[Battle]) -> None:
        candidate = next((b for b in battles if b.score >= settings.battle_threshold), None)

        # Track the score of whatever is on air so the guard can compare.
        if self.guard.current:
            live = next((b for b in battles if b.ahead_num == self.guard.current), None)
            self.guard.current_score = live.score if live else 0.0
            if live is None and self.state.on_air and self.state.on_air.get("hot"):
                self.state.on_air["hot"] = False
                self._emit("release", f"battle resolved — car {self.guard.current} released")

        verdict = self.guard.evaluate(frame.t, candidate, self.guard.current_score)
        if not verdict.allowed or candidate is None:
            return
        if self._pending and not self._pending.done():
            return
        if time.monotonic() - self._last_call < settings.director_cooldown_s:
            return

        self._last_call = time.monotonic()
        self._pending = asyncio.create_task(
            self._direct(battles, frame, candidate, verdict.reason)
        )

    async def _direct(self, battles: list[Battle], frame: Frame,
                      candidate: Battle, reason: str) -> None:
        try:
            decision: Decision = await self.director.decide(
                battles, frame.lap, frame.total_laps, self.guard.current
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("director failed: %s", exc)
            return

        chosen = next((b for b in battles if b.ahead_num == decision.cut_to), candidate)
        if decision.cut_to != chosen.ahead_num:
            log.debug("director named car %s, not a candidate — using %s",
                      decision.cut_to, chosen.ahead_num)

        # The guard cleared a change, but the director can still land on the car
        # already on air — the battle it left may have re-formed. That is a hold
        # with a fresh commentary line, not a cut: refresh the shot, don't
        # re-count it and don't put a second annotation on the timeline.
        if chosen.ahead_num == self.guard.current and self.state.on_air:
            self.guard.current_score = chosen.score
            self.state.on_air.update({
                "line": decision.line, "gap": chosen.gap,
                "score": chosen.score, "against": chosen.behind, "hot": True,
            })
            self._emit("hold", f"holding car {chosen.ahead_num} — {decision.line}")
            return

        self.guard.commit(chosen.ahead_num, frame.t, chosen.score)
        self.state.director_tier = decision.tier
        self.state.director_latency = decision.latency_ms
        metrics.inc("gallery_cuts_total", tier=decision.tier)
        self._cut_count += 1
        metrics.gauge("gallery_director_latency_ms", decision.latency_ms)

        self.state.on_air = {
            "num": chosen.ahead_num, "code": chosen.ahead,
            "against": chosen.behind, "against_num": chosen.behind_num,
            "shot": decision.shot, "line": decision.line,
            "score": chosen.score, "gap": chosen.gap,
            "position": chosen.position, "confidence": decision.confidence,
            "tier": decision.tier, "hot": True,
        }
        self._emit("cut",
                   f"cut → car {chosen.ahead_num} ({chosen.ahead}) · P{chosen.position} "
                   f"· {chosen.score:.2f} — {decision.line}",
                   decision.tier)
        log.info("CUT %s (%s) tier=%s %dms — %s",
                 chosen.ahead_num, chosen.ahead, decision.tier,
                 decision.latency_ms, decision.line)

        await grafana.annotate_cut(
            time_ms=int(time.time() * 1000), driver=chosen.ahead_num,
            code=chosen.ahead, line=decision.line, tier=decision.tier,
            score=chosen.score,
        )

    async def _push_loop(self) -> None:
        """Ship the current metric snapshot to Grafana Cloud every 15 s.

        Scrape-shaped rather than event-shaped on purpose: sending the latest
        value of everything on a fixed interval keeps the series continuous,
        where pushing only on change leaves gaps the graphs render as breaks.
        """
        if not self.writer.enabled:
            self._emit("system", "metrics: no remote_write credentials — local /metrics only")
            return
        self._emit("system", "metrics: pushing to grafana cloud every 15s")
        while self._running:
            await asyncio.sleep(15)
            batch = list(self._push_batch)
            if batch:
                await self.writer.push(batch)

    async def _audit_loop(self) -> None:
        """Read back through MCP what this director actually put on air.

        Runs on its own timer rather than inside a decision, so the partner
        integration is exercised at runtime regardless of whether the model
        elects to call a tool, and without adding latency to a cut.
        """
        from .grafana_mcp import available, broadcast_audit
        if not available():
            self._emit("system", "grafana mcp: server not available")
            return
        self._emit("system", "grafana mcp: broadcast audit every 120s")
        while self._running:
            await asyncio.sleep(120)
            res = await broadcast_audit()
            if res.get("ok"):
                self.audit_count += 1
                self._emit("system", f"grafana mcp: read back {self.audit_count} audits")
            else:
                log.debug("audit: %s", res.get("reason"))

    # ---------------------------------------------------------------- grafana
    async def _provision_grafana(self) -> None:
        ok = await grafana.health()
        self.state.grafana_live = ok
        if not ok:
            self._emit("system", "grafana: offline — calls recorded, see /api/grafana")
            return
        url = await grafana.ensure_dashboard()
        await grafana.ensure_alert_rule()
        self._emit("system", f"grafana: dashboard provisioned · alert rule installed")
        if url:
            log.info("dashboard %s", url)

    # --------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        s = self.state
        return {
            "lap": s.lap, "total_laps": s.total_laps, "race_t": round(s.race_t, 1),
            "circuit": s.circuit, "source": s.source,
            "cars": s.cars, "battles": s.battles,
            "on_air": s.on_air, "top_score": round(s.top_score, 3),
            "director": {"tier": s.director_tier, "latency_ms": s.director_latency,
                         "model": settings.director_model,
                         "blocked": self.director.block_reason,
                         "configured": settings.gemini_ready,
                         "mcp": self.director.mcp_ready,
                         },
            "grafana": {"live": s.grafana_live, "url": grafana.dashboard_url,
                        "enabled": grafana.enabled,
                        "pushed": self.writer.sent,
                        "push_error": self.writer.last_error},
            "pairings_scored": s.pairings_scored,
            "transport": {"paused": self.paused, "speed": self.speed,
                          "race_id": self.race_id},
            "races": catalogue(),
            "circuit_rev": self.circuit_rev,
            "hold": round(max(0.0, s.race_t - self.guard.since), 1) if self.guard.current else 0.0,
            "log": [{"t": round(e.t, 1), "kind": e.kind, "text": e.text, "tier": e.tier}
                    for e in self._log],
        }

    def stop(self) -> None:
        self._running = False
