#!/usr/bin/env bash
# Set up the local chat models for Quorum on macOS/Linux (repeatable; safe to re-run).
#   scripts/setup-local-models.sh               # Gemma 4 E4B (default)
#   scripts/setup-local-models.sh qwen3:8b ...  # other / additional models
# Needs internet for the downloads only. Gemma 4 E4B is ~9.6 GB.
set -euo pipefail

MODELS=("${@:-gemma4:e4b}")
MIN_VERSION="0.31.0"   # gemma4 needs Ollama >= 0.31 (ollama show gemma4:e4b → "requires")
OLLAMA_URL="${OLLAMA_HOST:-http://127.0.0.1:11434}"
[[ "$OLLAMA_URL" == http* ]] || OLLAMA_URL="http://$OLLAMA_URL"

ver_ge() { [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]; }

echo "== 1/4 Ollama installed?"
if ! command -v ollama >/dev/null 2>&1; then
  echo "✗ Ollama is not installed."
  if [[ "$(uname)" == "Darwin" ]]; then echo "  Install it with: brew install ollama   (or download from https://ollama.com/download)"
  else echo "  Install it with: curl -fsSL https://ollama.com/install.sh | sh"; fi
  exit 1
fi
VERSION="$(ollama --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1)"
echo "✓ ollama $VERSION"
if ! ver_ge "$VERSION" "$MIN_VERSION"; then
  echo "✗ Gemma 4 needs Ollama $MIN_VERSION or newer (you have $VERSION). Please update it first:"
  [[ "$(uname)" == "Darwin" ]] && echo "  brew upgrade ollama" || echo "  curl -fsSL https://ollama.com/install.sh | sh"
  exit 1
fi

echo "== 2/4 Ollama running?"
if ! curl -sf "$OLLAMA_URL/api/version" >/dev/null; then
  echo "  Starting 'ollama serve' in the background (logs: /tmp/ollama-serve.log)…"
  OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-32768}" \
    nohup ollama serve >/tmp/ollama-serve.log 2>&1 &
  for _ in $(seq 1 30); do curl -sf "$OLLAMA_URL/api/version" >/dev/null && break; sleep 1; done
fi
curl -sf "$OLLAMA_URL/api/version" >/dev/null || { echo "✗ Ollama did not start; see /tmp/ollama-serve.log"; exit 1; }
echo "✓ Ollama is running at $OLLAMA_URL"

echo "== 3/4 Downloading models (progress is shown; there is no time limit)"
for model in "${MODELS[@]}"; do
  if ollama list | awk 'NR>1 {print $1}' | grep -qx "$model"; then
    echo "✓ $model already downloaded"
  else
    [[ "$model" == "gemma4:e4b" ]] && echo "  $model is about 9.6 GB — this can take a while."
    ollama pull "$model"   # resumes if interrupted; just run the script again
  fi
done

echo "== 4/4 Verifying"
ollama list
for model in "${MODELS[@]}"; do
  printf '  %s → ' "$model"
  curl -sf "$OLLAMA_URL/api/chat" -d "{\"model\":\"$model\",\"stream\":false,\"think\":false,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: ready\"}]}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["message"]["content"].strip()[:60])' \
    || { echo "✗ test prompt failed"; exit 1; }
done
echo "✓ Local models are ready. Select them in Quorum's sidebar (Local group)."
