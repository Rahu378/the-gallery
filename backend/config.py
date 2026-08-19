"""Runtime configuration for The Gallery."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CACHE_DIR = ROOT / "cache"
CACHE_DIR.mkdir(exist_ok=True)


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Gemini
    api_key: str = os.getenv("GOOGLE_API_KEY", "")
    use_vertex: bool = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "0") == "1"
    gcp_project: str = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    gcp_location: str = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    director_model: str = os.getenv("DIRECTOR_MODEL", "gemini-flash-lite-latest")

    # Grafana
    grafana_url: str = os.getenv("GRAFANA_URL", "http://localhost:3000").rstrip("/")
    grafana_token: str = os.getenv("GRAFANA_TOKEN", "")
    # Prometheus remote_write, so the app can push its own metrics to Grafana
    # Cloud instead of relying on something nearby to scrape it.
    prom_push_url: str = os.getenv("GRAFANA_PROM_URL", "")
    prom_user: str = os.getenv("GRAFANA_PROM_USER", "")
    prom_token: str = os.getenv("GRAFANA_PROM_TOKEN", "")
    # Public (no-login) dashboard share link, surfaced in the nav.
    grafana_public_url: str = os.getenv("GRAFANA_PUBLIC_URL", "")

    # Replay
    race_year: int = int(os.getenv("RACE_YEAR", "2023"))
    race_event: str = os.getenv("RACE_EVENT", "Monza")
    race_session: str = os.getenv("RACE_SESSION", "R")
    replay_speed: float = _f("REPLAY_SPEED", 8.0)

    # Director tuning
    tick_hz: float = _f("TICK_HZ", 10.0)
    min_hold_s: float = _f("MIN_HOLD_S", 4.0)
    max_hold_s: float = _f("MAX_HOLD_S", 25.0)
    battle_threshold: float = _f("BATTLE_THRESHOLD", 0.55)
    # Wall-clock seconds between director calls. The Gemini free tier allows 5
    # generate_content requests per minute, so 13 s keeps us just under it
    # (~4.6/min). On a paid tier drop this to 3 for a much livelier feed.
    director_cooldown_s: float = _f("DIRECTOR_COOLDOWN_S", 13.0)
    # Hard deadline on a director call. Past this the overtake has
    # happened and a deterministic cut now beats a good cut late.
    director_timeout_s: float = _f("DIRECTOR_TIMEOUT_S", 4.0)

    @property
    def gemini_ready(self) -> bool:
        return bool(self.api_key) or (self.use_vertex and bool(self.gcp_project))

    @property
    def grafana_ready(self) -> bool:
        return bool(self.grafana_token)

    @property
    def prom_push_ready(self) -> bool:
        return bool(self.prom_push_url and self.prom_user and self.prom_token)


settings = Settings()
