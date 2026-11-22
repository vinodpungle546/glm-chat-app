#!/usr/bin/env bash
# Start the local (offline) AI services natively on macOS so they can use the
# Apple GPU: Ollama (chat models) and the meeting transcriber (Whisper + pyannote).
#
#   scripts/start_local_ai.sh          # start both (skips any already running)
#   scripts/start_local_ai.sh stop     # stop both
#   scripts/start_local_ai.sh restart  # restart the transcriber (after updating its code)
#
# Logs: data/logs/ollama.log, data/logs/transcriber.log
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="$ROOT/data/logs"
mkdir -p "$LOGS"

# Load MEETINGS_DIR / TRANSCRIPTS_DIR etc. from .env if present (no secrets needed here).
if [[ -f "$ROOT/.env" ]]; then
  set -a; source "$ROOT/.env"; set +a
fi

# --- Ollama tuned for a 16 GB MacBook Air (see plan: Hardware fit) -----------
export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
export OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-32768}"
export OLLAMA_FLASH_ATTENTION="${OLLAMA_FLASH_ATTENTION:-1}"
export OLLAMA_KV_CACHE_TYPE="${OLLAMA_KV_CACHE_TYPE:-q8_0}"
export OLLAMA_MAX_LOADED_MODELS="${OLLAMA_MAX_LOADED_MODELS:-1}"
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-5m}"

# --- Transcriber: strictly offline --------------------------------------------
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
export PYANNOTE_METRICS_ENABLED=0
export MEETINGS_DIR="${MEETINGS_DIR_HOST:-$HOME/Meetings}"
export TRANSCRIPTS_DIR="${TRANSCRIPTS_DIR_HOST:-$ROOT/data/transcripts}"
export OLLAMA_URL="http://$OLLAMA_HOST"
TRANSCRIBER_PORT="${TRANSCRIBER_PORT:-8765}"
mkdir -p "$MEETINGS_DIR" "$TRANSCRIPTS_DIR"

if [[ "${1:-}" == "stop" ]]; then
  pkill -f "uvicorn transcriber.server:app" && echo "Transcriber stopped" || echo "Transcriber not running"
  pkill -f "ollama serve" && echo "Ollama stopped" || echo "Ollama not running"
  exit 0
fi

if curl -sf "http://$OLLAMA_HOST/api/version" >/dev/null; then
  echo "✓ Ollama already running on $OLLAMA_HOST"
else
  echo "Starting Ollama on $OLLAMA_HOST ..."
  nohup ollama serve >>"$LOGS/ollama.log" 2>&1 &
  for _ in $(seq 1 30); do curl -sf "http://$OLLAMA_HOST/api/version" >/dev/null && break; sleep 1; done
  curl -sf "http://$OLLAMA_HOST/api/version" >/dev/null && echo "✓ Ollama started" || { echo "✗ Ollama failed; see $LOGS/ollama.log"; exit 1; }
fi

if [[ "${1:-}" == "restart" ]]; then
  pkill -f "uvicorn transcriber.server:app" && sleep 1 || true  # pick up new transcriber code
fi
if curl -sf "http://127.0.0.1:$TRANSCRIBER_PORT/health" >/dev/null; then
  echo "✓ Transcriber already running on 127.0.0.1:$TRANSCRIBER_PORT"
else
  if [[ ! -x "$ROOT/transcriber/.venv/bin/python" ]]; then
    echo "✗ Transcriber venv missing. Run scripts/setup_local_ai.sh first."; exit 1
  fi
  echo "Starting transcriber on 127.0.0.1:$TRANSCRIBER_PORT (offline) ..."
  cd "$ROOT"
  nohup "$ROOT/transcriber/.venv/bin/python" -m uvicorn transcriber.server:app \
    --host 127.0.0.1 --port "$TRANSCRIBER_PORT" >>"$LOGS/transcriber.log" 2>&1 &
  for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:$TRANSCRIBER_PORT/health" >/dev/null && break; sleep 1; done
  if curl -sf "http://127.0.0.1:$TRANSCRIBER_PORT/health" >/dev/null; then
    echo "✓ Transcriber started"
  else
    echo "✗ Transcriber failed; see $LOGS/transcriber.log"; tail -n 20 "$LOGS/transcriber.log"; exit 1
  fi
fi

echo "Meetings watch folder: $MEETINGS_DIR"
