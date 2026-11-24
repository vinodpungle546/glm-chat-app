#!/usr/bin/env python3
"""A model's review of a corrected transcript: only lines where the other transcripts differ are sent, every word
the model writes must be in the line or the other transcripts, speakers are real and never the microphone, one retry
on bad JSON; through the API: a local model runs, a cloud model asks first (once per meeting and provider) and a
project set to local refuses it. Synthetic data and fakes only."""

from __future__ import annotations

import json

import correction_review as cr
import meetings
import model_registry
from api import main
from tests.test_api import client, events  # noqa: F401  (client is a fixture)

LINES = [
    {"line": 0, "speaker": "SPEAKER_00", "text": "Good morning everyone.", "refs": [{"name": "memo", "text": "Good morning everyone."}]},
    {"line": 1, "speaker": "SPEAKER_00", "text": "The build green and QA finished.",
     "refs": [{"name": "memo", "text": "[Owen Hart:] The build is green and QA finished."}]},
    {"line": 2, "speaker": "SPEAKER_02", "text": "I will raise it.", "refs": [{"name": "memo", "text": "[Priya Sharma:] I will raise it."}]},
    {"line": 3, "speaker": "SPEAKER_01", "text": "No other source here.", "refs": []},
]
SPEAKERS = {"SPEAKER_00": "Priya Sharma", "SPEAKER_01": "Owen Hart", "SPEAKER_02": "You"}
FIXED = {"SPEAKER_02"}


def test_only_lines_the_other_transcripts_disagree_with_are_reviewed():
    assert [line["line"] for b in cr.batches(LINES) for line in b] == [1, 2]
    messages = cr.prompt(cr.batches(LINES)[0], SPEAKERS, FIXED)
    assert "#1 [SPEAKER_00] The build green" in messages[1]["content"]
    assert "SPEAKER_02: You (your microphone: never move lines to or from it)" in messages[1]["content"]
    assert "Never add facts, names or words that are in neither" in messages[0]["content"]


def test_validation_drops_invented_words_unknown_or_microphone_speakers_and_missing_reasons():
    batch = cr.batches(LINES)[0]
    valid, dropped = cr.validate([
        {"line": 1, "text": "The build is green and QA finished.", "speaker": "SPEAKER_01", "reason": "memo"},
        {"line": 1, "text": "duplicate", "reason": "twice"},
        {"line": 2, "speaker": "SPEAKER_00", "reason": "memo says Priya"},          # from the microphone
        {"line": 2, "text": "I will escalate it.", "reason": "better"},              # invented word
        {"line": 9, "text": "x", "reason": "y"},                                     # not asked
        {"line": "#1", "speaker": "SPEAKER_07", "reason": "z"},                     # unknown (and line 1 done)
        {"line": 2, "text": "I will raise it.", "reason": ""},                       # no change
    ], batch, SPEAKERS, FIXED)
    assert valid == [{"line": 1, "before": "The build green and QA finished.", "reason": "memo",
                      "text": "The build is green and QA finished.", "speaker": "SPEAKER_01", "speaker_name": "Owen Hart"}]
    assert any("microphone" in d for d in dropped) and any("escalate" in d for d in dropped)
    assert any("line 9" in d for d in dropped)
    rewrite, why_not = cr.validate([{"line": 1, "text": "QA is green.", "reason": "shorter"}], batch, SPEAKERS, FIXED)
    assert rewrite == [] and why_not == ["line 1: rewrites most of the line"]
    reason_less, why = cr.validate([{"line": 1, "text": "The build is green and QA finished."}], batch, SPEAKERS, FIXED)
    assert reason_less == [] and why == ["line 1: no reason given"]


def test_one_retry_when_the_reply_isnt_json():
    replies = iter(["Sure! Here are my thoughts.", json.dumps({"changes": [
        {"line": 1, "text": "The build is green and QA finished.", "reason": "memo"}]})])
    asked = []

    def complete(messages):
        asked.append(messages)
        return next(replies)

    valid, dropped = cr.review(LINES, SPEAKERS, FIXED, complete)
    assert len(asked) == 2 and "wasn't valid" in asked[1][-1]["content"]
    assert [c["line"] for c in valid] == [1] and dropped == []


def setup(monkeypatch, saved):
    monkeypatch.setattr(meetings, "correction_context", lambda tid: {
        "lines": LINES, "speakers": list(SPEAKERS), "fixed": sorted(FIXED)})
    monkeypatch.setattr(meetings, "transcript", lambda tid: {"id": tid, "speakers": list(SPEAKERS), "segments": [],
                                                            "speaker_names": {}, "correction": {"changes": []}})
    monkeypatch.setattr(meetings, "jobs", lambda: [])

    def save(tid, model, label, changes):
        saved.append({"model": model, "label": label, "changes": changes})
        return {"refs": [], "changes": [{"kind": "model"}], "names": {}, "stats": {"model": len(changes)}}

    monkeypatch.setattr(meetings, "correction_model", save)


def test_a_local_model_reviews_and_its_fixes_are_saved(client, monkeypatch):  # noqa: F811
    saved = []
    setup(monkeypatch, saved)
    reply = json.dumps({"changes": [{"line": 1, "text": "The build is green and QA finished.", "reason": "memo"},
                                    {"line": 2, "text": "We will escalate.", "reason": "invented"}]})
    monkeypatch.setattr(model_registry.OllamaProvider, "complete", lambda self, m, **k: reply)
    evs = events(client.post("/api/meetings/transcripts/abcdef0123456789/correction-review", json={}))
    done = evs[-1]
    assert done["type"] == "done" and done["changes"] == 1 and "words in neither transcript" in done["dropped"][0]
    assert saved[0]["model"] == "local:gemma4:e4b" and saved[0]["changes"][0]["line"] == 1
    assert any(e["type"] == "progress" for e in evs)


def test_a_cloud_model_asks_once_per_meeting_and_a_local_project_refuses_it(client, monkeypatch):  # noqa: F811
    saved, sent = [], []
    setup(monkeypatch, saved)
    monkeypatch.setattr(model_registry.GaiaProvider, "complete",
                        lambda self, m, **k: sent.append(m) or json.dumps({"changes": []}))
    url = "/api/meetings/transcripts/abcdef0123456789/correction-review"
    ask = events(client.post(url, json={"model_id": "cloud:gaia"}))
    assert ask[-1]["type"] == "confirm" and ask[-1]["stage"] == "corrections" and not sent
    assert "the other transcripts you added" in ask[-1]["sends"]
    events(client.post(url, json={"model_id": "cloud:gaia", "confirm": "cloud"}))
    assert sent and saved  # sent after the OK
    again = events(client.post(url, json={"model_id": "cloud:gaia"}))
    assert again[-1]["type"] == "done"  # this meeting was approved for this provider
    project = client.post("/api/projects", json={"name": "Confidential"}).json()
    client.patch(f"/api/projects/{project['id']}", json={"default_model_mode": "local"})
    client.put("/api/preferences", json={"active_project": project["id"]})
    refused = events(client.post(url, json={"model_id": "cloud:gaia", "confirm": "cloud"}))
    assert refused[-1]["type"] == "error" and refused[-1]["kind"] == "local_only"
    assert main.correction_review_clear  # the DELETE route exists
