"""Grafana's MCP server, wired in as agent tools.

Everything else in this project talks to Grafana one way: the app pushes
dashboards, annotations, alert rules and metrics over the HTTP API. This is the
other direction — the agent itself queries Grafana, through Grafana's own MCP
server (`mcp-grafana`) over stdio, exposed to Gemini as ADK tools.

That matters beyond neatness. The director's own decisions are already written
to Grafana as annotations, and its tension scores are already in Prometheus, so
MCP lets it read back what it has actually been doing rather than trusting a
local variable. The broadcast audit below does exactly that on a timer, so the
integration is exercised at runtime whether or not the model elects to call a
tool during a given decision.

Only read tools are exposed. The server offers 73, including ones that create
datasources and rewrite alert routing; a broadcast director has no business
holding those.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from ..config import settings

log = logging.getLogger("gallery.mcp")

# Read-only, and small on purpose. Every tool exposed lands in the prompt and
# costs tokens and latency on a path that was just optimised down to one round
# trip per decision.
DIRECTOR_TOOLS = ["get_annotations", "query_prometheus", "get_dashboard_summary"]

_CANDIDATES = [
    os.getenv("MCP_GRAFANA_BIN", ""),
    "/usr/local/bin/mcp-grafana",
    str(Path(__file__).resolve().parent.parent.parent / "bin" / "mcp-grafana"),
    "/tmp/mcpg/mcp-grafana",
]


def binary_path() -> str | None:
    for c in _CANDIDATES:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return c
    found = shutil.which("mcp-grafana")
    return found


def available() -> bool:
    return bool(binary_path()) and settings.grafana_ready


def build_toolset(tool_filter: list[str] | None = None):
    """An ADK toolset backed by mcp-grafana, or None if it cannot be built."""
    if not available():
        log.info("grafana MCP unavailable (binary=%s, token=%s)",
                 bool(binary_path()), settings.grafana_ready)
        return None
    try:
        from google.adk.tools import McpToolset
        from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
        from mcp import StdioServerParameters

        ts = McpToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(
                    command=binary_path(),
                    args=["-t", "stdio"],
                    env={
                        "GRAFANA_URL": settings.grafana_url,
                        # GRAFANA_API_KEY still works but the server warns it is
                        # deprecated on every single call.
                        "GRAFANA_SERVICE_ACCOUNT_TOKEN": settings.grafana_token,
                    },
                ),
                timeout=25,
            ),
            tool_filter=tool_filter if tool_filter is not None else DIRECTOR_TOOLS,
        )
        log.info("grafana MCP toolset built (%s)", binary_path())
        return ts
    except Exception as exc:  # noqa: BLE001 — MCP is an enhancement, never fatal
        log.warning("grafana MCP toolset failed: %s", exc)
        return None


async def broadcast_audit(limit: int = 20) -> dict:
    """Ask Grafana, over MCP, what this director has actually put on air.

    Reads the annotations the app wrote and summarises coverage. Deliberately
    goes through MCP rather than the HTTP client already in this codebase —
    the point is that the agent's view of its own history comes back through
    the partner's own protocol.
    """
    ts = build_toolset(tool_filter=["get_annotations"])
    if ts is None:
        return {"ok": False, "reason": "mcp unavailable"}
    try:
        tools = await ts.get_tools()
        tool = next((t for t in tools if t.name == "get_annotations"), None)
        if tool is None:
            return {"ok": False, "reason": "get_annotations not exposed"}
        res = await tool.run_async(
            args={"tags": ["gallery", "cut"], "limit": limit}, tool_context=None
        )
        items = res if isinstance(res, list) else getattr(res, "content", res)
        return {"ok": True, "raw": items}
    except Exception as exc:  # noqa: BLE001
        log.warning("broadcast audit failed: %s", exc)
        return {"ok": False, "reason": str(exc)[:160]}
    finally:
        try:
            await ts.close()
        except Exception:  # noqa: BLE001
            pass
