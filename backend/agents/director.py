"""The director agent.

Built on the Google Agent Development Kit (ADK) and Gemini. It is asked one
question the numbers cannot answer: of these candidate battles, which is the
story worth putting on air right now, and how do we justify it on commentary.

It is deliberately *not* in the 10 Hz loop. The tension scorer and the cut
guard gate it, so Gemini is consulted only at genuine decision points — a few
times a minute, which is also what a human director does.

Three execution tiers, so the project runs for anyone who clones it:
  1. ADK  `LlmAgent` + `Runner`  (primary — the hackathon requirement)
  2. google-genai direct call    (if ADK is unavailable)
  3. deterministic top-score     (if there are no credentials at all)
The tier actually used is reported on every decision and shown in the UI.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

from ..config import settings
from .tension import Battle

log = logging.getLogger("gallery.director")

INSTRUCTION = """You are the world feed director for a Formula 1 broadcast.

One camera goes out to a global audience. Your job is to be on the right car at
the right moment — a pass that happens off screen is lost forever.

You will be given ranked candidate battles. Each has a numeric score already
computed from gap, closing rate, DRS availability and tyre delta. Do not
recompute it. Your judgement is for what the number cannot capture:

- A fight for the lead outranks a marginally closer fight for P14.
- A move that is actually about to happen beats one that is merely close.
- Coverage should be spread. Call `recent_airtime` before deciding, and favour
  a story that has not been shown if the scores are comparable.
- Cutting away from a developing move to something slightly better is bad
  television. When in doubt, stay.

Reply with JSON only, no prose and no code fence:
{"cut_to": "<car number of the DEFENDING car>", "shot": "ONBOARD" | "TRACKSIDE" | "HELICOPTER", "line": "<one broadcast sentence, max 14 words>", "confidence": 0.0-1.0}
"""


@dataclass
class Decision:
    cut_to: str
    shot: str
    line: str
    confidence: float
    tier: str            # "adk" | "genai" | "heuristic"
    latency_ms: int


def _retry_after(exc: Exception) -> float:
    """Seconds to wait, read out of a 429 rather than guessed.

    The Gemini free tier permits 5 generate_content calls per minute. When the
    limit is hit the error carries both `retryDelay: '45s'` and a prose "Please
    retry in 45.5s" — either is a better backoff than an arbitrary constant.
    """
    text = str(exc)
    for pattern in (r"retryDelay['\"]?\s*:\s*['\"](\d+(?:\.\d+)?)s",
                    r"retry in (\d+(?:\.\d+)?)s"):
        m = re.search(pattern, text)
        if m:
            return min(120.0, float(m.group(1)) + 1.0)
    return 30.0


def _is_quota(exc: Exception) -> bool:
    t = str(exc)
    return "RESOURCE_EXHAUSTED" in t or "429" in t


class DirectorAgent:
    def __init__(self) -> None:
        self.model = settings.director_model
        self.tier = "heuristic"
        self._airtime: dict[str, float] = {}
        self._agent = None
        self._runner = None
        self._genai = None
        self._session_ready = False
        self.blocked_until = 0.0     # monotonic; set by a 429
        self.block_reason = ""

        if not settings.gemini_ready:
            log.warning("no Gemini credentials — director runs on the deterministic tier")
            return

        self._try_adk()
        if self._agent is None:
            self._try_genai()

    # ------------------------------------------------------------------ setup
    def _try_adk(self) -> None:
        try:
            from google.adk.agents import LlmAgent
            from google.adk.runners import Runner
            from google.adk.sessions import InMemorySessionService

            def recent_airtime() -> dict:
                """Seconds of airtime each car has received in this session.

                Call this before choosing, so coverage is spread across the field
                rather than concentrated on the leaders.
                """
                return {k: round(v, 1) for k, v in sorted(
                    self._airtime.items(), key=lambda kv: -kv[1])[:10]}

            self._agent = LlmAgent(
                name="world_feed_director",
                model=self.model,
                instruction=INSTRUCTION,
                tools=[recent_airtime],
            )
            self._sessions = InMemorySessionService()
            self._runner = Runner(
                agent=self._agent, app_name="gallery", session_service=self._sessions
            )
            self.tier = "adk"
            log.info("director: ADK LlmAgent on %s", self.model)
        except Exception as exc:  # noqa: BLE001
            log.warning("ADK unavailable (%s)", exc)
            self._agent = None

    def _try_genai(self) -> None:
        try:
            from google import genai

            self._genai = (
                genai.Client(vertexai=True, project=settings.gcp_project,
                             location=settings.gcp_location)
                if settings.use_vertex else genai.Client(api_key=settings.api_key)
            )
            self.tier = "genai"
            log.info("director: google-genai direct on %s", self.model)
        except Exception as exc:  # noqa: BLE001
            log.warning("google-genai unavailable (%s)", exc)
            self._genai = None

    # ------------------------------------------------------------------ helpers
    def note_airtime(self, driver_num: str, seconds: float) -> None:
        self._airtime[driver_num] = self._airtime.get(driver_num, 0.0) + seconds

    @staticmethod
    def _prompt(candidates: list[Battle], lap: int, total: int, on_air: str | None) -> str:
        lines = [f"Lap {lap} of {total}. Currently on air: car {on_air or 'none'}.", "",
                 "Candidate battles, highest score first:"]
        for i, b in enumerate(candidates[:5], 1):
            lines.append(
                f"{i}. P{b.position}: car {b.ahead_num} ({b.ahead}) defending from "
                f"car {b.behind_num} ({b.behind}) — score {b.score:.2f}, {b.why}"
            )
        lines.append("")
        lines.append("Which do we cut to?")
        return "\n".join(lines)

    @staticmethod
    def _parse(text: str, fallback: Battle) -> tuple[str, str, str, float] | None:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        cut = str(d.get("cut_to", "") or "").strip().lstrip("#")
        if not cut:
            return None
        shot = str(d.get("shot", "ONBOARD")).upper()
        if shot not in {"ONBOARD", "TRACKSIDE", "HELICOPTER"}:
            shot = "ONBOARD"
        line = str(d.get("line", "") or f"{fallback.behind} closing on {fallback.ahead}.")
        try:
            conf = float(d.get("confidence", 0.7))
        except (TypeError, ValueError):
            conf = 0.7
        return cut, shot, line[:120], max(0.0, min(1.0, conf))

    # ------------------------------------------------------------------ decide
    async def decide(self, candidates: list[Battle], lap: int, total: int,
                     on_air: str | None) -> Decision:
        started = time.perf_counter()
        top = candidates[0]

        def elapsed() -> int:
            return int((time.perf_counter() - started) * 1000)

        def heuristic(reason_tier: str = "heuristic") -> Decision:
            drs = " with DRS" if top.drs else ""
            return Decision(
                cut_to=top.ahead_num, shot="ONBOARD",
                line=f"{top.behind} is {top.gap:.1f}s behind {top.ahead}{drs}.",
                confidence=min(0.95, 0.5 + top.score / 2), tier=reason_tier,
                latency_ms=elapsed(),
            )

        prompt = self._prompt(candidates, lap, total, on_air)

        # Under a quota block, go straight to deterministic. Firing a request we
        # know will 429 costs ~1 s of latency and buys nothing.
        if time.monotonic() < self.blocked_until:
            return heuristic()

        def note_quota(exc: Exception) -> None:
            wait = _retry_after(exc)
            self.blocked_until = time.monotonic() + wait
            self.block_reason = f"quota — retrying in {wait:.0f}s"
            log.warning("director rate-limited, backing off %.0fs", wait)

        # ---- tier 1: ADK ----
        if self._runner is not None:
            try:
                from google.genai import types

                if not self._session_ready:
                    await self._ensure_session()
                content = types.Content(role="user", parts=[types.Part(text=prompt)])
                text = ""
                async for ev in self._runner.run_async(
                    user_id="gallery", session_id="race", new_message=content
                ):
                    if getattr(ev, "content", None) and ev.content.parts:
                        for p in ev.content.parts:
                            if getattr(p, "text", None):
                                text += p.text
                parsed = self._parse(text, top)
                if parsed:
                    self.block_reason = ""
                    cut, shot, line, conf = parsed
                    return Decision(cut, shot, line, conf, "adk", elapsed())
                log.debug("ADK returned unparseable output: %r", text[:200])
            except Exception as exc:  # noqa: BLE001
                if _is_quota(exc):
                    note_quota(exc)
                    return heuristic()
                log.warning("ADK decide failed: %s", str(exc).replace("\n", " ")[:220])

        # ---- tier 2: genai direct ----
        if self._genai is not None:
            try:
                resp = await self._genai.aio.models.generate_content(
                    model=self.model,
                    contents=f"{INSTRUCTION}\n\n{prompt}",
                )
                parsed = self._parse(resp.text or "", top)
                if parsed:
                    self.block_reason = ""
                    cut, shot, line, conf = parsed
                    return Decision(cut, shot, line, conf, "genai", elapsed())
            except Exception as exc:  # noqa: BLE001
                if _is_quota(exc):
                    note_quota(exc)
                    return heuristic()
                log.warning("genai decide failed: %s", str(exc).replace("\n", " ")[:220])

        # ---- tier 3: deterministic ----
        return heuristic()

    async def _ensure_session(self) -> None:
        try:
            res = self._sessions.create_session(
                app_name="gallery", user_id="gallery", session_id="race"
            )
            if hasattr(res, "__await__"):
                await res
        except Exception as exc:  # noqa: BLE001
            log.debug("session create: %s", exc)
        self._session_ready = True
