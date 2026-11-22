#!/usr/bin/env python3
"""Speaker names/roles with confidence from voice, conversation clues and the model's reading."""

import json

import people as people_store
import speaker_id as sid


def seg(spk, text, start):
    return {"speaker": spk, "text": text, "start": start, "end": start + 4}


MEETING = {
    "segments": [
        seg("SPEAKER_00", "Morning all. Owen, can you give us the test update?", 0),
        seg("SPEAKER_01", "Sure. Regression passed on Android, two bugs left on iOS payments.", 5),
        seg("SPEAKER_00", "Thanks, Owen. Mai, anything on the API?", 10),
        seg("SPEAKER_02", "The refund endpoint fix is merged, I'll deploy it to staging today.", 15),
        seg("SPEAKER_00", "Great. Owen, please retest refunds after Mai's deploy. Due Friday.", 20),
    ],
    "speakers": ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"],
    "speaker_embeddings": {"SPEAKER_00": [1.0, 0.0, 0.0], "SPEAKER_01": [0.0, 1.0, 0.0],
                           "SPEAKER_02": [0.0, 0.0, 1.0]},
}


def test_conversation_clues_name_the_person_who_answers_not_the_asker():
    votes, why = sid.text_clues(MEETING["segments"], ["Owen", "Mai"])
    assert votes["SPEAKER_01"]["Owen"] > 0 and votes["SPEAKER_00"]["Owen"] < 0
    assert votes["SPEAKER_02"]["Mai"] > 0
    assert any("Owen" in w for w in why["SPEAKER_01"])


def test_voice_match_with_people_memory_gives_high_confidence():
    known = [{"name": "Priya", "role": "Delivery manager", "meetings": ["old"],
              "voiceprints": [{"tid": "old", "speaker": "SPEAKER_03", "vector": [0.97, 0.1, 0.05]}]}]
    labels = sid.identify(MEETING, known, [], complete=None)
    assert labels["SPEAKER_00"]["name"] == "Priya" and labels["SPEAKER_00"]["level"] == "high"
    assert labels["SPEAKER_00"]["role"] == "Delivery manager"
    assert any("voice" in e for e in labels["SPEAKER_00"]["evidence"])


def test_model_reading_combined_with_clues_and_roles():
    replies = []

    def complete(messages):
        replies.append(messages[-1]["content"])
        return json.dumps({"speakers": [
            {"id": "SPEAKER_00", "name": None, "role": "Meeting lead", "confidence": 0, "evidence": "assigns tasks"},
            {"id": "SPEAKER_01", "name": "Owen", "role": "QA tester", "confidence": 0.8, "evidence": "reports test results"},
            {"id": "SPEAKER_02", "name": "Mai", "role": "Backend developer", "confidence": 0.7, "evidence": "merged a fix"},
        ]})

    labels = sid.identify(MEETING, [], ["Owen works in QA"], complete)
    prompt = replies[0]
    assert "Names mentioned in the meeting: " in prompt and "Owen" in prompt and "Owen works in QA" in prompt
    assert labels["SPEAKER_01"]["name"] == "Owen" and labels["SPEAKER_01"]["role"] == "QA tester"
    assert labels["SPEAKER_01"]["confidence"] > labels["SPEAKER_02"]["confidence"] * 0.8
    assert labels["SPEAKER_01"]["level"] in ("medium", "high")
    assert labels["SPEAKER_00"]["name"] == "" and labels["SPEAKER_00"]["role"] == "Meeting lead"


def test_model_alone_cannot_invent_confident_names_or_reuse_one():
    def complete(_messages):
        return json.dumps({"speakers": [
            {"id": "SPEAKER_00", "name": "Zed", "role": "", "confidence": 0.95, "evidence": "guess"},
            {"id": "SPEAKER_01", "name": "Owen", "confidence": 0.9},
            {"id": "SPEAKER_02", "name": "Owen", "confidence": 0.9}]})

    labels = sid.identify(MEETING, [], [], complete)
    assert labels["SPEAKER_00"]["name"] == ""  # "Zed" appears nowhere: not trusted
    names = [l["name"] for l in labels.values() if l["name"]]
    assert names.count("Owen") == 1


def test_confirmed_labels_are_kept():
    keep = {"SPEAKER_02": {"name": "Mai Tran", "role": "Dev", "confidence": 1.0, "level": "confirmed",
                           "source": "user", "evidence": []}}
    labels = sid.identify(MEETING, [], [], None, keep=keep)
    assert labels["SPEAKER_02"]["name"] == "Mai Tran" and labels["SPEAKER_02"]["source"] == "user"


def test_corrections_teach_people_memory_and_can_be_revised(tmp_path):
    path = tmp_path / "people.json"
    people_store.learn("t1", "SPEAKER_01", "Owen", "QA", [0.0, 1.0, 0.0], path=path)
    owen = people_store.find_by_name(people_store.load(path), "owen")
    assert owen["role"] == "QA" and owen["meetings"] == ["t1"] and len(owen["voiceprints"]) == 1
    # The user changes their mind: the voice from that speaker now belongs to Sam, not Owen.
    people_store.learn("t1", "SPEAKER_01", "Sam", "", [0.0, 1.0, 0.0], path=path)
    known = people_store.load(path)
    assert people_store.find_by_name(known, "Owen")["voiceprints"] == []
    assert len(people_store.find_by_name(known, "Sam")["voiceprints"]) == 1
    labels = sid.identify(MEETING, known, [], None)
    assert labels["SPEAKER_01"]["name"] == "Sam"


def test_parse_reply_tolerates_prose_and_bad_values():
    reply = 'Sure! {"speakers": [{"id": "SPEAKER_00", "name": "null", "confidence": "x"}, {"id": "BAD"}]}'
    assert sid.parse_reply(reply, ["SPEAKER_00"]) == {
        "SPEAKER_00": {"name": None, "role": "", "confidence": 0.0, "evidence": ""}}


def test_memory_role_match_needs_a_distinctive_word():
    known = [{"name": "Cian Byrne", "role": "release manager", "projects": [], "team": ""},
             {"name": "Mai Tran", "role": "backend developer", "projects": ["Atlas"], "team": ""}]
    llm = {"S0": {"role": "Delivery manager"}, "S1": {"role": "Backend developer"}}
    texts = {"S0": "Morning all", "S1": "The Atlas refund fix is merged"}
    out = sid.memory_matches(["S0", "S1"], texts, llm, known, {"cian byrne", "mai tran"})
    assert "Cian Byrne" not in out.get("S0", {})
    probs, evidence = out["S1"]["Mai Tran"]
    assert evidence[0] == "Matched to Mai Tran: backend developer in memory" and len(probs) == 2  # role + project


def test_model_first_name_maps_to_the_known_full_name():
    transcript = {"segments": [{"speaker": "S0", "start": 0, "end": 1, "text": "Morning all, let's start."}]}
    known = [{"name": "Priya Shah", "role": "Mobile QA lead", "aliases": [], "voiceprints": []}]
    reply = json.dumps({"speakers": [{"id": "S0", "name": "Priya", "role": "QA lead", "confidence": 0.6}]})
    labels = sid.identify(transcript, known, [], lambda _m: reply)
    assert labels["S0"]["name"] == "Priya Shah"
