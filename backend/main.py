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
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
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
    return {"outline": orch.outline, "name": orch.source.meta.name,
            "source": orch.source.meta.source}


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


@app.websocket("/ws")
async def ws(sock: WebSocket) -> None:
    await sock.accept()
    if not orch:
        await sock.close()
        return
    await sock.send_json({"type": "circuit", "outline": orch.outline,
                          "name": orch.source.meta.name})
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
async def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
