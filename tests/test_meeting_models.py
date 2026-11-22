#!/usr/bin/env python3
"""Model choices for meetings: Settings, New meeting (kept with the job and the transcript), the
Speakers and Summary panels, fallbacks for missing models, frames only for vision models, and the
"identify and summarise when transcription finishes" runner. Synthetic data, fake services."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import llm_backends as lb
import meetings
import model_registry as mr
from api import main
from tests.test_api import TRANSCRIPT, client, events, ollama_reply  # noqa: F401  (client is a fixture)

INSTALLED = ["gemma4:e4b", "qwen3:8b", "qwen3-vl:4b-instruct"]
SPEAKERS = json.dumps({"speakers": [{"id": "SPEAKER_00", "name": None, "role": "Lead", "confidence": 0},
                                    {"id": "SPEAKER_01", "name": "Owen", "role": "QA", "confidence": 0.7}]})


@pytest.fixture
def ollama(monkeypatch):
    """Ollama with a text-only model (qwen3:8b) and two that can read images; records each request."""
    installed = {n: {"name": n, "size": 1, "details": {}} for n in INSTALLED}
    monkeypatch.setattr(mr, "ollama_state", lambda: {"running": True, "version": "0.40.16", "installed": installed})
    requests_seen: list[dict] = []

    def post(url, json=None, **_kw):
        requests_seen.append(dict(json or {}, _url=url))
        return ollama_reply(SPEAKERS)

    monkeypatch.setattr(lb.requests, "post", post)
    monkeypatch.setattr(mr.requests, "post", post)  # unload_local
    return {"installed": installed, "requests": requests_seen}


@pytest.fixture
def meeting(monkeypatch):
    """A transcript made by a New meeting job with its model choices."""
    state = {"transcript": dict(TRANSCRIPT, models={"speaker_model": "qwen3-vl:4b-instruct", "frames_model": "",
                                                    "summary_model": "", "after": False}), "names": {}}
    monkeypatch.setattr(meetings, "transcript", lambda tid: dict(state["transcript"], speaker_names=dict(state["names"])))
    monkeypatch.setattr(meetings, "transcripts", lambda: [{"id": "t1", "speaker_names": {}}])
    monkeypatch.setattr(meetings, "markdown", lambda tid: "[00:00:00] SPEAKER_00: Owen, can you give the update?\n")
    monkeypatch.setattr(meetings, "set_speaker_names", lambda tid, names: state["names"].update(names) or {})
    monkeypatch.setattr(meetings, "jobs", lambda: [])
    return state


def chat_models(seen: list[dict]) -> list[str]:
    return [r["model"] for r in seen if r["_url"].endswith("/api/chat")]


# --- Settings ----------------------------------------------------------------------------

def test_settings_choices_and_the_speaker_model_list(client, ollama):
    data = client.get("/api/models/speaker").json()
    by = {m["model"]: m for m in data["models"]}
    assert data["default"] == "gemma4:e4b" and by["gemma4:e4b"]["recommended"]
    assert by["qwen3-vl:4b-instruct"]["vision"] and by["gemma4:e4b"]["vision"] and not by["qwen3:8b"]["vision"]
    assert data["effective"] == "gemma4:e4b" and data["frames"] == "qwen3-vl:4b-instruct"
    assert client.put("/api/preferences", json={"speaker_model": "qwen3:8b", "summary_model": "cloud:gaia"}).status_code == 200
    assert client.get("/api/models/speaker").json()["effective"] == "qwen3:8b"
    assert client.put("/api/preferences", json={"speaker_model": "cloud:gaia"}).status_code == 400  # always local
    assert client.put("/api/preferences", json={"summary_model": "cloud:nope:x"}).status_code == 400


# --- New meeting ---------------------------------------------------------------------------

def test_new_meeting_choices_go_with_the_job(client, ollama, monkeypatch):
    seen = {}
    monkeypatch.setattr(meetings, "start", lambda path, speakers=None, screen=None, models=None:
                        seen.update(screen=screen, models=models) or {"id": "job1"})
    client.post("/api/meetings/jobs", json={"path": "/x/call.mov", "speaker_model": "qwen3:8b",
                                            "frames_model": "qwen3-vl:4b-instruct", "summary_model": "local:gemma4:e4b"})
    assert seen["models"] == {"speaker_model": "qwen3:8b", "frames_model": "qwen3-vl:4b-instruct",
                              "summary_model": "local:gemma4:e4b", "after": False}
    assert seen["screen"]["vision_model"] == "qwen3-vl:4b-instruct"


def test_a_text_only_model_never_gets_video_frames(client, ollama, monkeypatch):
    seen = {}
    monkeypatch.setattr(meetings, "start", lambda path, speakers=None, screen=None, models=None:
                        seen.update(screen=screen, models=models) or {"id": "job1"})
    client.post("/api/meetings/jobs", json={"path": "/x/call.mov", "speaker_model": "qwen3:8b", "frames_model": "qwen3:8b"})
    assert seen["screen"]["vision_model"] == "qwen3-vl:4b-instruct"  # the recommended vision model instead
    assert seen["models"]["speaker_model"] == "qwen3:8b"            # it still reads the transcript
    client.put("/api/preferences", json={"vision_model": "qwen3:8b"})
    assert main.screen_options()["vision_model"] == "qwen3-vl:4b-instruct"


def test_estimate_includes_the_chosen_models(client, ollama, monkeypatch):
    calls = {}
    monkeypatch.setattr(meetings, "estimate", lambda path, cap=0, seconds=None: calls.update(cap=cap, seconds=seconds)
                        or {"duration": 1800.0, "estimate_low": 100, "estimate_high": 200, "has_video": True})
    data = client.post("/api/meetings/estimate", json={"path": "/x/call.mov", "speakers": 3,
                                                       "frames_model": "qwen3-vl:4b-instruct",
                                                       "summary_model": "local:gemma4:e4b"}).json()
    assert calls == {"cap": 12, "seconds": 9.0}
    assert data["followups"]["speakers"]["model"] == "gemma4:e4b" and data["followups"]["speakers"]["high"] > 0
    assert data["followups"]["summary"]["local"] and data["followups"]["summary"]["low"] > 0
    cloud = client.post("/api/meetings/estimate", json={"path": "/x/call.mov", "summary_model": "cloud:gaia"}).json()
    assert cloud["followups"]["summary"]["local"] is False and cloud["followups"]["summary"]["low"] is None
    ask = client.post("/api/meetings/estimate", json={"path": "/x/call.mov", "model_id": "local:gemma4:e4b"}).json()
    assert ask["followups"]["summary"]["ask"] is True  # no summary model chosen: not the chat model
    missing = client.post("/api/meetings/estimate", json={"path": "/x/call.mov", "speaker_model": "llava:13b"}).json()
    assert missing["models"]["fallbacks"] == [{"kind": "speaker_model", "wanted": "llava:13b", "using": "gemma4:e4b"}]


# --- Speakers and Summary panels: re-runs reuse the meeting's choice --------------------------

def test_identify_uses_the_meetings_model_and_remembers_a_new_one(client, ollama, meeting):
    out = events(client.post("/api/meetings/transcripts/t1/identify", json={}))
    assert chat_models(ollama["requests"]) == ["qwen3-vl:4b-instruct"]  # from New meeting, not Settings
    assert next(e for e in out if e["type"] == "labels")["model"] == "qwen3-vl:4b-instruct"
    events(client.post("/api/meetings/transcripts/t1/identify", json={"speaker_model": "qwen3:8b"}))
    events(client.post("/api/meetings/transcripts/t1/identify", json={}))  # re-run: keeps qwen3:8b
    assert chat_models(ollama["requests"])[1:] == ["qwen3:8b", "qwen3:8b"]
    view = client.get("/api/meetings/transcripts/t1").json()["models"]
    assert view["speaker_model"] == "qwen3:8b" and view["speaker_used"] == "qwen3:8b"
    events(client.post("/api/meetings/transcripts/t1/identify", json={"speaker_model": ""}))  # back to Settings
    assert chat_models(ollama["requests"])[-1] == "gemma4:e4b"


def test_the_speaker_reply_limit_grows_with_the_number_of_speakers(client, ollama, meeting):
    import speaker_id
    assert speaker_id.reply_tokens(2) == 1800 and speaker_id.reply_tokens(20) == 3400
    segments = [{"start": i, "end": i + 1, "speaker": f"SPEAKER_{i:02d}", "text": f"Point number {i}."} for i in range(20)]
    meeting["transcript"]["segments"] = segments
    events(client.post("/api/meetings/transcripts/t1/identify", json={}))
    chat = [r for r in ollama["requests"] if r["_url"].endswith("/api/chat")]
    assert chat[-1]["options"]["num_predict"] == 3400


def test_a_missing_speaker_model_falls_back_and_says_so(client, ollama, meeting):
    meeting["transcript"]["models"]["speaker_model"] = "llava:13b"
    out = events(client.post("/api/meetings/transcripts/t1/identify", json={}))
    notice = next(e for e in out if e.get("kind") == "model_fallback")
    assert "llava:13b isn't installed" in notice["text"] and "gemma4:e4b" in notice["text"]
    assert out[-1]["type"] == "done" and chat_models(ollama["requests"]) == ["gemma4:e4b"]


def test_summary_model_is_kept_for_the_meeting(client, ollama, meeting, monkeypatch):
    monkeypatch.setattr(main.meeting_notes, "generate", lambda meta, *a, **k: {
        "notes": {}, "meeting_type": "general", "detected_type": "general", "warnings": [], "follow_up": "",
        "valid": True, "attempts": 1, "parts": 1})
    monkeypatch.setattr(main.meeting_notes, "render", lambda *a, **k: "# Notes")
    out = events(client.post("/api/meetings/transcripts/t1/summary", json={"model_id": "cloud:gaia",
                                                                            "summary_model": "local:qwen3:8b"}))
    assert out[-1]["type"] == "done" and out[-1]["summary"]["model_id"] == "local:qwen3:8b"
    again = events(client.post("/api/meetings/transcripts/t1/summary", json={"model_id": "cloud:gaia"}))
    assert again[-1]["summary"]["model_id"] == "local:qwen3:8b"  # the re-run keeps the meeting's model
    asks = events(client.post("/api/meetings/transcripts/t1/summary", json={"model_id": "local:gemma4:e4b",
                                                                             "summary_model": ""}))
    assert [e["type"] for e in asks] == ["choose_model"]  # no choice left: ask, never the chat model
    client.put("/api/preferences", json={"summary_model": "cloud:gaia"})
    settings = events(client.post("/api/meetings/transcripts/t1/summary", json={"model_id": "local:gemma4:e4b"}))
    assert settings[-1]["type"] == "confirm"  # the Settings choice (cloud) is used, and asks before sending
    client.put("/api/preferences", json={"summary_model": ""})
    missing = events(client.post("/api/meetings/transcripts/t1/summary", json={"summary_model": "local:llava:13b"}))
    assert any(e.get("kind") == "model_fallback" for e in missing) and missing[-1]["type"] == "done"


def test_local_steps_wait_for_a_running_transcription(client, ollama, meeting, monkeypatch):
    states = [[{"status": "running"}], [{"status": "running"}], []]
    monkeypatch.setattr(meetings, "jobs", lambda: states.pop(0) if len(states) > 1 else states[0])
    monkeypatch.setattr(main, "TRANSCRIBER_POLL", 0)
    out = events(client.post("/api/meetings/transcripts/t1/identify", json={}))
    waits = [e for e in out if "Waiting for the running transcription" in e.get("text", "")]
    assert len(waits) == 1 and out[-1]["type"] == "done" and not states[:-1]


# --- After transcription ------------------------------------------------------------------------

def run_followup(client, monkeypatch, meeting, job_states, summary_model=""):
    monkeypatch.setattr(main.FollowUps, "POLL", 0)
    # like the transcriber: the job's model choices end up in the transcript
    monkeypatch.setattr(meetings, "start", lambda path, speakers=None, screen=None, models=None:
                        meeting["transcript"].update(models=models) or {"id": "job1"})
    monkeypatch.setattr(meetings, "job", lambda job_id: job_states.pop(0) if len(job_states) > 1 else job_states[0])
    monkeypatch.setattr(main.meeting_notes, "generate", lambda meta, *a, **k: {
        "notes": {}, "meeting_type": "general", "detected_type": "general", "warnings": [], "follow_up": "",
        "valid": True, "attempts": 1, "parts": 1})
    monkeypatch.setattr(main.meeting_notes, "render", lambda *a, **k: "# Notes")
    job = client.post("/api/meetings/jobs", json={"path": "/x/call.mov", "after": True, "speaker_model": "qwen3-vl:4b-instruct",
                                                  "summary_model": summary_model, "model_id": "local:gemma4:e4b"}).json()
    assert job["followup"]["state"] == "waiting"
    main.FOLLOWUPS.wait("job1")
    return main.FOLLOWUPS.get("job1")


def test_identify_and_summarise_when_the_transcription_finishes(client, ollama, meeting, monkeypatch):
    done = run_followup(client, monkeypatch, meeting, [{"status": "running"}, {"status": "done", "transcript_id": "t1"}],
                        summary_model="local:gemma4:e4b")
    assert done["state"] == "done" and done["transcript_id"] == "t1"
    assert done["speaker_model_used"] == "qwen3-vl:4b-instruct"
    assert any(r.get("keep_alive") == 0 and r["model"] == "qwen3-vl:4b-instruct" for r in ollama["requests"])
    view = client.get("/api/meetings/transcripts/t1").json()
    assert view["followup"]["state"] == "done" and view["labels"]["SPEAKER_01"]["name"] == "Owen"
    assert view["summary"]["model_id"] == "local:gemma4:e4b"
    assert client.get("/api/meetings/jobs/job1").json()["followup"]["state"] == "done"


def test_without_a_summary_model_the_summary_waits_for_a_choice(client, ollama, meeting, monkeypatch):
    done = run_followup(client, monkeypatch, meeting, [{"status": "done", "transcript_id": "t1"}])
    assert done["state"] == "needs_model" and "Choose which model" in done["message"]
    view = client.get("/api/meetings/transcripts/t1").json()
    assert view["labels"]["SPEAKER_01"]["name"] == "Owen" and view["summary"] is None  # speakers done, no summary


def test_a_cloud_summary_model_waits_for_ok_after_transcription(client, ollama, meeting, monkeypatch):
    done = run_followup(client, monkeypatch, meeting, [{"status": "done", "transcript_id": "t1"}], summary_model="cloud:gaia")
    assert done["state"] == "needs_ok" and "cloud model" in done["message"]
    assert client.get("/api/meetings/transcripts/t1").json()["summary"] is None  # nothing was sent


def test_nothing_runs_after_a_failed_transcription(client, ollama, meeting, monkeypatch):
    done = run_followup(client, monkeypatch, meeting, [{"status": "error"}])
    assert done["state"] == "cancelled" and chat_models(ollama["requests"]) == []


def test_unfinished_follow_ups_resume_when_the_api_starts(client, ollama, meeting, monkeypatch):
    monkeypatch.setattr(main.FollowUps, "POLL", 0)
    monkeypatch.setattr(meetings, "job", lambda job_id: {"status": "done", "transcript_id": "t1"})
    monkeypatch.setattr(main.meeting_notes, "generate", lambda meta, *a, **k: {
        "notes": {}, "meeting_type": "general", "detected_type": "general", "warnings": [], "follow_up": "",
        "valid": True, "attempts": 1, "parts": 1})
    monkeypatch.setattr(main.meeting_notes, "render", lambda *a, **k: "# Notes")
    meeting["transcript"]["models"]["summary_model"] = "local:gemma4:e4b"
    path = main.meeting_models_dir() / "jobs" / "job9.json"
    main._write_json(path, {"job_id": "job9", "state": "waiting", "step": "transcription", "chat_model": "local:gemma4:e4b"})
    runner = main.FollowUps()
    with patch.object(main, "FOLLOWUPS", runner):
        runner.resume()
        runner.wait("job9")
    assert runner.get("job9")["state"] == "done"


def test_forgetting_a_meetings_models_goes_back_to_settings(client, ollama, meeting):
    events(client.post("/api/meetings/transcripts/t1/identify", json={"speaker_model": "qwen3:8b"}))
    assert client.get("/api/meetings/transcripts/t1").json()["models"]["speaker_model"] == "qwen3:8b"
    assert client.delete("/api/meetings/transcripts/t1/models").json() == {"ok": True}
    assert client.get("/api/meetings/transcripts/t1").json()["models"]["speaker_model"] == "qwen3-vl:4b-instruct"
