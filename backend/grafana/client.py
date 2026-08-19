"""Grafana integration — the partner service, driven from code at runtime.

Three things happen against a live Grafana instance:

  1. `ensure_dashboard()`   provisions the race dashboard via the HTTP API
  2. `annotate_cut()`       writes every director decision onto the race
                            timeline as a Grafana annotation
  3. `ensure_alert_rule()`  installs a "battle imminent" alert so the agent's
                            own signal can page a human

and one thing happens the other way:

  4. `recent_cuts()`        reads annotations back out of Grafana, so the
                            agent can query its own broadcast history

With no token configured the client records the exact calls it would have made
and exposes them on /api/grafana — the pipeline stays honest about what did and
did not reach Grafana instead of pretending.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger("gallery.grafana")

DASHBOARD = Path(__file__).with_name("dashboard.json")

ALERT_TITLE = "Battle imminent — cut recommended"
FOLDER_TITLE = "The Gallery"


def _alert_rule(folder_uid: str, ds_uid: str) -> dict:
    """A Grafana unified-alerting rule: query Prometheus, threshold the result.

    Provisioning rejects a rule without a folder and without a `data` pipeline,
    so both are resolved from the live instance rather than hardcoded.
    """
    return {
        "title": ALERT_TITLE,
        "ruleGroup": "the-gallery",
        "folderUID": folder_uid,
        "orgID": 1,
        "condition": "C",
        "for": "10s",
        "noDataState": "OK",
        "execErrState": "Error",
        "labels": {"source": "the-gallery", "severity": "info"},
        "annotations": {
            "summary": "A contested position has crossed the cut threshold.",
            "description": "gallery_battle_tension exceeded 0.55 — the world "
                           "feed director should be on this battle.",
        },
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 300, "to": 0},
                "datasourceUid": ds_uid,
                "model": {
                    "refId": "A",
                    "instant": True,
                    "expr": "max(gallery_battle_tension)",
                },
            },
            {
                "refId": "C",
                "relativeTimeRange": {"from": 300, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "C",
                    "type": "threshold",
                    "expression": "A",
                    "conditions": [
                        {"evaluator": {"type": "gt", "params": [0.55]}}
                    ],
                },
            },
        ],
    }


class GrafanaClient:
    def __init__(self) -> None:
        self.url = settings.grafana_url
        self.token = settings.grafana_token
        self.enabled = settings.grafana_ready
        self.calls: list[dict[str, Any]] = []
        self.dashboard_url: str | None = None
        self.last_error: str | None = None
        self._client = httpx.AsyncClient(
            base_url=self.url,
            timeout=6.0,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"} if self.token else {},
        )

    # ------------------------------------------------------------------ core
    def _record(self, method: str, path: str, body: Any, sent: bool,
                result: str = "") -> None:
        self.calls.append({
            "method": method, "path": path, "sent": sent, "result": result,
            "body": body if isinstance(body, (str, int, float)) else "…",
        })
        del self.calls[:-40]

    async def _request(self, method: str, path: str, body: Any = None,
                       quiet: bool = False) -> dict | None:
        if not self.enabled:
            self._record(method, path, body, sent=False, result="no GRAFANA_TOKEN")
            return None
        try:
            r = await self._client.request(method, path, json=body)
            ok = r.status_code < 300
            self._record(method, path, body, sent=True, result=f"{r.status_code}")
            if not ok:
                msg = f"{method} {path} → {r.status_code} {r.text[:160]}"
                if quiet:
                    log.debug(msg)
                else:
                    self.last_error = msg
                    log.warning(msg)
                return None
            return r.json() if r.content else {}
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{method} {path} → {exc}"
            self._record(method, path, body, sent=True, result=str(exc)[:80])
            log.warning("grafana %s", self.last_error)
            return None

    async def health(self) -> bool:
        if not self.enabled:
            return False
        return await self._request("GET", "/api/health") is not None

    # ------------------------------------------------------------- provisioning
    async def ensure_dashboard(self) -> str | None:
        try:
            spec = json.loads(DASHBOARD.read_text())
        except Exception as exc:  # noqa: BLE001
            log.warning("dashboard spec unreadable: %s", exc)
            return None

        # Panels pushed through the API render empty unless every panel and
        # every target names a datasource by UID — which is only knowable at
        # runtime, so it is stamped in here rather than committed to the spec.
        ds_uid = await self._prometheus_uid()
        if ds_uid:
            ref = {"type": "prometheus", "uid": ds_uid}
            for panel in spec.get("panels", []):
                panel["datasource"] = ref
                for target in panel.get("targets", []):
                    target["datasource"] = ref
        else:
            log.warning("no prometheus datasource — dashboard will render empty")

        res = await self._request("POST", "/api/dashboards/db", {
            "dashboard": spec, "overwrite": True,
            "message": "provisioned by The Gallery",
        })
        if res and res.get("url"):
            self.dashboard_url = f"{self.url}{res['url']}"
            log.info("grafana dashboard: %s", self.dashboard_url)
        return self.dashboard_url

    async def _folder_uid(self) -> str | None:
        existing = await self._request("GET", "/api/folders")
        if isinstance(existing, list):
            for f in existing:
                if f.get("title") == FOLDER_TITLE:
                    return f.get("uid")
        made = await self._request("POST", "/api/folders", {"title": FOLDER_TITLE})
        return made.get("uid") if made else None

    async def _prometheus_uid(self) -> str | None:
        # Probe the conventional names first. A miss is normal — Grafana Cloud
        # names its own `grafanacloud-<stack>-prom` — so these are looked up
        # quietly rather than logged as failures.
        for name in ("Prometheus", "prometheus"):
            ds = await self._request("GET", f"/api/datasources/name/{name}", quiet=True)
            if ds and ds.get("uid"):
                return ds["uid"]

        all_ds = await self._request("GET", "/api/datasources")
        if not isinstance(all_ds, list):
            return None
        proms = [d for d in all_ds if d.get("type") == "prometheus"]
        if not proms:
            return None
        # A Grafana Cloud stack ships several prometheus datasources, and one of
        # them — grafanacloud-usage — holds billing telemetry, not yours. Taking
        # the first match provisions a dashboard that queries the wrong store
        # and renders empty with no error anywhere.
        for d in proms:
            name = (d.get("name") or "").lower()
            if "usage" not in name and "cardinality" not in name:
                return d.get("uid")
        return proms[0].get("uid")

    async def ensure_alert_rule(self) -> bool:
        """Install the battle-imminent alert. Idempotent."""
        rules = await self._request("GET", "/api/v1/provisioning/alert-rules")
        if isinstance(rules, list) and any(r.get("title") == ALERT_TITLE for r in rules):
            log.info("grafana alert rule already installed")
            return True

        folder_uid = await self._folder_uid()
        ds_uid = await self._prometheus_uid()
        if not folder_uid or not ds_uid:
            log.warning("alert rule skipped — folder=%s datasource=%s", folder_uid, ds_uid)
            return False

        res = await self._request(
            "POST", "/api/v1/provisioning/alert-rules", _alert_rule(folder_uid, ds_uid)
        )
        if res is not None:
            log.info("grafana alert rule installed")
            return True
        return False

    # ------------------------------------------------------------- annotations
    async def annotate_cut(self, *, time_ms: int, driver: str, code: str,
                           line: str, tier: str, score: float) -> None:
        await self._request("POST", "/api/annotations", {
            "time": time_ms,
            "tags": ["gallery", "cut", f"tier:{tier}", f"car:{driver}"],
            "text": f"CUT → car {driver} ({code}) · score {score:.2f} · {line}",
        })

    async def recent_cuts(self, limit: int = 20) -> list[dict]:
        res = await self._request("GET", f"/api/annotations?tags=cut&limit={limit}")
        return res if isinstance(res, list) else []

    async def aclose(self) -> None:
        await self._client.aclose()


grafana = GrafanaClient()
