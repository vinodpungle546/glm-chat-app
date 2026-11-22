#!/usr/bin/env bash
# Start Quorum natively: the Python API (127.0.0.1:8000) and the Next.js web app (http://localhost:3000).
# Ollama + the transcriber are started separately with scripts/start_local_ai.sh.
#   scripts/start_app.sh          # production build of the web app (builds on first run)
#   scripts/start_app.sh dev      # Next.js dev server with hot reload
#   scripts/start_app.sh api      # (re)start only the API, e.g. after changing Python code
#   scripts/start_app.sh stop
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="$ROOT/data/logs"
API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3000}"
mkdir -p "$LOGS"

if [[ "${1:-}" == "stop" ]]; then
  pkill -f "uvicorn api.main:app" && echo "API stopped" || echo "API not running"
  pkill -f "next (start|dev).*-p $WEB_PORT" && echo "Web app stopped" || echo "Web app not running"
  exit 0
fi

if [[ -f "$ROOT/.env" ]]; then set -a; source "$ROOT/.env"; set +a; fi
export APP_DATA_DIR="${APP_DATA_DIR:-$ROOT/data}"
export PROJECTS_DIR="${PROJECTS_DIR_HOST:-$ROOT/data/projects}"
export SESSIONS_DIR="${SESSIONS_DIR_HOST:-$ROOT/data/sessions}"
export CONNECTORS_FILE="${CONNECTORS_FILE_HOST:-$ROOT/data/connectors/connectors.json}"
export PEOPLE_FILE="${PEOPLE_FILE:-$ROOT/data/people.json}"
export LOCAL_LLM_BASE_URL="${LOCAL_LLM_BASE_URL_HOST:-http://localhost:11434}"
export TRANSCRIBER_URL="${TRANSCRIBER_URL_HOST:-http://localhost:8765}"
export WEB_ORIGINS="http://localhost:$WEB_PORT,http://127.0.0.1:$WEB_PORT"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "Creating the Python environment (.venv)…"
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt" -r "$ROOT/api/requirements.txt"
fi

if [[ "${1:-}" == "api" ]]; then
  pkill -f "uvicorn api.main:app" && sleep 1 || true
fi

if curl -sf "http://127.0.0.1:$API_PORT/api/health" >/dev/null; then
  echo "✓ API already running on 127.0.0.1:$API_PORT"
else
  cd "$ROOT"
  nohup "$ROOT/.venv/bin/python" -m uvicorn api.main:app --host 127.0.0.1 --port "$API_PORT" >>"$LOGS/api.log" 2>&1 &
  for _ in $(seq 1 30); do curl -sf "http://127.0.0.1:$API_PORT/api/health" >/dev/null && break; sleep 0.5; done
  curl -sf "http://127.0.0.1:$API_PORT/api/health" >/dev/null && echo "✓ API started (logs: data/logs/api.log)" \
    || { echo "✗ API failed; see $LOGS/api.log"; tail -n 20 "$LOGS/api.log"; exit 1; }
fi

[[ "${1:-}" == "api" ]] && exit 0

cd "$ROOT/web"
[[ -d node_modules ]] || npm install
export NEXT_PUBLIC_API_URL="http://localhost:$API_PORT"
if [[ "${1:-}" == "dev" ]]; then
  nohup npx next dev -p "$WEB_PORT" >>"$LOGS/web.log" 2>&1 &
else
  [[ -f .next/BUILD_ID ]] || npm run build
  nohup npx next start -p "$WEB_PORT" >>"$LOGS/web.log" 2>&1 &
fi
for _ in $(seq 1 60); do curl -sf "http://localhost:$WEB_PORT" >/dev/null && break; sleep 0.5; done
curl -sf "http://localhost:$WEB_PORT" >/dev/null && echo "✓ Quorum is running at http://localhost:$WEB_PORT" \
  || { echo "✗ Web app failed; see $LOGS/web.log"; tail -n 20 "$LOGS/web.log"; exit 1; }
