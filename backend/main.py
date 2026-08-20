"""The Gallery — server.

  GET  /                websocket-driven broadcast UI
  WS   /ws              live race + agent state, ~10 Hz
  GET  /metrics         Prometheus exposition, scraped by Grafana
  GET  /api/state       one-shot snapshot
  GET  /api/grafana     what has actually been sent to Grafana
  GET  /api/health      readiness of each integration
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi import Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .agents.orchestrator import Orchestrator
from .config import settings
from .grafana.client import grafana
from .grafana.metrics import metrics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-22s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gallery")

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


def _asset_version() -> str:
    """Short hash of the frontend bundle, used to bust caches on deploy.

    no-cache headers still leave a window where a proxy or an already-open tab
    keeps yesterday's stylesheet against today's markup, which renders as every
    view stacked on top of each other. Versioning the URLs makes a stale asset
    unreachable rather than merely discouraged.
    """
    h = hashlib.sha1()
    for name in sorted(("index.html", "style.css", "app.js")):
        f = FRONTEND / name
        if f.exists():
            h.update(f.read_bytes())
    return h.hexdigest()[:10]


ASSET_V = _asset_version()

app = FastAPI(title="The Gallery", version="1.0.0")
orch: Orchestrator | None = None
_task: asyncio.Task | None = None


@app.on_event("startup")
async def _startup() -> None:
    global orch, _task
    log.info("─" * 62)
    log.info("The Gallery — agentic world feed direction")
    log.info("gemini: %s · grafana: %s",
             "ready" if settings.gemini_ready else "no credentials",
             settings.grafana_url if settings.grafana_ready else "no token")
    orch = Orchestrator()
    _task = asyncio.create_task(orch.run())
    log.info("→ http://localhost:8042")
    log.info("─" * 62)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if orch:
        orch.stop()
    if _task:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
    await grafana.aclose()


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus() -> str:
    return metrics.render()


@app.get("/api/state")
async def state() -> dict:
    return orch.snapshot() if orch else {"error": "starting"}


@app.get("/api/circuit")
async def circuit() -> dict:
    if not orch:
        return {"outline": []}
    meta = orch.source.meta
    return {"outline": orch.outline, "name": meta.name, "source": meta.source,
            "corners": meta.corners, "drs_zones": meta.drs_zones,
            "bounds": meta.bounds, "elevation_m": meta.elevation_m}


@app.get("/api/grafana")
async def grafana_calls() -> dict:
    return {
        "enabled": grafana.enabled,
        "url": grafana.url,
        "dashboard": grafana.dashboard_url,
        "last_error": grafana.last_error,
        "calls": grafana.calls[-20:],
    }


@app.get("/api/health")
async def health() -> dict:
    return {
        "gemini": {"configured": settings.gemini_ready,
                   "model": settings.director_model,
                   "tier": orch.director.tier if orch else "-"},
        "grafana": {"configured": grafana.enabled, "url": grafana.url,
                    "dashboard": grafana.dashboard_url},
        "race": {"source": orch.source.meta.source if orch else "-",
                 "circuit": orch.source.meta.name if orch else "-"},
    }


@app.post("/api/control/{action}")
async def control(action: str, value: float = 0) -> dict:
    """Transport for the replay. The race loops continuously by design so a
    visitor always lands on live action, but a demo needs to be able to stop it."""
    if not orch:
        return {"ok": False, "error": "starting"}
    if action == "pause":
        orch.set_paused(True)
    elif action == "resume":
        orch.set_paused(False)
    elif action == "speed":
        orch.set_speed(value)
    elif action == "race":
        return {"ok": False, "error": "use /api/control/race/{id}"}
    elif action == "seek":
        if not orch.seek_lap(int(value)):
            return {"ok": False, "error": "lap out of range"}
    else:
        return {"ok": False, "error": f"unknown action {action}"}
    return {"ok": True, "paused": orch.paused, "speed": orch.speed}


@app.post("/api/control/race/{race_id}")
async def switch_race(race_id: str) -> dict:
    if not orch:
        return {"ok": False, "error": "starting"}
    ok = await orch.switch_race(race_id)
    return {"ok": ok, "circuit": orch.source.meta.name}


@app.get("/api/commentary")
async def commentary_audio(line: str = ""):
    """Speak a commentary line. Only ever called when a client enables audio."""
    from .agents.commentary import commentary
    wav = await commentary.speak(line)
    if not wav:
        return PlainTextResponse(
            commentary.last_error or "no audio", status_code=503)
    return Response(content=wav, media_type="audio/wav",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.websocket("/ws")
async def ws(sock: WebSocket) -> None:
    await sock.accept()
    if not orch:
        await sock.close()
        return
    await sock.send_json({
        "type": "circuit", "outline": orch.outline,
        "name": orch.source.meta.name,
        "corners": orch.source.meta.corners,
        "drs_zones": orch.source.meta.drs_zones,
        "bounds": orch.source.meta.bounds,
        "elevation_m": orch.source.meta.elevation_m,
    })
    q = orch.subscribe()
    try:
        while True:
            payload = await q.get()
            await sock.send_json({"type": "state", **payload})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        orch.unsubscribe(q)


@app.get("/")
async def index() -> HTMLResponse:
    html = (FRONTEND / "index.html").read_text()
    html = html.replace("/static/style.css", f"/static/style.css?v={ASSET_V}")
    html = html.replace("/static/app.js", f"/static/app.js?v={ASSET_V}")
    html = html.replace("__GRAFANA_PUBLIC__", settings.grafana_public_url or "")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})


class RevalidatingStatic(StaticFiles):
    """Serve assets with must-revalidate.

    Without an explicit Cache-Control the browser applies heuristic caching, so
    a viewer who saw an earlier revision keeps the old CSS and JS after a
    deploy — the page renders with stale layout against fresh data and looks
    broken. ETags still make the revalidation cheap.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


app.mount("/static", RevalidatingStatic(directory=FRONTEND), name="static")
