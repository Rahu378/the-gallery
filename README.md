# The Gallery

Agent-directed world feed for a Formula 1 broadcast.

**Live app** — https://the-gallery-742484896712.us-central1.run.app
**Dashboard** — https://bluehalibut1967.grafana.net/public-dashboards/d86453f15cce4974a052692a6374e6c4 (no login)

Built for the Agentic Cinema hackathon, Grafana track.

## The problem

An F1 broadcast has a camera on every car and a hundred more around the
circuit, and sends one of them to the world feed. One person picks. When they
hold on the leader in clean air, a pass for P8 happens off screen and there is
no second take.

This scores every contested position continuously and cuts to a fight before
it resolves.

## How it works

```
FastF1 telemetry -> tension scorer -> director -> cut guard -> feed
                     deterministic   Gemini/ADK  deterministic
                          |               |            |
                          +---------------+------------+
                                          |
                            Grafana: annotations, alerts, metrics
```

| Stage | Kind | Does |
|---|---|---|
| Tension scorer | plain Python, 10 Hz | Scores all 19 adjacent pairings on gap, closing rate, DRS range, tyre delta |
| Director | Gemini 2.5 Flash via ADK | Picks which fight to show and writes the commentary line. Calls `recent_airtime` to spread coverage |
| Cut guard | plain Python | Minimum hold, staleness forcing, a margin before leaving a live story |

Scoring 19 pairings ten times a second is arithmetic and runs without a model.
The director is asked only what the arithmetic cannot answer — which of these
is worth showing. It sits behind the scorer and the guard, so it is called a
few times a minute rather than in the hot loop.

Every decision reports which tier produced it. `adk` and `genai` are model
decisions; `heuristic` and `timeout` are deterministic fallbacks. The UI
colours them differently and the logs label them, so a fallback is never shown
as though the model chose.

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
docker compose up -d              # Grafana + Prometheus, optional
.venv/bin/uvicorn backend.main:app --port 8042
```

First start pulls the race from FastF1 and caches it (~30 s). Without network
it falls back to a deterministic synthetic race and says so in the header.

| Variable | Default | Notes |
|---|---|---|
| `GOOGLE_GENAI_USE_VERTEXAI` | `0` | `1` for Vertex; then no API key is needed |
| `GOOGLE_CLOUD_PROJECT` | — | Required for Vertex |
| `GOOGLE_API_KEY` | — | AI Studio path only |
| `DIRECTOR_MODEL` | `gemini-2.5-flash` | Vertex and AI Studio expose different IDs |
| `DIRECTOR_COOLDOWN_S` | `13` | Wall seconds between director calls |
| `DIRECTOR_TIMEOUT_S` | `4` | Deadline before falling back |
| `GRAFANA_URL` / `GRAFANA_TOKEN` | — | Any Grafana, local or Cloud |
| `RACE_YEAR` / `RACE_EVENT` | `2023` / `Monza` | Anything FastF1 can fetch |
| `REPLAY_SPEED` | `8.0` | Race seconds per wall second |

## Deploying

```bash
gcloud auth login && gcloud config set project YOUR_PROJECT
./scripts/deploy-cloudrun.sh
```

Three flags matter and none are optional:

- `--min-instances 1` — the race loop has to keep running between requests
- `--no-cpu-throttling` — Cloud Run parks CPU outside request handling, which
  stops the 10 Hz loop dead the moment a request finishes
- `--timeout 3600` — keeps the WebSocket open

The image carries the race (`scripts/prefetch.py`), so a cold container starts
in about 9 s and does not depend on the F1 timing API being reachable.

## Notes from building it

Things that cost time, kept here because none of them are obvious and all of
them fail quietly.

**The centerline has to be exactly one lap.** Race order and every gap come
from arc-length along a circuit centerline. Building it from a raw slice of
position data pulls in in-laps, out-laps and the pit lane, so the line doubles
back on itself and every downstream gap is wrong. Symptom was Albon leading
Monza by 141 seconds. Use the fastest lap's telemetry.

**Start the replay at lights-out, not session start.** Session telemetry
begins on the grid, where the whole field sits at the same track position.
Every gap reads 0.00 s and the scorer sees twenty simultaneous photo finishes
scoring 0.61.

**Measure before tuning.** The momentum term was normalised by 0.28 s/s.
Actual closing rates across a full replay: p50 0.004, p90 0.012, p99 0.018.
The term contributed nothing until the divisor became 0.015.

**A race is finite.** The replay ran out after ~8 minutes, the loop returned,
and the WebSocket went silent while HTTP stayed healthy. Anyone opening the
page after that saw a frozen screen. Found by connecting six clients and
watching all six receive the opening message and then hang.

**Gemini 2.5 thinks by default, and it dominates latency.** Measured on
Vertex: 3.1–7.5 s per call, against 0.7–0.9 s with `thinking_budget=0`. The
director picks from a ranked shortlist, so the thinking budget buys nothing.

**Model IDs differ between AI Studio and Vertex.** The `-latest` aliases do
not exist on Vertex at all.

**Free-tier AI Studio caps at 20 requests a day** on the newest flash model,
and function calling makes one director decision cost two or three requests.
That is about two minutes of running before everything silently falls back.

**Vertex 429s carry no numbers.** Just "resource exhausted, try again later" —
dynamic shared quota, regional capacity rather than a fixed ceiling. A fixed
30 s backoff threw away most of a race for a transient blip; now 6–10 s with
jitter.

**The first ADK call pays 27 s of warm-up.** Left alone that lands on whoever
opens the page first. A throwaway decision now fires at startup.

**Cloud Build cannot reach the F1 timing API.** Position data comes back empty
there, so regenerating the race inside the build produced an empty cache and
the container served the synthetic race while reporting healthy.

**`gcloud run deploy --source` reads `.gcloudignore`,** and with no such file
derives one from `.gitignore` — which excluded `cache/`. The 101 MB never left
the machine and `COPY` failed on a directory that exists locally.

**A fresh GCP project gives the compute service account no build roles.** The
first deploy dies on `storage.objects.get denied` against a bucket Cloud Run
had just created itself, which reads like a Cloud Run bug rather than IAM.

**A Grafana Cloud stack ships several `prometheus` datasources.** One of them,
`grafanacloud-usage`, holds billing telemetry. Binding to the first type match
gives a dashboard that queries the wrong store, renders empty, and logs
nothing.

## State

Working and verified against the live deployment:

- FastF1 loads the 2023 Italian Grand Prix, 20 drivers, 9,718 replay frames
- Race order matches history: Sainz leads from pole, Verstappen 0.097 s behind,
  Verstappen through around lap 15
- Director on Vertex via ADK, 1.6–2.9 s per decision, roughly 72% of cuts
  model-authored over a three-minute sample, the rest falling back cleanly
- Six simultaneous WebSocket viewers, 97 frames each over 10 s, no spread
- Deployed WebSocket verified over `wss`, 118 frames in 12 s
- Grafana Cloud: dashboard provisioned by the app, alert rule installed, cut
  annotations landing on the timeline, public sharing on

Sample of unedited director output:

```
Verstappen is right on the gearbox of leader Carlos Sainz!
Norris under immense pressure from Hamilton in the fight for eighth.
Incredible wheel-to-wheel action between the two McLaren teammates!
```

Nothing in the prompt says Piastri and Norris drive for the same team.

## Known gaps

**No metrics in Grafana Cloud.** The timeseries panels are empty. Grafana
Cloud cannot scrape a process it has no route to, so the metrics need pushing
via Prometheus `remote_write`, which needs a Cloud Access Policy token that is
not yet configured. The "Director cuts" panel works regardless, since
annotations go over the API.

**Gap accuracy.** Gaps are progress difference times median lap time. Good to
roughly a tenth under racing conditions; not a substitute for official timing.

## Layout

```
backend/
  agents/     tension.py  director.py  cutguard.py  orchestrator.py
  data/       geometry.py  source.py
  grafana/    client.py  metrics.py  dashboard.json
  main.py     FastAPI + WebSocket
frontend/     broadcast UI
scripts/      deploy, prefetch, Grafana Cloud switchover
```

## License

MIT — see [LICENSE](LICENSE).
