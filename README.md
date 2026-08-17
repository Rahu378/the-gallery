# The Gallery

**Agentic world feed direction for Formula 1.**

Formula 1 puts a camera on every car and a hundred more around the circuit, and
sends *one* of them to a global audience. The most persistent complaint in the
sport is that the broadcast misses the fight — the director holds on the leader
in clean air while a move for P8 starts and finishes off screen. You cannot get
it back. The overtake does not happen again.

The Gallery watches all twenty cars at once, scores every contested position for
how close it is to resolving, and cuts to the battle **before** it happens.

Built for the Agentic Cinema hackathon — **Grafana track**.

---

## What it does

```
Real F1 telemetry ─► Tension scorer ─► Director ─► Cut guard ─► World feed
     FastF1           deterministic    Gemini/ADK   deterministic
        │                   │              │             │
        └───────────────────┴──────────────┴─────────────┘
                            │
              Grafana — metrics, annotations, alert rules
```

The split is deliberate. Scoring 19 pairings ten times a second is a numeric
problem: it must be stable, cheap and explainable, so it is plain Python with no
model in the loop. Gemini is asked only the question the numbers cannot answer —
*which of these stories is worth the cut, and how do we justify it on air.* The
cut guard then applies the grammar a human director works to, so the output is
watchable rather than a slideshow of whatever scored highest this tick.

| Agent | Kind | Job |
|---|---|---|
| **Tension scorer** | deterministic, 10 Hz | Scores every adjacent pairing on gap, closing rate, DRS range and tyre delta |
| **Director** | Gemini via ADK | Picks the story, writes the commentary line, calls `recent_airtime` to spread coverage |
| **Cut guard** | deterministic | Minimum hold, staleness forcing, and a margin before abandoning a live story |

---

## Google Cloud + partner integration

Both are imported and called at runtime, not named in a readme.

**Gemini / Agent Development Kit** — [`backend/agents/director.py`](backend/agents/director.py)
`LlmAgent` with a registered tool, driven by `Runner` over `InMemorySessionService`.
Three execution tiers so the project runs for anyone who clones it: ADK (primary),
`google-genai` direct (if ADK is unavailable), deterministic top-score (no
credentials). **The tier actually used is reported on every decision, shown in
the UI, and labelled on each log line** — the project never presents a
deterministic cut as a model decision.

**Grafana** — [`backend/grafana/client.py`](backend/grafana/client.py)
Four live interactions over the HTTP API:

1. `ensure_dashboard()` provisions the race dashboard, resolving and stamping the
   Prometheus datasource UID into every panel at runtime
2. `annotate_cut()` writes each director decision onto the race timeline as an
   annotation
3. `ensure_alert_rule()` installs a *battle imminent* threshold rule against
   `max(gallery_battle_tension)`
4. `recent_cuts()` reads annotations back out, so the agent can query its own
   broadcast history

Metrics are exposed at `/metrics` in Prometheus format and scraped into Grafana.

With no `GRAFANA_TOKEN` the client records the exact calls it would have made and
exposes them at `/api/grafana` rather than pretending they were sent.

---

## Running it

```bash
git clone <this repo> && cd gallery
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # add GOOGLE_API_KEY for the Gemini tier
```

Start Grafana and Prometheus:

```bash
docker compose up -d
```

Create a Grafana service account token (Administration → Service accounts, or
the API) and put it in `.env` as `GRAFANA_TOKEN`. Then:

```bash
.venv/bin/uvicorn backend.main:app --port 8042
```

- Broadcast UI → <http://localhost:8042>
- Grafana dashboard → <http://localhost:3000/d/the-gallery>
- Metrics → <http://localhost:8042/metrics>

First start downloads the race from FastF1 and caches it (~30 s). Without
network or without FastF1 the app falls back to a deterministic synthetic race
and says so in the header — the agent stack is identical either way.

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `GOOGLE_API_KEY` | — | Gemini. Without it the director runs deterministic |
| `GOOGLE_GENAI_USE_VERTEXAI` | `0` | Set `1` to use Vertex AI with ADC |
| `DIRECTOR_MODEL` | `gemini-flash-latest` | |
| `GRAFANA_URL` / `GRAFANA_TOKEN` | `localhost:3000` | |
| `RACE_YEAR` / `RACE_EVENT` | `2023` / `Monza` | Any race FastF1 can fetch |
| `REPLAY_SPEED` | `8.0` | Race seconds per wall second |
| `BATTLE_THRESHOLD` | `0.55` | Score a pairing must clear to be a candidate |
| `MIN_HOLD_S` / `MAX_HOLD_S` | `4` / `25` | Cut guard, in race seconds |

---

## Deploying to Cloud Run

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
./scripts/deploy-cloudrun.sh
```

The script enables the required APIs, stores `GOOGLE_API_KEY` and
`GRAFANA_TOKEN` in Secret Manager, grants the runtime service account access,
and deploys. Three flags are load-bearing:

| Flag | Why |
|---|---|
| `--min-instances 1` | The race loop must keep running between requests |
| `--no-cpu-throttling` | Cloud Run parks CPU outside request handling by default, which freezes the 10 Hz orchestrator the moment a request ends |
| `--timeout 3600` | Lets the live WebSocket stay open |

The image bakes the race in at build time (`scripts/prefetch.py`), so a cold
container starts in ~9 s and has no runtime dependency on the F1 timing API
being up or ungated. Build is ~1.2 GB, of which 106 MB is cached telemetry.

### Grafana Cloud

Grafana Cloud **cannot scrape a local process** — there is no route from their
network to your machine. The dashboard and annotations work over the HTTP API
as soon as `GRAFANA_URL` and `GRAFANA_TOKEN` point at your stack, but metrics
have to be pushed: uncomment the `remote_write` block in
[`scripts/prometheus.yml`](scripts/prometheus.yml) so the local Prometheus
scrapes and forwards to Grafana Cloud's hosted Prometheus.

## Notes on the data

Race order and every gap derive from arc-length along a circuit centerline built
from one clean lap of position telemetry. Two details matter and both were bugs
first:

- **The centerline must be exactly one lap.** A raw slice of position data spans
  in-laps, out-laps and the pit lane, producing a line that doubles back on
  itself and corrupts every downstream gap.
- **Replay starts at lights-out, not session start.** Session telemetry begins on
  the grid, where the whole field sits at the same track position, every gap
  reads 0.00 s, and the scorer sees twenty simultaneous photo finishes.

Gaps are computed as progress difference × median lap time. That is the standard
approximation and is accurate to roughly a tenth in racing conditions; it is not
a substitute for official timing.

Tuning was measured, not guessed. Closing rate across a full replay sits at p50
0.004 s/s and p99 0.018 s/s, which is why the momentum term normalises by 0.015 —
an earlier value of 0.28 left that term contributing effectively zero.

---

## What is verified

All of the following was exercised against a live stack, not asserted:

- FastF1 loads the 2023 Italian Grand Prix — 20 drivers, 9,718 replay frames
- Race order matches history: Sainz leading from pole with Verstappen 0.097 s
  behind, Verstappen taking the lead around lap 15
- Prometheus target healthy, 8 tension series flowing with per-pairing labels
- Grafana dashboard provisioned via API, panels bound to the datasource and
  rendering real series (`P14 — BOT v ZHO`); alert rule installed
  (`201 Created`); annotations written per cut (`200`)
- **Director running live on Gemini via ADK — 9 of 9 cuts on the `adk` tier,
  zero fallbacks**, ~1.3–1.9 s per decision

The commentary is genuinely contextual rather than templated. Unedited output:

> Verstappen is right on the gearbox of leader Carlos Sainz!
> Norris under immense pressure from Hamilton in the fight for eighth.
> Incredible wheel-to-wheel action between the two McLaren teammates!

The last one is the tell — nothing in the prompt says Piastri and Norris drive
for the same team.

### Choosing a model — this will bite you

`gemini-flash-latest` resolves to the newest flash model, which on the free tier
carries a **daily** cap of 20 `generate_content` requests. That is roughly two
minutes of operation, after which every decision silently falls back to the
deterministic tier.

Two things make this worse than it first looks. Function calling means one
director decision costs **two to three** model requests, not one — the agent
calls `recent_airtime`, receives it, then answers. And the 429 advertises a
`retryDelay` of ~45 s even when the exhausted quota is daily, so naive retry
logic waits and fails forever.

The default is therefore `gemini-flash-lite-latest`, which has enough headroom to
run continuously, plus `DIRECTOR_COOLDOWN_S=13` to stay inside the per-minute
limit. On a paid tier, drop the cooldown to 3 for a much livelier feed.
Quota errors are parsed for their real retry delay and back off without burning
further requests; the block is surfaced in the UI header rather than hidden.

---

## Layout

```
backend/
  agents/
    tension.py       deterministic pairing scorer
    director.py      Gemini + ADK — the judgement call
    cutguard.py      broadcast grammar
    orchestrator.py  the 10 Hz loop
  data/
    geometry.py      circuit centerline, arc-length projection
    source.py        FastF1 replay + synthetic fallback
  grafana/
    client.py        dashboard, annotations, alert rules
    metrics.py       Prometheus exposition
    dashboard.json   panel spec
  main.py            FastAPI + WebSocket
frontend/            broadcast UI
```

## License

MIT — see [LICENSE](LICENSE).
