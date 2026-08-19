#!/usr/bin/env bash
# Deploy The Gallery to Google Cloud Run.
#
#   ./scripts/deploy-cloudrun.sh                 # uses gcloud's current project
#   PROJECT_ID=my-proj REGION=us-central1 ./scripts/deploy-cloudrun.sh
#
# Reads GOOGLE_API_KEY / GRAFANA_URL / GRAFANA_TOKEN out of .env, stores the
# two secrets in Secret Manager, and mounts them into the service. Idempotent:
# safe to re-run for every redeploy.

set -euo pipefail

SERVICE="${SERVICE:-the-gallery}"
REGION="${REGION:-us-central1}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

say()  { printf '\033[1;35m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

command -v gcloud >/dev/null || die "gcloud not installed — https://cloud.google.com/sdk/docs/install"
gcloud auth list --filter=status:ACTIVE --format='value(account)' | grep -q . \
  || die "not authenticated — run: gcloud auth login"

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
[ -n "$PROJECT_ID" ] && [ "$PROJECT_ID" != "(unset)" ] \
  || die "no project set — run: gcloud config set project YOUR_PROJECT_ID"

say "project $PROJECT_ID · region $REGION · service $SERVICE"

# ---------------------------------------------------------------- env
[ -f .env ] || die ".env not found — copy .env.example and fill it in"
# .env fills gaps; it must not overwrite anything the caller set explicitly,
# or `GRAFANA_URL="" ./deploy.sh` silently ships the localhost value from .env.
while IFS= read -r line; do
  case "$line" in ''|'#'*) continue ;; esac
  key="${line%%=*}"; val="${line#*=}"
  case "$key" in *[!A-Za-z0-9_]*) continue ;; esac
  if [ -z "${!key+x}" ]; then export "$key=$val"; fi
done < .env
[ -n "${GOOGLE_API_KEY:-}${GOOGLE_CLOUD_PROJECT:-}" ] || warn "no Gemini credentials — the director will run deterministic"
[ -n "${GRAFANA_TOKEN:-}" ]  || warn "GRAFANA_TOKEN empty — Grafana calls will only be recorded"

# ---------------------------------------------------------------- apis
say "enabling APIs (no-op if already on)"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  --project "$PROJECT_ID" --quiet

# ---------------------------------------------------------------- secrets
put_secret() {  # name value
  local name="$1" value="$2"
  [ -n "$value" ] || return 0
  if gcloud secrets describe "$name" --project "$PROJECT_ID" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$name" \
      --data-file=- --project "$PROJECT_ID" --quiet >/dev/null
    say "secret $name — new version"
  else
    printf '%s' "$value" | gcloud secrets create "$name" \
      --data-file=- --replication-policy=automatic \
      --project "$PROJECT_ID" --quiet >/dev/null
    say "secret $name — created"
  fi
}

USE_VERTEX="${GOOGLE_GENAI_USE_VERTEXAI:-0}"
if [ "$USE_VERTEX" = "1" ]; then
  say "auth mode: Vertex AI (no API key — the runtime service account signs requests)"
else
  put_secret gallery-gemini-key "${GOOGLE_API_KEY:-}"
fi
put_secret gallery-grafana-token "${GRAFANA_TOKEN:-}"
put_secret gallery-prom-token "${GRAFANA_PROM_TOKEN:-}"

PROJECT_NUM="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
RUNTIME_SA="${PROJECT_NUM}-compute@developer.gserviceaccount.com"
if [ "$USE_VERTEX" = "1" ]; then
  # Without this the container authenticates fine and then 403s on every
  # generateContent call — the failure looks like a model problem, not IAM.
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role=roles/aiplatform.user --condition=None --quiet >/dev/null
  say "granted roles/aiplatform.user to the runtime service account"
fi

# On a fresh project the compute default service account doubles as the Cloud
# Build account but holds none of the roles the build needs. The failure is
# opaque — "could not resolve source ... storage.objects.get denied" on a
# bucket Cloud Run itself just created — so grant them up front.
for R in roles/cloudbuild.builds.builder roles/storage.objectViewer \
         roles/artifactregistry.writer roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${RUNTIME_SA}" --role="$R" \
    --condition=None --quiet >/dev/null 2>&1 || warn "could not grant $R"
done
say "build roles granted"

for s in gallery-gemini-key gallery-grafana-token gallery-prom-token; do
  gcloud secrets describe "$s" --project "$PROJECT_ID" >/dev/null 2>&1 || continue
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role=roles/secretmanager.secretAccessor \
    --project "$PROJECT_ID" --quiet >/dev/null
done
say "secret access granted to $RUNTIME_SA"

# ---------------------------------------------------------------- deploy
SECRET_FLAGS=""
if [ "$USE_VERTEX" != "1" ] && gcloud secrets describe gallery-gemini-key --project "$PROJECT_ID" >/dev/null 2>&1; then
  SECRET_FLAGS="GOOGLE_API_KEY=gallery-gemini-key:latest"
fi
if gcloud secrets describe gallery-grafana-token --project "$PROJECT_ID" >/dev/null 2>&1; then
  [ -n "$SECRET_FLAGS" ] && SECRET_FLAGS="${SECRET_FLAGS},"
  SECRET_FLAGS="${SECRET_FLAGS}GRAFANA_TOKEN=gallery-grafana-token:latest"
fi
if gcloud secrets describe gallery-prom-token --project "$PROJECT_ID" >/dev/null 2>&1; then
  [ -n "$SECRET_FLAGS" ] && SECRET_FLAGS="${SECRET_FLAGS},"
  SECRET_FLAGS="${SECRET_FLAGS}GRAFANA_PROM_TOKEN=gallery-prom-token:latest"
fi

say "building and deploying — first run takes ~5 min"

# --min-instances 1   the race loop must keep running between requests
# --no-cpu-throttling CPU stays allocated outside request handling, or the
#                     10 Hz orchestrator freezes the moment a request ends
# --timeout 3600      let the live WebSocket stay open
gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --min-instances 1 \
  --max-instances 3 \
  --cpu 1 --memory 2Gi \
  --no-cpu-throttling \
  --timeout 3600 \
  --concurrency 60 \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=${USE_VERTEX},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION:-us-central1},DIRECTOR_TIMEOUT_S=${DIRECTOR_TIMEOUT_S:-4},DIRECTOR_MODEL=${DIRECTOR_MODEL:-gemini-2.5-flash},RACE_YEAR=${RACE_YEAR:-2023},RACE_EVENT=${RACE_EVENT:-Monza},RACE_SESSION=${RACE_SESSION:-R},REPLAY_SPEED=${REPLAY_SPEED:-8.0},GRAFANA_URL=${GRAFANA_URL:-},GRAFANA_PROM_URL=${GRAFANA_PROM_URL:-},GRAFANA_PROM_USER=${GRAFANA_PROM_USER:-},DIRECTOR_COOLDOWN_S=${DIRECTOR_COOLDOWN_S:-13}" \
  ${SECRET_FLAGS:+--set-secrets "$SECRET_FLAGS"} \
  --quiet

URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" \
        --region "$REGION" --format='value(status.url)')"

printf '\n\033[1;32m✓ deployed\033[0m  %s\n\n' "$URL"
say "verify:"
echo "    curl -s $URL/api/health | python3 -m json.tool"
echo "    open $URL"
echo
say "the header must read 'adk', not 'standby' — if it reads standby, check:"
echo "    gcloud run services logs read $SERVICE --region $REGION --limit 40"
