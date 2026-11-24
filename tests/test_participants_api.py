"""The participants list in the app: speaker names checked against it at the end of identification (Keep / Revert,
reverted stays reverted), attendees kept in step with the speakers (your edits win), the list as evidence for a
speaker, the cloud review's spelling fixes, your edits of the transcript (new speakers, the voice question, Undo),
and deleting one meeting's voice fingerprints. Synthetic meetings and fakes only."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import attendees as att
import cloud_providers as cp
import llm_backends as lb
import meetings
import participants_check as pc
import people as people_store
import speaker_id as sid
import speaker_review as sr
from api import main
from tests import fake_cloud
from tests.fake_cloud import FakeCloud
from tests.test_api import client, events, ollama_reply  # noqa: F401  (fixtures)
from transcriber import edits as transcript_edits

ROSTER = ["Ciarán Byrne", "Owen Hart", "Priya Sharma", "Lorraine Connolly", "Sam Lee"]


def seg(spk, text, start):
    return {"speaker": spk, "text": text, "start": float(start), "end": float(start + 4)}


def meeting() -> dict:
    return {
        "id": "fedcba9876543210", "title": "CRQ runbook", "speakers": ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02", "SPEAKER_03"],
        "segments": [seg("SPEAKER_00", "Kieran here, rolling the NAT changes now.", 0),
                     seg("SPEAKER_01", "Thanks Kieran. Owen, are the routes ready?", 5),
                     seg("SPEAKER_02", "Bob says the routes are ready.", 10),
                     seg("SPEAKER_03", "I'll update the runbook.", 15)],
        "speaker_embeddings": {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]},
        "participants": {"status": "done", "names": [
            {"name": n, "frames": 40, "first_seen": float(i), "last_seen": 900.0, "spoke": 0} for i, n in enumerate(ROSTER)],
            "invited": ["Mei Lin Chen"], "frames_scanned": 900, "frames_with_list": 600},
    }


LOCAL = {"SPEAKER_00": {"name": "Kieran", "role": "", "confidence": 0.7, "level": "medium", "evidence": ["said 'Kieran here'"],
                        "source": "auto"},
         "SPEAKER_01": {"name": "Owen", "role": "", "confidence": 0.6, "level": "medium", "evidence": [], "source": "auto"},
         "SPEAKER_02": {"name": "Bob", "role": "", "confidence": 0.6, "level": "medium", "evidence": [], "source": "auto"},
         "SPEAKER_03": {"name": "", "role": "", "confidence": 0.0, "level": "low", "evidence": [], "source": "auto"}}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_the_check_takes_the_lists_spelling_flags_strangers_and_suggests_the_rest():
    labels = dict(LOCAL, SPEAKER_04={"name": "Priya", "source": "user", "confidence": 1.0})
    out, result = pc.check(labels, ROSTER)
    assert out["SPEAKER_00"]["name"] == "Ciarán Byrne" and out["SPEAKER_00"]["previous"]["name"] == "Kieran"
    assert "sounds like Ciarán Byrne" in out["SPEAKER_00"]["evidence"][0]
    assert out["SPEAKER_01"]["name"] == "Owen Hart" and out["SPEAKER_01"]["source"] == "participants"
    assert out["SPEAKER_02"]["flag"] == "not_in_list" and out["SPEAKER_02"]["confidence"] == 0.42
    assert out["SPEAKER_04"] == labels["SPEAKER_04"]  # a name you set is never changed
    assert out["SPEAKER_03"]["suggestions"] == ["Priya Sharma", "Lorraine Connolly", "Sam Lee"]
    assert [c["to"] for c in result["changes"]] == ["Ciarán Byrne", "Owen Hart"] and result["flagged"][0]["name"] == "Bob"
    assert pc.revert(out, "SPEAKER_00")["SPEAKER_00"]["name"] == "Kieran"
    twice, _ = pc.check({"A": {"name": "Owen", "confidence": 0.9}, "B": {"name": "Owen H", "confidence": 0.5}}, ROSTER)
    assert twice["A"]["name"] == "Owen Hart" and twice["B"]["name"] == "Owen H"  # one listed name, one speaker
    # a speaker already named exactly as listed keeps it, even when less sure: no two speakers get one name
    exact, why = pc.check({"A": {"name": "Owen", "confidence": 0.9}, "B": {"name": "Owen Hart", "confidence": 0.4}}, ROSTER)
    assert exact["A"]["name"] == "Owen" and exact["B"]["name"] == "Owen Hart" and why["taken"][0]["by"] == "B"


def test_attendees_link_speakers_keep_listeners_and_follow_your_edits():
    t = meeting()
    labels = {"SPEAKER_00": {"name": "Ciarán Byrne"}, "SPEAKER_01": {"name": "Owen"}, "SPEAKER_02": {"name": "Bob"}}
    view = att.build(t, labels, {"added": ["Tara Quinn"], "removed": ["Lorraine Connolly"], "renamed": {}})
    rows = {r["name"]: r for r in view["attendees"]}
    assert rows["Ciarán Byrne"]["speaker"] == "SPEAKER_00" and rows["Owen Hart"]["speaker_name"] == "Owen"
    assert rows["Bob"]["source"] == "speaker" and rows["Tara Quinn"]["source"] == "you"
    assert not rows["Sam Lee"]["spoke"] and "Lorraine Connolly" not in rows and view["invited"] == ["Mei Lin Chen"]
    with pytest.raises(ValueError, match="spoke in this meeting"):
        att.edit(att.empty_edits(), view["attendees"], "remove", "Bob")
    edits = att.follow_speaker_rename(att.empty_edits(), view["attendees"], "SPEAKER_00", "Ciaran B.")
    assert edits["renamed"] == {"Ciarán Byrne": "Ciaran B."}
    renamed = att.build(t, {**labels, "SPEAKER_00": {"name": "Ciaran B."}}, edits)
    assert next(r for r in renamed["attendees"] if r["speaker"] == "SPEAKER_00")["name"] == "Ciaran B."


def test_the_list_is_evidence_and_agreeing_with_the_tile_makes_it_strong():
    t = {"screen_names": {
        "SPEAKER_00": {"names": [{"name": "Priya Sharma", "count": 1, "ocr": 1, "list": 12, "both": 1, "times": [3]}],
                       "frames": 3, "resolved": 1, "list_frames": 12},
        "SPEAKER_01": {"names": [{"name": "Owen Hart", "count": 0, "ocr": 0, "list": 8, "both": 0, "times": []}],
                       "frames": 3, "resolved": 0, "list_frames": 9}}}
    found = sid.screen_evidence(t, [])
    p, why, info = found["SPEAKER_00"]["Priya Sharma"]
    assert p >= sid.SCREEN_BOTH and "tile and list agree (1×)" in why and info["list"] == 12
    p2, why2, _ = found["SPEAKER_01"]["Owen Hart"]
    assert p2 == sid.LIST_STRONG and why2 == "marked as speaking in the participants list (8 frames)"


def test_spelling_fixes_are_checked_line_by_line():
    t = dict(meeting(), confirmed_speakers={"SPEAKER_03": {"source": "microphone"}})
    reply = json.dumps({"fixes": [
        {"line": 1, "text": "Ciarán here, rolling the NAT changes now.", "reason": "misheard name"},
        {"line": 2, "text": "Thanks Ciarán. Owen, all routes are ready and tested?", "reason": "tidier"},
        {"line": 4, "text": "I'll update the run book.", "reason": "spelling"},
        {"line": 3, "text": "Bob says the roots are ready.", "reason": ""}]})
    found = sr.spelling(t, {}, ROSTER, lambda m: reply)
    assert found["fixes"] == [{"line": 0, "before": "Kieran here, rolling the NAT changes now.",
                               "text": "Ciarán here, rolling the NAT changes now.", "reason": "misheard name"}]
    why = {r["line"]: r["why"] for r in found["rejected"]}
    assert why[3] == "your microphone's line" and why[2] == "no reason given" and "adds or drops" in why[1]


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------

@pytest.fixture
def api(client, monkeypatch):  # noqa: F811
    state = {"t": meeting(), "names": {}, "model_changes": [], "forgot": []}
    base = [dict(s) for s in state["t"]["segments"]]
    ops: list[dict] = []

    def transcript(tid):
        segs, _origin, st = transcript_edits.apply(base, ops)
        speakers = list(dict.fromkeys(s["speaker"] for s in segs))
        return dict(state["t"], segments=segs, speakers=speakers, speaker_names=dict(state["names"]),
                    edits={"ops": list(ops), "state": st} if ops else None)

    def add_edit(tid, edit):
        from fastapi.testclient import TestClient  # noqa: F401  (the transcriber's checks are in its own tests)
        current = transcript(tid)["segments"]
        i = edit["lines"][0]
        op = {"id": transcript_edits.new_id(), "kind": edit["kind"]}
        if edit["kind"] == "speaker":
            op.update(at=[current[j]["start"] for j in edit["lines"]], i=edit["lines"], speaker=edit["speaker"])
        elif edit["kind"] == "text":
            op.update(at=current[i]["start"], i=i, text=edit["text"], before=current[i]["text"])
        ops.append(op)
        return {"op": op}

    def undo_edit(tid, op_id=None):
        ops[:] = [o for o in ops if op_id and o["id"] != op_id]
        return {}

    monkeypatch.setattr(meetings, "transcript", transcript)
    monkeypatch.setattr(meetings, "transcripts", lambda: [{"id": state["t"]["id"], "speaker_names": {}}])
    monkeypatch.setattr(meetings, "set_speaker_names", lambda tid, names: state["names"].update(names) or {})
    monkeypatch.setattr(meetings, "add_edit", add_edit)
    monkeypatch.setattr(meetings, "undo_edit", undo_edit)
    monkeypatch.setattr(meetings, "correction_model",
                        lambda tid, model, label, changes, source="review": state["model_changes"].append(
                            (source, label, changes)) or {"changes": [], "names": {}, "refs": []})
    monkeypatch.setattr(meetings, "forget_voiceprints", lambda tid: state["forgot"].append(tid) or {"deleted": 2})
    monkeypatch.setattr(sid, "identify", lambda *a, **k: {k2: dict(v) for k2, v in LOCAL.items()})
    with FakeCloud() as url:
        monkeypatch.setenv("ANTHROPIC_PROVIDER_URL", url)
        cp.add_provider("anthropic", key="sk-ant-test")
        cp.test_provider("anthropic")
        cp.update_provider("anthropic", {"enabled": ["claude-opus-5-5"]})
        fake_cloud.REQUESTS.clear()
        yield client, state


URL = "/api/meetings/transcripts/fedcba9876543210"


def identify(client, **body):  # noqa: F811
    with patch.object(lb.requests, "post", return_value=ollama_reply(json.dumps({"speakers": []}))):
        return events(client.post(f"{URL}/identify", json=body))


def test_identify_ends_with_the_list_check_and_a_reverted_change_stays_reverted(api):
    client, _state = api  # noqa: F811
    out = identify(client, model_id="local:gemma4:e4b")
    assert any("Checked against the participants list: 2 names changed, 1 not on the list" in e.get("text", "") for e in out)
    view = client.get(URL).json()
    assert view["labels"]["SPEAKER_00"]["name"] == "Ciarán Byrne" and view["display_names"]["SPEAKER_01"] == "Owen Hart"
    assert [c["state"] for c in view["participants_check"]["changes"]] == ["pending", "pending"]
    rows = {r["name"]: r for r in view["attendees"]["attendees"]}
    assert rows["Ciarán Byrne"]["speaker"] == "SPEAKER_00" and rows["Bob"]["source"] == "speaker"
    reverted = client.post(f"{URL}/speakers/participants/revert", json={"speaker": "SPEAKER_00"}).json()
    assert reverted["labels"]["SPEAKER_00"]["name"] == "Kieran"
    client.post(f"{URL}/speakers/participants/keep", json={"speaker": "SPEAKER_01"})
    identify(client, model_id="local:gemma4:e4b")
    again = client.get(URL).json()
    assert again["labels"]["SPEAKER_00"]["name"] == "Kieran"  # you reverted it: it stays reverted
    assert {c["speaker"]: c["state"] for c in again["participants_check"]["changes"]} == {
        "SPEAKER_00": "reverted", "SPEAKER_01": "kept"}


def test_attendees_can_be_added_removed_and_renamed_and_speakers_follow(api):
    client, _state = api  # noqa: F811
    identify(client, model_id="local:gemma4:e4b")
    r = client.post(f"{URL}/attendees", json={"action": "add", "name": "Tara Quinn"})
    assert r.status_code == 200, r.text
    assert "Tara Quinn" in att.names(r.json()["attendees"])
    assert client.post(f"{URL}/attendees", json={"action": "remove", "name": "Bob"}).status_code == 409
    r = client.post(f"{URL}/attendees", json={"action": "remove", "name": "Sam Lee"})
    assert "Sam Lee" not in att.names(r.json()["attendees"])
    r = client.post(f"{URL}/attendees", json={"action": "rename", "name": "Owen Hart", "to": "Owen Harte"}).json()
    assert r["labels"]["SPEAKER_01"]["name"] == "Owen Harte" and r["labels"]["SPEAKER_01"]["source"] == "user"
    assert "Owen Harte" in att.names(r["attendees"]) and "Owen Hart" not in att.names(r["attendees"])
    # renaming the speaker renames the listed attendee it was linked to
    client.put(f"{URL}/speakers", json={"labels": {"SPEAKER_00": {"name": "C. Byrne"}}})
    names = att.names(client.get(URL).json()["attendees"])
    assert "C. Byrne" in names and "Ciarán Byrne" not in names


def test_the_cloud_review_fixes_spelling_first_and_saves_it_as_a_correction_layer(api):
    client, state = api  # noqa: F811
    client.put("/api/preferences", json={"confirm_cloud_sensitive": False})
    fake_cloud.SPELLING_PLAN["fixes"] = {"Kieran": "Ciarán"}
    out = identify(client, model_id="cloud:anthropic:claude-opus-5-5")
    source, label, changes = state["model_changes"][0]
    assert source == "cloud_review" and label == "Claude Opus 5.5"
    assert [c["line"] for c in changes] == [0, 1] and changes[0]["text"].startswith("Ciarán here")
    assert out[-1]["review"]["spelling"]["fixes"] == 2
    systems = [json.dumps(r["body"].get("system")) for r in fake_cloud.REQUESTS if r["api"] == "anthropic"]
    assert "proofread" in systems[0] and "review speaker identification" in systems[1]  # spelling first


def test_your_edits_create_speakers_ask_about_the_voice_and_undo(api):
    client, _state = api  # noqa: F811
    identify(client, model_id="local:gemma4:e4b")
    main._write_json(main.SUMMARIES_DIR / "fedcba9876543210.json", {"markdown": "notes", "created_at": "2026-01-01"})
    r = client.post(f"{URL}/edits", json={"kind": "speaker", "lines": [2], "name": "Sam Lee"}).json()
    assert r["segments"][2]["speaker"] == "SPEAKER_U01" and r["labels"]["SPEAKER_U01"]["source"] == "user"
    assert r["display_names"]["SPEAKER_U01"] == "Sam Lee" and r["voice_question"] == {"speaker": "SPEAKER_U01", "name": "Sam Lee"}
    assert r["edits"][0]["text"] == "1 line now said by Sam Lee" and r["summary"]["stale"] is True
    rows = {x["name"]: x for x in r["attendees"]["attendees"]}
    assert rows["Sam Lee"]["speaker"] == "SPEAKER_U01" and rows["Sam Lee"]["spoke"]
    same = client.post(f"{URL}/edits", json={"kind": "speaker", "lines": [3], "name": "sam lee"}).json()
    assert same["segments"][3]["speaker"] == "SPEAKER_U01"  # an existing speaker with that name
    done = client.delete(f"{URL}/edits").json()
    assert done["edits"] == [] and "SPEAKER_U01" not in done["labels"]
    assert client.post(f"{URL}/edits", json={"kind": "speaker", "lines": [0]}).status_code == 400


def test_one_meetings_voice_fingerprints_can_be_deleted(api):
    client, state = api  # noqa: F811
    people_store.learn("fedcba9876543210", "SPEAKER_00", "Ciarán Byrne", "", [1.0, 0.0])
    people_store.learn("otherotherother0", "SPEAKER_00", "Ciarán Byrne", "", [1.0, 0.0])
    r = client.delete(f"{URL}/voiceprints").json()
    assert r["deleted"] == 1 and state["forgot"] == ["fedcba9876543210"]
    person = next(p for p in people_store.load() if p["name"] == "Ciarán Byrne")
    assert [v["tid"] for v in person["voiceprints"]] == ["otherotherother0"]


def test_meeting_notes_list_attendees_who_didnt_speak():
    import meeting_notes

    t = dict(meeting(), attendees=[{"name": "Ciarán Byrne", "speaker": "SPEAKER_00"}, {"name": "Sam Lee", "speaker": None},
                                   {"name": "Tara Quinn", "speaker": None}])
    meta = meeting_notes.meeting_meta(t, {"SPEAKER_00": "Ciarán Byrne", "SPEAKER_01": "Owen Hart", "SPEAKER_02": "Bob",
                                          "SPEAKER_03": "Speaker 4"}, {})
    names = [a["name"] for a in meta["attendees"]]
    assert names[:2] == ["Ciarán Byrne", "Owen Hart"] and names[-2:] == ["Sam Lee", "Tara Quinn"]
    assert next(a for a in meta["attendees"] if a["name"] == "Sam Lee")["spoke"] is False
