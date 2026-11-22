"""Records calls on this Mac with Quorum Recorder.app (transcriber/record/quorum_record.swift).

The helper runs as its own small background app (opened with `open`), so macOS asks for microphone permission for
"Quorum Recorder" whatever started the transcriber, and a transcriber restart never stops a recording. Without
Screen & System Audio Recording permission (common on managed Macs, where the switch may not take effect for a
self-built app) the helper opens macOS's own window/screen picker at start: `list` then says `picker: true` and the
app list has no window titles. The two talk through files only:

- events: JSON lines the helper appends (launched, started, levels, warning, stopped, error);
- the recording: a hidden `.recording-<id>.mov` in the uploads folder (hidden files aren't listed as recordings, so a
  call in progress is never offered for transcription), renamed to `Call <date>.mov` when it ends;
- `recording.json` in the transcripts folder: the one recording in progress (pid, files, options).

The recording keeps the call and your microphone on separate tracks (see pipeline.track_roles). Tests point
QUORUM_RECORD_BIN at a fake helper (transcriber/tests/fake_recorder.py) that speaks the same events.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

from transcriber import config

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "record" / "quorum_record.swift"
BUILD_SCRIPT = HERE / "record" / "build.sh"
APP = HERE / "bin" / "Quorum Recorder.app"
BINARY = APP / "Contents" / "MacOS" / "quorum-record"

MIN_FREE_BYTES = 3 * 1024 ** 3   # refuse to start below this (the helper stops by itself below 2 GB)
START_TIMEOUT = 25.0             # seconds for the helper to start capturing (a permission prompt may be open)
PICK_TIMEOUT = 300.0             # once macOS's picker is shown: time to choose a window or screen
STOP_TIMEOUT = 60.0
CALL_SILENT_SECONDS = 60         # warn when the call is silent this long while you talk
SYSTEM_SOUND = 0.003             # RMS above this: the call track has sound
MIC_SOUND = 0.02

# Apps that are calls, and remote desktop clients a call may run inside (by bundle id or name).
MEETING_APPS = {
    "com.microsoft.teams2", "com.microsoft.teams", "us.zoom.xos", "com.cisco.webexmeetingsapp",
    "Cisco-Systems.Spark", "com.webex.meetingmanager", "com.tinyspeck.slackmacgap", "com.apple.FaceTime",
    "com.skype.skype", "com.ringcentral.glip", "com.gotomeeting.GoToMeeting", "com.bluejeans.mac",
}
BROWSERS = {
    "com.google.Chrome", "com.apple.Safari", "org.mozilla.firefox", "com.microsoft.edgemac",
    "company.thebrowser.Browser", "com.brave.Browser", "com.operasoftware.Opera", "com.vivaldi.Vivaldi",
}
MEETING_TITLE = re.compile(r"\b(google meet|meet\b|teams|zoom|webex|whereby|jitsi|gotomeeting|chime)", re.I)
REMOTE_APPS = {
    "com.microsoft.rdc.macos", "com.microsoft.rdc.mac", "com.microsoft.rdc.osx.beta",
    "com.citrix.receiver.icaviewer.mac", "com.citrix.receiver.nomas", "com.vmware.horizon",
    "com.omnissa.horizon.client.mac", "com.apple.ScreenSharing", "com.realvnc.vncviewer",
    "com.teamviewer.TeamViewer", "com.parallels.client", "com.anydesk.anydeskmacos", "com.p5sys.jump.mac.viewer",
}
REMOTE_NAME = re.compile(r"remote desktop|windows app|citrix|horizon|vnc|screen sharing|teamviewer|anydesk", re.I)


class RecorderError(Exception):
    """A recording couldn't start or stop. `kind`: unavailable, busy, disk, permission_screen, permission_mic,
    app_not_found, display_not_found, capture, timeout, not_recording, cancelled (nothing chosen in the picker),
    picker (the picker couldn't open)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------------
# Paths and files
# ---------------------------------------------------------------------------

def state_file() -> Path:
    """The one recording in progress (none when the file doesn't exist)."""
    return config.TRANSCRIPTS_DIR / "recording.json"


def work_root() -> Path:
    """Per-recording working files: the events file and, with the live draft, its PCM and frames."""
    return config.TRANSCRIPTS_DIR / ".recordings"


def load_state() -> dict | None:
    """The recording in progress (or ended but not yet collected), or None."""
    try:
        data = json.loads(state_file().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("id") else None


def save_state(state: dict | None) -> None:
    """Writes (or with None, removes) recording.json."""
    if state is None:
        state_file().unlink(missing_ok=True)
        return
    state_file().parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(state_file())


def read_events(path: Path) -> list[dict]:
    """The helper's JSON lines so far (a half-written last line is skipped)."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def final_name(title: str, started: datetime, folder: Path) -> Path:
    """`<title> 2026-11-22 14.05.mov` in `folder`, with ' (2)'… when taken. Unsafe characters are removed."""
    clean = re.sub(r"[^\w .,()&'-]+", " ", title or "").strip() or "Call"
    base = f"{clean[:80]} {started:%Y-%m-%d %H.%M}"
    path = folder / f"{base}.mov"
    n = 2
    while path.exists():
        path = folder / f"{base} ({n}).mov"
        n += 1
    return path


# ---------------------------------------------------------------------------
# The helper process
# ---------------------------------------------------------------------------

def fake_command() -> list[str] | None:
    """The test helper's command line (QUORUM_RECORD_BIN), or None for the real app."""
    raw = os.environ.get("QUORUM_RECORD_BIN", "").strip()
    return shlex.split(raw) if raw else None


def available() -> bool:
    """True when recording can work here: the app is built, or swiftc can build it (or a test helper is set)."""
    return bool(fake_command()) or BINARY.exists() or bool(shutil.which("swiftc"))


def ensure_app() -> None:
    """Builds Quorum Recorder.app when it's missing or older than its source (needs Xcode command line tools)."""
    if fake_command():
        return
    if BINARY.exists() and BINARY.stat().st_mtime >= SOURCE.stat().st_mtime:
        return
    if not shutil.which("swiftc"):
        raise RecorderError("unavailable", "Recording calls needs the Xcode command line tools "
                                           "(run xcode-select --install), then try again.")
    result = subprocess.run(["bash", str(BUILD_SCRIPT)], capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not BINARY.exists():
        raise RecorderError("unavailable", f"Couldn't build the recorder: {(result.stderr or result.stdout)[-300:]}")


def launch(args: list[str]) -> None:
    """Starts the helper with `args`, detached: the real app through `open` (its own permissions), the test
    helper as a child in its own session. Output goes to the --events file in `args`."""
    fake = fake_command()
    if fake:
        subprocess.Popen(fake + args, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    ensure_app()
    subprocess.run(["open", "-n", "-g", str(APP), "--args", *args], check=True, timeout=20,
                   capture_output=True)


def wait_for(events: Path, kinds: set[str], timeout: float, poll: float = 0.1) -> dict | None:
    """The first event whose `event` is in `kinds` (or a list reply: no `event` key), within `timeout` seconds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for item in read_events(events):
            if item.get("event") in kinds or ("event" not in item and "displays" in item):
                return item
        time.sleep(poll)
    return None


def alive(pid: int | None) -> bool:
    """True while the helper process `pid` is running (and is the recorder, not a reused pid)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
                                 timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "quorum-record" in command or "fake_recorder" in command


# ---------------------------------------------------------------------------
# Sources: apps, displays, microphones
# ---------------------------------------------------------------------------

def classify(app: dict) -> dict:
    """Adds `meeting` (a call app, or a browser showing a call) and `remote` (a remote desktop client)."""
    bundle, name = app.get("bundle_id", ""), app.get("name", "")
    titles = " ".join(w.get("title", "") for w in app.get("windows", []))
    meeting = bundle in MEETING_APPS or (bundle in BROWSERS and bool(MEETING_TITLE.search(titles)))
    remote = bundle in REMOTE_APPS or bool(REMOTE_NAME.search(name))
    return dict(app, meeting=meeting, remote=remote and not meeting)


def sort_apps(apps: list[dict]) -> list[dict]:
    """Calls first, then remote desktop clients, then the other apps, each by name."""
    return sorted((classify(a) for a in apps),
                  key=lambda a: (not a["meeting"], not a["remote"], a.get("name", "").lower()))


def list_sources(timeout: float = 15.0) -> dict:
    """Displays, apps with windows (calls and remote desktops first) and microphones, from the helper."""
    folder = work_root() / f"list-{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=True)
    events = folder / "events.jsonl"
    try:
        launch(["list", "--events", str(events)])
        reply = wait_for(events, {"error"}, timeout)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if reply is None:
        raise RecorderError("timeout", "The recorder didn't answer. Try again.")
    if reply.get("event") == "error":
        raise RecorderError(reply.get("kind", "capture"), reply.get("message", "The recorder failed."))
    return {"displays": reply.get("displays", []), "apps": sort_apps(reply.get("apps", [])),
            "microphones": reply.get("microphones", []), "picker": bool(reply.get("picker", False))}


# ---------------------------------------------------------------------------
# Start, status, stop
# ---------------------------------------------------------------------------

def source_args(source: dict) -> list[str]:
    """The helper's capture arguments for {kind: app|display|audio, id}. An app without an id is chosen in
    macOS's picker (--app-any)."""
    kind = source.get("kind")
    if kind == "app":
        return ["--app", str(source["id"])] if source.get("id") else ["--app-any"]
    if kind == "display":
        return ["--display", str(source.get("id") or 0)] if source.get("id") else ["--display", "0"]
    if kind == "audio":
        return ["--audio-only"]
    raise RecorderError("bad_source", "Choose a meeting app, a screen, or audio only.")


def start(source: dict, mic: str | None = None, live: bool = False, title: str = "") -> dict:
    """Starts recording. One recording at a time; refused when disk space is short. Returns the state (with
    `live_dir` when the live draft is on). Raises RecorderError with the helper's reason (permissions, …)."""
    current = load_state()
    if current and alive(current.get("pid")) and not current.get("stopped"):
        raise RecorderError("busy", "A call is already being recorded.")
    config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(config.UPLOADS_DIR).free < MIN_FREE_BYTES:
        raise RecorderError("disk", "Less than 3 GB free: free up space before recording a call.")
    rec_id = uuid.uuid4().hex[:12]
    folder = work_root() / rec_id
    folder.mkdir(parents=True, exist_ok=True)
    events = folder / "events.jsonl"
    out = config.UPLOADS_DIR / f".recording-{rec_id}.mov"
    args = ["start", *source_args(source), "--out", str(out), "--events", str(events)]
    if mic:
        args += ["--mic", mic]
    live_dir = folder / "live" if live else None
    if live_dir:
        args += ["--live", str(live_dir)]
    launch(args)
    first = wait_for(events, {"started", "error", "picker"}, START_TIMEOUT)
    if first is not None and first.get("event") == "picker":  # the user is choosing in macOS's picker
        first = wait_for(events, {"started", "error"}, PICK_TIMEOUT)
    launched = next((e for e in read_events(events) if e.get("event") == "launched"), {})
    if first is None or first.get("event") == "error":
        if launched.get("pid") and alive(launched["pid"]):
            os.kill(launched["pid"], signal.SIGTERM)
        shutil.rmtree(folder, ignore_errors=True)
        out.unlink(missing_ok=True)
        if first is None:
            picked = any(e.get("event") == "picker" for e in read_events(events))
            raise RecorderError("timeout", "Nothing was chosen in the macOS picker in time. Start again and choose "
                                           "the meeting window or screen." if picked else
                                           "The recorder didn't start. If macOS asked for permission, allow it and "
                                           "try again.")
        raise RecorderError(first.get("kind", "capture"), first.get("message", "Recording couldn't start."))
    state = {
        "id": rec_id, "pid": launched.get("pid"), "file": str(out), "events": str(events),
        "live_dir": str(live_dir) if live_dir else None, "source": dict(source, **(first.get("source") or {})),
        "mic": mic,
        "mic_name": first.get("mic"), "video": bool(first.get("video", source.get("kind") != "audio")),
        "live": live, "title": title.strip(), "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_state(state)
    return state


def call_silence_warning(events: list[dict]) -> dict | None:
    """A warning when the call track has been silent for CALL_SILENT_SECONDS while your mic had sound: usually
    the call's sound goes to another device, or the wrong app is recorded."""
    levels = [e for e in events if e.get("event") == "levels"]
    if not levels:
        return None
    now = levels[-1].get("elapsed", 0)
    last_call = max((e.get("elapsed", 0) for e in levels if e.get("system", 0) > SYSTEM_SOUND), default=0)
    you_talked = any(e.get("mic", 0) > MIC_SOUND and e.get("elapsed", 0) > last_call for e in levels)
    if now - last_call >= CALL_SILENT_SECONDS and you_talked:
        return {"kind": "call_silent",
                "message": "No sound from the call for a minute. Check that the meeting app is the one being "
                           "recorded and that its sound plays on this Mac."}
    return None


def status() -> dict:
    """The recording's state for the app: idle, recording, stopped (ended by itself, e.g. the meeting app quit or
    the disk filled up) or interrupted (the recorder died; what was saved can still be transcribed), with the
    elapsed time, the last levels, the file size and recent warnings."""
    state = load_state()
    if not state:
        return {"state": "idle"}
    events = read_events(Path(state["events"]))
    stopped = next((e for e in reversed(events) if e.get("event") == "stopped"), None)
    running = alive(state.get("pid")) and stopped is None
    levels = next((e for e in reversed(events) if e.get("event") == "levels"), {})
    warnings = [{k: e.get(k) for k in ("kind", "message", "mic")} for e in events if e.get("event") == "warning"]
    silent = call_silence_warning(events) if running else None
    if silent:
        warnings.append(silent)
    file = Path(state["file"])
    started = datetime.fromisoformat(state["started_at"])
    return {
        "state": "recording" if running else ("stopped" if stopped else "interrupted"),
        "id": state["id"], "title": state.get("title", ""), "source": state.get("source"),
        "mic": state.get("mic_name"), "video": state.get("video", True), "live": bool(state.get("live")),
        "started_at": state["started_at"],
        "elapsed": levels.get("elapsed") if running else
                   (stopped or {}).get("seconds", round((datetime.now() - started).total_seconds())),
        "levels": {"system": levels.get("system", 0.0), "mic": levels.get("mic", 0.0)} if running else None,
        "bytes": file.stat().st_size if file.exists() else 0,
        "warnings": warnings[-5:],
        "reason": (stopped or {}).get("reason"),
    }


def stop() -> dict:
    """Stops the recording (or collects one that already ended or was interrupted): the helper finishes the
    file, which is renamed to `<title> <date>.mov` in the uploads folder with its track layout next to it.
    Returns {path (None when nothing was recorded), seconds, complete, reason, id, live_dir}."""
    state = load_state()
    if not state:
        raise RecorderError("not_recording", "No call is being recorded.")
    events_path = Path(state["events"])
    stopped = next((e for e in reversed(read_events(events_path)) if e.get("event") == "stopped"), None)
    if stopped is None and alive(state.get("pid")):
        os.kill(state["pid"], signal.SIGINT)
        stopped = wait_for(events_path, {"stopped"}, STOP_TIMEOUT)
        if stopped is None and alive(state.get("pid")):
            os.kill(state["pid"], signal.SIGTERM)
            stopped = wait_for(events_path, {"stopped"}, 10)
    raw = Path(state["file"])
    started = datetime.fromisoformat(state["started_at"])
    path = None
    if raw.exists() and raw.stat().st_size > 0:
        path = final_name(state.get("title") or "Call", started, raw.parent)
        raw.rename(path)
        layout = Path(str(raw) + ".quorum.json")
        if layout.exists():
            layout.rename(Path(str(path) + ".quorum.json"))
    else:
        raw.unlink(missing_ok=True)
        Path(str(raw) + ".quorum.json").unlink(missing_ok=True)
    save_state(None)
    return {"id": state["id"], "path": str(path) if path else None,
            "seconds": (stopped or {}).get("seconds"), "complete": bool((stopped or {}).get("complete", False)),
            "reason": (stopped or {}).get("reason", "interrupted"), "live_dir": state.get("live_dir"),
            "title": state.get("title", "")}


def busy() -> bool:
    """True while a call is being recorded (heavy work waits, so the call and the recording stay smooth)."""
    state = load_state()
    return bool(state) and alive(state.get("pid")) and not any(
        e.get("event") == "stopped" for e in read_events(Path(state["events"])))


def cleanup(rec_id: str) -> None:
    """Deletes a finished recording's working files (events, live draft) once its transcript exists."""
    if re.fullmatch(r"[0-9a-f]{12}", rec_id or ""):
        shutil.rmtree(work_root() / rec_id, ignore_errors=True)
