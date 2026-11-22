"""One-time model download (the only step that uses the internet).

    HF_HUB_OFFLINE=0 transcriber/.venv/bin/python -m transcriber.download_models

Whisper is public. pyannote's speaker-label models are gated: accept each one's terms
(see config.DIARIZATION_REPOS) on huggingface.co and run `hf auth login` once (the token stays on this machine).
"""

from __future__ import annotations

import sys

from huggingface_hub import snapshot_download

from transcriber.config import DIARIZATION_REPOS, WHISPER_MODEL



def main() -> int:
    """Downloads Whisper and the pyannote speaker models into the Hugging Face cache (needs a token and the
    accepted model terms for pyannote). Returns 1 if a speaker model failed.
    """
    print(f"Downloading {WHISPER_MODEL} ...")
    snapshot_download(WHISPER_MODEL)
    print("✓ Whisper ready")

    failed = []
    for repo, patterns in DIARIZATION_REPOS:
        try:
            print(f"Downloading {repo} ...")
            snapshot_download(repo, allow_patterns=patterns)
        except Exception as exc:  # gated / no token
            failed.append((repo, exc))
    if failed:
        print("\n✗ Speaker-label models could not be downloaded:")
        for repo, exc in failed:
            print(f"  - {repo}: {type(exc).__name__}")
        pages = "".join(f"       https://huggingface.co/{repo}\n" for repo, _ in DIARIZATION_REPOS)
        print(
            "\nTo enable speaker labels:\n"
            "  1. While signed in to huggingface.co, accept the terms on each model page:\n"
            f"{pages}"
            "  2. Run:  transcriber/.venv/bin/hf auth login\n"
            "  3. Re-run: scripts/setup_local_ai.sh\n"
            "Transcription still works without them (no speaker labels)."
        )
        return 1
    print("✓ Speaker-label models ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
