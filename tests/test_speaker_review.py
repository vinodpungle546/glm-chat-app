"""On-screen names as evidence, automatic voice fingerprints, and the cloud review of speaker
names (unit level and through the API). Synthetic meetings; cloud calls go to tests/fake_cloud."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import cloud_providers as cp
import llm_backends as lb
import meetings
import people as people_store
import speaker_id as sid
import speaker_review as sr
from tests import fake_cloud
from tests.fake_cloud import FakeCloud
from tests.test_api import client, events, ollama_reply  # noqa: F401  (fixtures)


def seg(spk, text, start):
    return {"speaker": spk, "text": text, "start": start, "end": start + 4}


MEETING = {
    "id": "0123456789abcdef", "title": "Release sync", "speakers": ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"],
    "segments": [seg("SPEAKER_00", "Morning all, let's start the release sync.", 0),
                 seg("SPEAKER_01", "Hi, Daniel here. Payments regression is green.", 5),
                 seg("SPEAKER_02", "I've merged the refund fix.", 10),
                 seg("SPEAKER_00", "Thanks Daniel. Mei, deploy today?", 15),
                 seg("SPEAKER_02", "Yes, after lunch.", 20)],
    "speaker_embeddings": {"SPEAKER_00": [1.0, 0.0, 0.0], "SPEAKER_01": [0.0, 1.0, 0.0], "SPEAKER_02": [0.0, 0.0, 1.0]},
    "screen_names": {
        "SPEAKER_00": {"names": [{"name": "Priya Sharma", "count": 4, "times": [1.5, 16.5], "truncated": False,
                                  "ocr": 4, "speaker_view": 0, "vision": 0}], "frames": 4, "resolved": 4},
        "SPEAKER_01": {"names": [{"name": "Daniel Okaf", "count": 1, "times": [6.5], "truncated": True,
                                  "ocr": 1, "speaker_view": 0, "vision": 0}], "frames": 2, "resolved": 1},
        "SPEAKER_02": {"names": [{"name": "Mei Lin Chen", "count": 2, "times": [11.5, 21.5], "truncated": False,
                                  "ocr": 0, "speaker_view": 0, "vision": 2}], "frames": 2, "resolved": 2}},
}


# ---------------------------------------------------------------------------
# On-screen evidence in the local pass
# ---------------------------------------------------------------------------

def test_screen_evidence_strength_and_why():
    found = sid.screen_evidence(MEETING, ["Daniel Okafor"])
    p, why, info = found["SPEAKER_00"]["Priya Sharma"]
    assert p == sid.SCREEN_STRONG and why == "name on the active tile at 0:01 (seen 4×)"
    assert "Daniel Okafor" in found["SPEAKER_01"]  # truncated label resolved to the known full name
    assert found["SPEAKER_01"]["Daniel Okafor"][0] == sid.SCREEN_ONCE
    p_vision, why_vision, _ = found["SPEAKER_02"]["Mei Lin Chen"]
    assert p_vision <= sid.SCREEN_VISION_CAP and why_vision.startswith("name read by the local vision model")


def test_screen_names_name_speakers_without_a_model():
    labels = sid.identify(MEETING, [], [], complete=None)
    assert labels["SPEAKER_00"]["name"] == "Priya Sharma" and labels["SPEAKER_00"]["level"] == "high"
    assert labels["SPEAKER_00"]["evidence"][0].startswith("name on the active tile")
    assert labels["SPEAKER_02"]["name"] == "Mei Lin Chen"


def test_only_consistent_text_read_names_save_voices():
    assert sid.screen_confirmed(MEETING) == {"SPEAKER_00": ("Priya Sharma", 4)}  # vision-only and seen-once don't


def test_same_name_on_two_speakers_is_weakened():
    meeting = json.loads(json.dumps(MEETING))
    meeting["screen_names"]["SPEAKER_01"]["names"] = [dict(meeting["screen_names"]["SPEAKER_00"]["names"][0], count=2)]
    found = sid.screen_evidence(meeting, [])
    assert found["SPEAKER_01"]["Priya Sharma"][0] < found["SPEAKER_00"]["Priya Sharma"][0]
    assert sid.screen_confirmed(meeting) == {}


# ---------------------------------------------------------------------------
# The review itself
# ---------------------------------------------------------------------------

LOCAL = {"SPEAKER_00": {"name": "Priya Sharma", "role": "Lead", "confidence": 0.9, "level": "high",
                        "evidence": ["name on the active tile at 0:01 (seen 4×)"], "source": "auto"},
         "SPEAKER_01": {"name": "", "role": "QA", "confidence": 0.0, "level": "low", "evidence": [], "source": "auto"},
         "SPEAKER_02": {"name": "Mai", "role": "Developer", "confidence": 1.0, "level": "confirmed",
                        "evidence": ["confirmed by you"], "source": "user"}}
KNOWN = [{"name": "Daniel Okafor", "aliases": [], "role": "QA lead", "team": "Payments", "projects": ["Atlas"],
          "voiceprints": [{"vector": [0.1, 0.2]}], "notes": "private note"},
         {"name": "Zed Quill", "role": "CFO", "aliases": [], "voiceprints": []}]


def reply_with(rows):
    return lambda messages: json.dumps({"speakers": rows})


def test_request_contains_only_what_the_review_needs():
    seen = []
    sr.review(MEETING, LOCAL, KNOWN, lambda m: seen.append(m) or json.dumps({"speakers": []}))
    prompt = seen[0][1]["content"]
    assert "#2 [00:05] SPEAKER_01: Hi, Daniel here." in prompt
    assert "Priya Sharma (seen 4×)" in prompt and '"name": "Daniel Okafor"' in prompt
    assert "Zed Quill" not in prompt          # not relevant to this meeting
    assert "private note" not in prompt and "voiceprints" not in prompt and "0.1" not in prompt
    assert "speaker_embeddings" not in prompt and '"id": "SPEAKER_02"' not in prompt.split("Speakers the user named")[0]


def test_changes_need_supporting_lines_and_user_labels_never_change():
    result = sr.review(MEETING, LOCAL, KNOWN, reply_with([
        {"id": "SPEAKER_01", "action": "change", "name": "Daniel Okafor", "role": "QA lead", "confidence": 0.85,
         "reason": "Says 'Daniel here'.", "lines": [2]},
        {"id": "SPEAKER_00", "action": "change", "name": "Zed Quill", "confidence": 0.9, "reason": "guess", "lines": []},
        {"id": "SPEAKER_02", "action": "change", "name": "Someone", "confidence": 0.9, "reason": "x", "lines": [3]}]))
    assert [c["speaker"] for c in result["changes"]] == ["SPEAKER_01"]
    change = result["changes"][0]
    assert change["from"]["name"] == "" and change["name"] == "Daniel Okafor" and change["lines"] == [2]
    assert result["rejected"][0]["why"] == "no transcript line supports it"
    labels = sr.apply(LOCAL, result, "Claude Opus 5.5")
    assert labels["SPEAKER_01"]["source"] == "cloud" and labels["SPEAKER_01"]["previous"]["role"] == "QA"
    assert labels["SPEAKER_01"]["evidence"][0] == "Claude Opus 5.5: Says 'Daniel here'."
    assert labels["SPEAKER_02"] == LOCAL["SPEAKER_02"]
    assert sr.revert(labels, "SPEAKER_01")["SPEAKER_01"] == LOCAL["SPEAKER_01"]


def test_a_change_matching_the_on_screen_name_needs_no_line():
    result = sr.review(MEETING, LOCAL, KNOWN, reply_with([
        {"id": "SPEAKER_00", "action": "change", "name": "Priya Sharma", "role": "Engineering manager",
         "confidence": 0.8, "reason": "Runs the meeting.", "lines": []}]))
    assert result["changes"][0]["on_screen"] and result["changes"][0]["role"] == "Engineering manager"


def test_bad_json_is_retried_once_then_fails():
    calls = []

    def complete(messages):
        calls.append(messages)
        return "not json" if len(calls) == 1 else json.dumps({"speakers": [{"id": "SPEAKER_01", "action": "keep"}]})

    assert sr.review(MEETING, LOCAL, KNOWN, complete)["agreed"] == 2
    assert "no JSON object" in calls[1][-1]["content"]
    with pytest.raises(ValueError):
        sr.review(MEETING, LOCAL, KNOWN, lambda m: "still not json")


def test_long_transcripts_are_split_and_merged_per_speaker():
    meeting = dict(MEETING, segments=[seg("SPEAKER_01", f"Line {i} " + "words " * 30, i * 5) for i in range(40)])
    parts = []

    def complete(messages):
        parts.append(messages)
        n = len(parts)
        return json.dumps({"speakers": [{"id": "SPEAKER_01", "action": "change", "name": "Daniel Okafor",
                                         "confidence": 0.6, "reason": f"part {n}", "lines": [n]}]})

    result = sr.review(meeting, {}, KNOWN, complete, budget_chars=2000)
    assert result["parts"] == len(parts) > 1
    assert result["changes"][0]["lines"] == list(range(1, len(parts) + 1))


# ---------------------------------------------------------------------------
# Through the API: routing, Keep/Revert, fingerprints, failures
# ---------------------------------------------------------------------------

@pytest.fixture
def meeting_api(client, monkeypatch):  # noqa: F811
    store = {"names": {}}
    monkeypatch.setattr(meetings, "transcript", lambda tid: dict(MEETING, speaker_names=dict(store["names"])))
    monkeypatch.setattr(meetings, "transcripts", lambda: [{"id": MEETING["id"], "speaker_names": {}}])
    monkeypatch.setattr(meetings, "set_speaker_names", lambda tid, names: store["names"].update(names) or {})
    with FakeCloud() as url:
        monkeypatch.setenv("ANTHROPIC_PROVIDER_URL", url)
        cp.add_provider("anthropic", key="sk-ant-test")
        cp.test_provider("anthropic")
        cp.update_provider("anthropic", {"enabled": ["claude-opus-5-5"]})
        fake_cloud.REQUESTS.clear()
        fake_cloud.REVIEW_PLAN["changes"] = {"SPEAKER_01": {"name": "Daniel Okafor", "role": "QA lead", "lines": [2],
                                                            "reason": "Introduces himself as Daniel."}}
        yield client


URL = f"/api/meetings/transcripts/{MEETING['id']}"
LOCAL_READING = ollama_reply(json.dumps({"speakers": []}))


def identify(client, **body):
    with patch.object(lb.requests, "post", return_value=LOCAL_READING):
        return events(client.post(f"{URL}/identify", json=body))


def test_local_model_selected_means_no_review(meeting_api):
    out = identify(meeting_api, model_id="local:gemma4:e4b")
    assert out[-1]["type"] == "done" and out[-1]["review"] is None
    assert fake_cloud.REQUESTS == []


def test_cloud_mode_reviews_without_asking_and_tells_once(meeting_api):
    meeting_api.put("/api/preferences", json={"confirm_cloud_sensitive": False})
    out = identify(meeting_api, model_id="cloud:anthropic:claude-opus-5-5")
    done = out[-1]
    assert done["review"]["status"] == "done" and done["review"]["model"] == "Claude Opus 5.5"
    assert done["labels"]["SPEAKER_01"]["name"] == "Daniel Okafor" and done["labels"]["SPEAKER_01"]["source"] == "cloud"
    assert sum(e["type"] == "notice" and e.get("kind") == "privacy" for e in out) == 1
    body = json.dumps(fake_cloud.REQUESTS[-1]["body"])
    assert "Hi, Daniel here" in body and "speaker_embeddings" not in body and "1.0, 0.0" not in body
    again = identify(meeting_api, model_id="cloud:anthropic:claude-opus-5-5")
    assert not any(e.get("kind") == "privacy" for e in again)
    view = meeting_api.get(URL).json()
    assert view["review"]["changes"][0]["state"] == "pending"


def test_auto_mode_asks_first_then_reviews_on_confirm(meeting_api):
    out = identify(meeting_api, model_id="cloud:anthropic:claude-opus-5-5")
    kinds = [e["type"] for e in out]
    assert kinds[-1] == "confirm" and "labels" in kinds and fake_cloud.REQUESTS == []
    assert out[-1]["stage"] == "speaker_review" and "Claude Opus 5.5" in out[-1]["question"]
    confirmed = identify(meeting_api, model_id="cloud:anthropic:claude-opus-5-5", confirm="cloud", review_only=True)
    assert confirmed[-1]["review"]["changes"][0]["name"] == "Daniel Okafor"
    assert len([r for r in fake_cloud.REQUESTS if r["api"] == "anthropic"]) == 1


def test_choosing_local_keeps_local_names(meeting_api):
    out = identify(meeting_api, model_id="cloud:anthropic:claude-opus-5-5", confirm="local", review_only=True)
    assert out[-1]["review"] is None and fake_cloud.REQUESTS == []


def test_confidential_project_skips_the_review(meeting_api):
    project = meeting_api.get("/api/projects").json()[0]
    meeting_api.patch(f"/api/projects/{project['id']}", json={"default_model_mode": "local"})
    meeting_api.put("/api/preferences", json={"confirm_cloud_sensitive": False})
    out = identify(meeting_api, model_id="cloud:anthropic:claude-opus-5-5")
    assert any(e.get("kind") == "review_skipped" for e in out) and fake_cloud.REQUESTS == []


def test_keep_saves_the_voice_and_revert_restores(meeting_api):
    meeting_api.put("/api/preferences", json={"confirm_cloud_sensitive": False})
    identify(meeting_api, model_id="cloud:anthropic:claude-opus-5-5")
    reverted = meeting_api.post(f"{URL}/speakers/review/revert", json={"speaker": "SPEAKER_01"}).json()
    assert reverted["labels"]["SPEAKER_01"]["source"] == "auto" and reverted["review"]["changes"][0]["state"] == "reverted"
    assert meeting_api.post(f"{URL}/speakers/review/keep", json={"speaker": "SPEAKER_01"}).status_code == 404

    identify(meeting_api, model_id="cloud:anthropic:claude-opus-5-5")
    daniel = people_store.find_by_name(people_store.load(), "Daniel Okafor")
    assert daniel is None or not daniel.get("voiceprints")  # nothing learned before Keep
    kept = meeting_api.post(f"{URL}/speakers/review/keep", json={"speaker": "SPEAKER_01"}).json()
    assert kept["review"]["changes"][0]["state"] == "kept" and kept["labels"]["SPEAKER_01"]["cloud"]["kept"]
    daniel = people_store.find_by_name(people_store.load(), "Daniel Okafor")
    assert daniel["voiceprints"][-1]["source"] == "cloud_review" and daniel["role"] == "QA lead"
    entry = next(e for e in meeting_api.get("/api/memory").json()["entries"] if e["name"] == "Daniel Okafor")
    assert entry["facts"][0]["source"] == "cloud_review"
    # next time the local pass knows the voice, and the cloud model has nothing to change
    again = identify(meeting_api, model_id="cloud:anthropic:claude-opus-5-5")
    assert again[-1]["labels"]["SPEAKER_01"]["name"] == "Daniel Okafor" and again[-1]["review"]["changes"] == []


def test_revert_all(meeting_api):
    fake_cloud.REVIEW_PLAN["changes"]["SPEAKER_00"] = {"name": "Priya Sharma", "role": "Engineering manager", "lines": [1]}
    meeting_api.put("/api/preferences", json={"confirm_cloud_sensitive": False})
    out = identify(meeting_api, model_id="cloud:anthropic:claude-opus-5-5")
    assert len(out[-1]["review"]["changes"]) == 2
    reverted = meeting_api.post(f"{URL}/speakers/review/revert-all", json={}).json()
    assert {c["state"] for c in reverted["review"]["changes"]} == {"reverted"}
    assert all(lab["source"] == "auto" for lab in reverted["labels"].values())


def test_user_labels_are_never_sent_for_change(meeting_api):
    meeting_api.put("/api/preferences", json={"confirm_cloud_sensitive": False})
    meeting_api.put(f"{URL}/speakers", json={"labels": {"SPEAKER_01": {"name": "Dan", "role": "QA"}}})
    out = identify(meeting_api, model_id="cloud:anthropic:claude-opus-5-5")
    assert out[-1]["labels"]["SPEAKER_01"]["name"] == "Dan" and out[-1]["review"]["changes"] == []


def test_failure_keeps_local_names(meeting_api):
    meeting_api.put("/api/preferences", json={"confirm_cloud_sensitive": False})
    cp.update_provider("anthropic", {"key": "wrong-key"})
    out = identify(meeting_api, model_id="cloud:anthropic:claude-opus-5-5")
    assert any(e.get("kind") == "review_failed" and "rejected" in e["text"] for e in out)
    assert out[-1]["review"]["status"] == "failed" and out[-1]["labels"]["SPEAKER_01"]["source"] == "auto"


def test_on_screen_names_save_voices_automatically_unless_switched_off(meeting_api):
    identify(meeting_api, model_id="local:gemma4:e4b")
    priya = people_store.find_by_name(people_store.load(), "Priya Sharma")
    assert priya["voiceprints"][0]["source"] == "screen" and priya["screen_names"][0]["count"] == 4
    assert people_store.find_by_name(people_store.load(), "Mei Lin Chen") is None  # vision-only: not saved
    meeting_api.delete(f"/api/people/{priya['id']}/voice")
    meeting_api.put("/api/preferences", json={"voiceprints": False})
    identify(meeting_api, model_id="local:gemma4:e4b")
    assert people_store.find_by_name(people_store.load(), "Priya Sharma")["voiceprints"] == []
