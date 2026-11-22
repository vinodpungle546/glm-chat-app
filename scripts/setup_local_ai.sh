#!/usr/bin/env bash
# One-time setup of the local (offline) AI stack on macOS / Apple silicon.
# This is the ONLY step that needs internet. Afterwards everything runs offline.
#
#   scripts/setup_local_ai.sh
#
# Speaker labels use pyannote/speaker-diarization-3.1, a gated model. Before
# running, accept the terms on huggingface.co for pyannote/speaker-diarization-3.1,
# pyannote/segmentation-3.0, pyannote/wespeaker-voxceleb-resnet34-LM and
# pyannote/speaker-diarization-community-1 (pyannote 4 loads its PLDA file from it),
# then log in once with:  hf auth login
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "== 1/4 Homebrew packages (ollama, ffmpeg)"
brew list ollama >/dev/null 2>&1 || brew install ollama
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg

echo "== 2/4 Local chat models and the vision model (names on screen)"
scripts/start_local_ai.sh >/dev/null 2>&1 || true   # make sure ollama serve is up
for model in ${LOCAL_MODELS_TO_PULL:-qwen3:8b qwen3:4b nomic-embed-text qwen3-vl:4b-instruct}; do
  ollama pull "$model"
done

echo "== 3/4 Transcriber Python environment"
command -v uv >/dev/null || { echo "Install uv first: https://docs.astral.sh/uv/"; exit 1; }
[[ -d transcriber/.venv ]] || uv venv transcriber/.venv --python 3.12
VIRTUAL_ENV=transcriber/.venv uv pip install -r transcriber/requirements.txt

echo "== 4/4 Transcription models (downloaded once, then used offline)"
HF_HUB_OFFLINE=0 transcriber/.venv/bin/python -m transcriber.download_models

echo "== On-screen names: Apple's on-device text recognition helper (needs Xcode command line tools)"
if command -v swiftc >/dev/null; then
  mkdir -p transcriber/bin && swiftc -O transcriber/ocr/quorum_ocr.swift -o transcriber/bin/quorum-ocr \
    && echo "✓ transcriber/bin/quorum-ocr" || echo "✗ Couldn't build the text recognition helper (names on screen stay off)"
else
  echo "swiftc not found: run xcode-select --install to read names from meeting videos"
fi

echo "== Recording calls: Quorum Recorder.app (ScreenCaptureKit; your mic and the call on separate tracks)"
if command -v swiftc >/dev/null; then
  bash transcriber/record/build.sh >/dev/null \
    && echo "✓ transcriber/bin/Quorum Recorder.app (macOS asks for Screen & System Audio Recording and Microphone the first time)" \
    || echo "✗ Couldn't build the call recorder (Record a call stays off)"
else
  echo "swiftc not found: run xcode-select --install to record calls"
fi

echo
echo "Done. Start the services with: scripts/start_local_ai.sh"
