#!/usr/bin/env python3
"""Benchmark: on-screen names on a synthetic video call (no real meetings, no real people).

  .venv/bin/python scripts/benchmark_screen_names.py [--minutes 10] [--vision-model qwen3-vl:4b-instruct]
      [--vision-cap 6] [--speaker-models gemma4:e4b qwen3-vl:4b-instruct]

1. Makes a synthetic gallery-view call (transcriber/tests/synthetic_meeting.py): 4 participants,
   the active speaker's tile highlighted (a few with a white border, as newer Teams versions draw);
   a few turns have no highlight at all and a few are screen shares.
2. Runs the on-screen name stage with the real on-device text reader (and the local vision model,
   when given) in the transcriber's venv, and times it.
3. Runs the local speaker-identification pass on those results (no voices) and counts the speakers
   named correctly: without a model, and with each --speaker-models model reading the transcript.
4. Transcript reading on synthetic meetings where names can be inferred from what is said
   (tests/fixtures/speaker_meetings.py): correct / wrong / missed names and seconds per meeting,
   without a model and with each --speaker-models model.
5. Runs the cloud review against the fake cloud model (tests/fake_cloud.py), which is told to change
   one speaker, and counts the changes it made.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TRANSCRIBER_PY = ROOT / "transcriber" / ".venv" / "bin" / "python"

STAGE = r"""
import json, sys, time
from pathlib import Path
from transcriber import screen_names as sn
from transcriber.tests import synthetic_meeting as synth
out, minutes, vision_model, cap = Path(sys.argv[1]), float(sys.argv[2]), sys.argv[3], int(sys.argv[4])
turns = synth.meeting_turns(minutes)
modes = {i: ("none" if i % 9 == 4 else "share" if i % 13 == 7 else "white" if i % 11 == 2 else "gallery")
         for i in range(len(turns))}
video = out / "call.mp4"
t0 = time.time()
synth.make_video(video, turns, synth.NAMES, modes=modes)
made = time.time() - t0
segments = synth.segments_for(turns)
t0 = time.time()
found, info = sn.run(video, segments, out / "work", lambda f: None,
                     {"vision_model": vision_model, "vision_cap": cap if vision_model else 0})
info["stage_seconds"] = round(time.time() - t0, 1)
info["video_seconds"] = round(made, 1)
json.dump({"turns": turns, "segments": segments, "screen_names": found, "screen_info": info,
           "truth": {f"SPEAKER_{i:02d}": n for i, n in enumerate(synth.NAMES)}}, open(out / "stage.json", "w"))
"""


def main() -> None:
    """Runs the benchmark described at the top of this file and prints the results as JSON."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--vision-model", default="")
    parser.add_argument("--vision-cap", type=int, default=6)
    parser.add_argument("--speaker-models", nargs="*", default=[],
                        help="local models that read the transcript (each is measured)")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        subprocess.run([str(TRANSCRIBER_PY), "-c", STAGE, str(out), str(args.minutes), args.vision_model,
                        str(args.vision_cap)], cwd=ROOT, check=True)
        data = json.loads((out / "stage.json").read_text())

        # Isolated stores: nothing from the user's data is read or written.
        os.environ.update(SECRETS_BACKEND="file", SECRETS_FILE=str(out / "secrets.json"))
        import cloud_providers
        import memory_store
        import speaker_id
        import speaker_review
        from tests import fake_cloud
        memory_store.MEMORY_DIR = out / "memory"
        memory_store._legacy_paths = lambda: {"people": out / "x", "imported": out / "y", "projects": []}
        cloud_providers.CONFIG_FILE = out / "providers.json"

        import model_registry
        from tests.fixtures import speaker_meetings

        transcript = {"segments": data["segments"], "speakers": sorted(data["truth"]),
                      "screen_names": data["screen_names"]}
        truth = data["truth"]

        def reader(model):
            """A `complete` function for speaker_id that uses `model` (loaded first, so loading time isn't
            counted per meeting); None for 'no model'.
            """
            if not model:
                return None
            provider = model_registry.OllamaProvider(model)
            provider.complete([{"role": "user", "content": "Say ok."}], max_tokens=5, temperature=0)  # load it
            return lambda msgs: provider.complete(msgs, max_tokens=1800, temperature=0)

        local_pass, reading = {}, {}
        for model in [""] + args.speaker_models:
            complete = reader(model)
            t0 = time.time()
            labels = speaker_id.identify(transcript, [], [], complete=complete)
            name = model or "no model"
            local_pass[name] = {"named_correctly": f"{sum(labels.get(s, {}).get('name') == n for s, n in truth.items())} of {len(truth)}",
                                "seconds": round(time.time() - t0, 1)}
            totals, seconds = {"correct": 0, "wrong": 0, "missed": 0}, []
            for meeting in speaker_meetings.MEETINGS:
                t0 = time.time()
                found = speaker_id.identify(speaker_meetings.transcript(meeting), [], [], complete=complete)
                seconds.append(time.time() - t0)
                for key, value in speaker_meetings.score(meeting, found).items():
                    totals[key] += value
            reading[name] = {**totals, "speakers": sum(totals.values()),
                             "seconds_per_meeting": round(sum(seconds) / len(seconds), 1)}
            if model:
                model_registry.unload_local(model)
        labels = speaker_id.identify(transcript, [], [], complete=None)

        with fake_cloud.FakeCloud() as url:
            os.environ["ANTHROPIC_PROVIDER_URL"] = url
            cloud_providers.add_provider("anthropic", key=fake_cloud.ANTHROPIC_KEY)
            fake_cloud.REVIEW_PLAN["changes"] = {"SPEAKER_03": {"name": truth["SPEAKER_03"], "role": "Release engineer",
                                                                "lines": [4], "reason": "Talks about the release."}}
            provider = cloud_providers.CloudChatProvider("anthropic", "claude-opus-5-5")
            review = speaker_review.review(transcript, labels, [], provider.complete)

    info = data["screen_info"]
    minutes = args.minutes
    print(json.dumps({
        "video": f"{minutes:g} min synthetic call, {len(data['turns'])} turns, 4 speakers",
        "stage_seconds": info["stage_seconds"], "frames": info["frames"], "named_frames": info["resolved"],
        "screen_share_frames": info["screen_share"], "unresolved_frames": info["unresolved"],
        "vision_frames": info["vision_frames"], "vision_resolved": info.get("vision_resolved", 0),
        "vision_model": info["vision_model"] or "off",
        "local_pass_on_the_call": local_pass,
        "transcript_reading_synthetic_meetings": reading,
        "cloud_review_changes": len(review["changes"]), "cloud_review_agreed": review["agreed"],
        "cloud_review_rejected": len(review["rejected"]),
    }, indent=1))


if __name__ == "__main__":
    main()
