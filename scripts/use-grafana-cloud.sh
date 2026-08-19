#!/usr/bin/env bash
# Point The Gallery at a Grafana Cloud stack.
#
#   ./scripts/use-grafana-cloud.sh \
#       --url        https://YOURSTACK.grafana.net \
#       --token      glsa_xxxxxxxx \
#       --prom-url   https://prometheus-prod-XX-prod-YY-ZZ.grafana.net/api/prom/push \
#       --prom-user  1234567 \
#       --prom-token glc_xxxxxxxx
#
# --url/--token drive dashboard provisioning and cut annotations over the
# Grafana HTTP API. The --prom-* values configure remote_write, because a
# hosted Grafana has no route to a process on your machine and therefore
# cannot scrape /metrics — the local Prometheus has to push instead.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"

URL=""; TOKEN=""; PURL=""; PUSER=""; PTOKEN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --url)        URL="$2";    shift 2 ;;
    --token)      TOKEN="$2";  shift 2 ;;
    --prom-url)   PURL="$2";   shift 2 ;;
    --prom-user)  PUSER="$2";  shift 2 ;;
    --prom-token) PTOKEN="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

[ -f .env ] || { echo ".env not found — copy .env.example first" >&2; exit 1; }

set_env() {  # key value
  local k="$1" v="$2"
  [ -n "$v" ] || return 0
  if grep -q "^${k}=" .env; then
    python3 - "$k" "$v" <<'PY'
import pathlib, sys
k, v = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
out = []
for line in p.read_text().splitlines():
    out.append(f"{k}={v}" if line.startswith(f"{k}=") else line)
p.write_text("\n".join(out) + "\n")
PY
  else
    printf '%s=%s\n' "$k" "$v" >> .env
  fi
  echo "  .env  ${k} set"
}

set_env GRAFANA_URL   "$URL"
set_env GRAFANA_TOKEN "$TOKEN"

if [ -n "$PURL" ] && [ -n "$PUSER" ] && [ -n "$PTOKEN" ]; then
  python3 - "$PURL" "$PUSER" "$PTOKEN" <<'PY'
import pathlib, re, sys
url, user, token = sys.argv[1:4]
p = pathlib.Path("scripts/prometheus.yml")
text = p.read_text()
block = f"""
remote_write:
  - url: {url}
    basic_auth:
      username: "{user}"
      password: "{token}"
    write_relabel_configs:
      - source_labels: [__name__]
        regex: "gallery_.*"
        action: keep
"""
# Drop any previous remote_write block (live or commented) and append a fresh one.
text = re.sub(r"\n#?\s*remote_write:.*\Z", "", text, flags=re.S).rstrip()
p.write_text(text + "\n" + block)
print("  prometheus.yml  remote_write configured")
PY
  docker compose restart prometheus >/dev/null 2>&1 && echo "  prometheus restarted"
else
  echo "  remote_write skipped (need --prom-url, --prom-user and --prom-token)"
fi

echo
echo "Now restart the app so it re-provisions against the cloud stack:"
echo "  pkill -f 'uvicorn backend.main'; .venv/bin/uvicorn backend.main:app --port 8042"
