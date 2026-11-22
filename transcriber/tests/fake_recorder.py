#!/usr/bin/env python3
"""A stand-in for Quorum Recorder.app in tests (QUORUM_RECORD_BIN="python fake_recorder.py"): the same commands
and JSON-line events, no screen capture. Env knobs:
  FAKE_RECORD_ERROR=permission_screen   start fails with that error kind
  FAKE_RECORD_TICK=0.1                  seconds per "second" of recording (levels events)
  FAKE_RECORD_CALL_SILENT=1             the call track stays silent (system level 0)
  FAKE_RECORD_MIC_SWITCH=3              after this many ticks, report mic_switched
  FAKE_RECORD_STOP_AFTER=5              end by itself after this many ticks (reason app_quit)
  FAKE_RECORD_PCM=<dir>                 with --live: copy system.pcm / mic.pcm / frames/<s>.jpg from this folder over time
  FAKE_RECORD_PICKER=1                  no screen access: list says picker (no window titles); start shows the picker
  FAKE_RECORD_PICK=cancel               the user closes the picker without choosing (default: picks zoom.us)
On stop it writes a small two-track .mov (ffmpeg: a tone on the call track, silence on the mic) and its layout file.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

args = sys.argv[1:]
events_path = None
if "--events" in args:
    i = args.index("--events")
    events_path = Path(args[i + 1])
    del args[i:i + 2]


def emit(obj):
    line = json.dumps(obj)
    print(line, flush=True)
    if events_path:
        with events_path.open("a") as f:
            f.write(line + "\n")


def opt(name):
    return args[args.index(name) + 1] if name in args else None


emit({"event": "launched", "pid": os.getpid()})
if args[0] == "list":
    emit({"displays": [{"id": 1, "name": "Built-in Retina Display", "width": 1512, "height": 982, "main": True}],
          "apps": [{"bundle_id": "com.apple.TextEdit", "name": "TextEdit", "display": 1,
                    "windows": [{"title": "Notes", "width": 800, "height": 600, "display": 1}]},
                   {"bundle_id": "com.microsoft.rdc.macos", "name": "Windows App", "display": 1,
                    "windows": [{"title": "Work PC", "width": 1400, "height": 900, "display": 1}]},
                   {"bundle_id": "us.zoom.xos", "name": "zoom.us", "display": 1,
                    "windows": [{"title": "Zoom Meeting", "width": 1200, "height": 800, "display": 1}]}],
          "microphones": [{"id": "BuiltInMicrophoneDevice", "name": "MacBook Air Microphone", "default": True,
                           "bluetooth": False},
                          {"id": "airpods-uid", "name": "AirPods Pro", "default": False, "bluetooth": True}],
          "picker": os.environ.get("FAKE_RECORD_PICKER") == "1"})
    sys.exit(0)

if os.environ.get("FAKE_RECORD_ERROR"):
    emit({"event": "error", "kind": os.environ["FAKE_RECORD_ERROR"], "message": "Not allowed (fake)."})
    sys.exit(1)

picked = None
if os.environ.get("FAKE_RECORD_PICKER") == "1" or "--app-any" in args:
    emit({"event": "picker", "mode": "app" if ("--app" in args or "--app-any" in args) else "display"})
    time.sleep(0.3)  # the user choosing
    if os.environ.get("FAKE_RECORD_PICK") == "cancel":
        emit({"event": "error", "kind": "cancelled", "message": "No window or screen was chosen, so nothing is recorded."})
        sys.exit(1)
    picked = ({"kind": "app", "id": "us.zoom.xos", "name": "zoom.us", "picked": True}
              if ("--app" in args or "--app-any" in args) else {"kind": "display", "picked": True})

out = Path(opt("--out"))
live = Path(opt("--live")) if opt("--live") else None
tick = float(os.environ.get("FAKE_RECORD_TICK", "0.1"))
silent = os.environ.get("FAKE_RECORD_CALL_SILENT") == "1"
switch_at = int(os.environ.get("FAKE_RECORD_MIC_SWITCH", "0") or 0)
stop_after = int(os.environ.get("FAKE_RECORD_STOP_AFTER", "0") or 0)
pcm_source = Path(os.environ["FAKE_RECORD_PCM"]) if os.environ.get("FAKE_RECORD_PCM") else None
stopping = {"reason": None}
signal.signal(signal.SIGINT, lambda *_: stopping.update(reason="stopped"))
signal.signal(signal.SIGTERM, lambda *_: stopping.update(reason="stopped"))

out.write_bytes(b"")  # the helper creates the file at once
layout = {"recorder": "quorum-record", "version": 1, "audio_tracks": ["system", "mic"],
          "source": {"kind": "audio"} if "--audio-only" in args else
          picked or ({"kind": "app", "id": opt("--app")} if opt("--app") else {"kind": "display", "id": opt("--display")}),
          "video": "--audio-only" not in args}
Path(str(out) + ".quorum.json").write_text(json.dumps(layout))
emit({"event": "started", "file": str(out), "mic": "MacBook Air Microphone", "video": layout["video"],
      "source": layout["source"]})
if live:
    live.mkdir(parents=True, exist_ok=True)

n = 0
while stopping["reason"] is None:
    time.sleep(tick)
    n += 1
    emit({"event": "levels", "system": 0.0 if silent else 0.08, "mic": 0.05, "elapsed": n})
    if live and pcm_source:
        # Copy the source PCM in 1-second steps (16 kHz mono 16-bit = 32000 bytes per second).
        for name in ("system.pcm", "mic.pcm"):
            src = (pcm_source / name).read_bytes() if (pcm_source / name).exists() else b""
            (live / name).write_bytes(src[: n * 32000])
        for frame in sorted((pcm_source / "frames").glob("*.jpg")) if (pcm_source / "frames").exists() else []:
            if int(frame.stem) <= n:  # frames/<second>.jpg appear as the recording reaches them
                (live / "frames").mkdir(exist_ok=True)
                (live / "frames" / frame.name).write_bytes(frame.read_bytes())
    if switch_at and n == switch_at:
        emit({"event": "warning", "kind": "mic_switched", "mic": "AirPods Pro",
              "message": "Microphone switched to AirPods Pro."})
    if stop_after and n >= stop_after:
        emit({"event": "warning", "kind": "capture_stopped", "message": "The meeting app quit."})
        stopping["reason"] = "app_quit"

seconds = max(1, n)
tmp = out.with_name(out.stem + ".tmp.mov")
subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate=48000",
                "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={seconds}",
                "-map", "0:a", "-map", "1:a", "-c:a", "aac", "-t", str(seconds),
                "-metadata:s:a:0", "handler_name=System audio", "-metadata:s:a:1", "handler_name=Microphone", str(tmp)],
               check=True)
tmp.replace(out)
emit({"event": "stopped", "reason": stopping["reason"], "file": str(out), "seconds": seconds, "complete": True})
