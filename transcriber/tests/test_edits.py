"""Your own edits of a transcript (wording, who said a line, split, merge): applied last, after the corrections
from other transcripts and the models, so they always win; each can be undone; they find their line by its start
time after the corrections are recomputed. The cloud review's spelling fixes are a second model layer that works
without another transcript and can be kept or reverted. Synthetic meeting only.

    transcriber/.venv/bin/python -m pytest transcriber/tests/test_edits.py -q
"""

from transcriber import edits
from transcriber.tests.test_references import TID, claude_text, client, text  # noqa: F401  (client is a fixture)

SEGS = [{"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00", "text": "Good morning everyone."},
        {"start": 4.5, "end": 9.0, "speaker": "SPEAKER_01", "text": "The build is green and QA finished yesterday."},
        {"start": 9.5, "end": 12.0, "speaker": "SPEAKER_00", "text": "Great."}]


def test_operations_find_their_line_by_time_and_report_stale_ones():
    ops = [{"id": "a", "kind": "text", "at": 9.5, "i": 2, "text": "Great, thanks."},
           {"id": "b", "kind": "split", "at": 4.5, "i": 1, "parts": [
               {"text": "The build is green", "speaker": "SPEAKER_01", "start": 4.5, "end": 6.5},
               {"text": "and QA finished yesterday.", "speaker": "SPEAKER_02", "start": 6.5, "end": 9.0}]},
           {"id": "c", "kind": "speaker", "at": [0.0], "i": [0], "speaker": "SPEAKER_02"},
           {"id": "d", "kind": "text", "at": 99.0, "i": 7, "text": "gone"},
           {"id": "e", "kind": "merge", "at": [6.5, 9.5], "i": 2, "text": "and QA finished yesterday. Great, thanks.",
            "speaker": "SPEAKER_02"}]
    segs, origin, state = edits.apply(SEGS, ops)
    assert [(s["speaker"], s["text"]) for s in segs] == [
        ("SPEAKER_02", "Good morning everyone."), ("SPEAKER_01", "The build is green"),
        ("SPEAKER_02", "and QA finished yesterday. Great, thanks.")]
    assert state == {"a": "applied", "b": "applied", "c": "applied", "d": "stale", "e": "applied"}
    assert origin == [0, None, None] and all(s["edited"] for s in segs)
    assert edits.split_times(SEGS[1], 4) == 6.75  # in proportion to the words
    assert edits.describe(ops[2], {"SPEAKER_02": "Owen Hart"}) == "1 line now said by Owen Hart"


def test_your_edits_win_over_corrections_and_can_be_undone(client):
    c, _config, _pipeline, original = client
    line = next(i for i, s in enumerate(original) if s["speaker"] == "SPEAKER_00")
    r = c.post(f"/transcripts/{TID}/edits", json={"kind": "text", "lines": [line], "text": "My  own wording here."})
    assert r.status_code == 200 and r.json()["segments"][line]["text"] == "My own wording here."
    op = r.json()["op"]
    assert op["before"] == original[line]["text"] and r.json()["edits"]["state"][op["id"]] == "applied"
    # Another transcript corrects the meeting: your line keeps your words, the rest is corrected.
    correction = c.post(f"/transcripts/{TID}/references", json={"source": "claude", "text": claude_text()}).json()
    assert correction["stats"]["words"] >= 4
    after = c.get(f"/transcripts/{TID}").json()
    assert after["segments"][line]["text"] == "My own wording here." and "Jira ticket" in text(c)
    assert str(line) not in correction["marks"]
    # Reassign two lines to a new person (a label the app created), then undo one edit, then all.
    r = c.post(f"/transcripts/{TID}/edits", json={"kind": "speaker", "lines": [0, 1], "speaker": "SPEAKER_U01"})
    assert "SPEAKER_U01" in r.json()["speakers"] and r.json()["segments"][1]["speaker"] == "SPEAKER_U01"
    second = r.json()["op"]["id"]
    r = c.delete(f"/transcripts/{TID}/edits/{op['id']}")
    assert r.json()["segments"][line]["text"] != "My own wording here." and len(r.json()["edits"]["ops"]) == 1
    assert c.delete(f"/transcripts/{TID}/edits/nope").status_code == 404
    r = c.delete(f"/transcripts/{TID}/edits")
    assert r.json()["edits"]["ops"] == [] and "SPEAKER_U01" not in r.json()["speakers"] and second


def test_split_and_merge_check_the_words_and_the_lines(client):
    c, _config, _pipeline, original = client
    words = original[0]["text"].split()
    bad = c.post(f"/transcripts/{TID}/edits", json={"kind": "split", "lines": [0], "parts": [
        {"text": "invented words", "speaker": "SPEAKER_00"}, {"text": "here", "speaker": "SPEAKER_01"}]})
    assert bad.status_code == 400
    r = c.post(f"/transcripts/{TID}/edits", json={"kind": "split", "lines": [0], "parts": [
        {"text": " ".join(words[:2]), "speaker": "SPEAKER_00"}, {"text": " ".join(words[2:]), "speaker": "SPEAKER_01"}]})
    segs = r.json()["segments"]
    assert len(segs) == len(original) + 1 and segs[1]["speaker"] == "SPEAKER_01"
    assert segs[0]["end"] == segs[1]["start"] and segs[1]["end"] == original[0]["end"]
    r = c.post(f"/transcripts/{TID}/edits", json={"kind": "merge", "lines": [0]})
    assert r.json()["segments"][0]["text"] == original[0]["text"] and len(r.json()["segments"]) == len(original)
    assert c.post(f"/transcripts/{TID}/edits", json={"kind": "text", "lines": [999], "text": "x"}).status_code == 400
    assert c.post(f"/transcripts/{TID}/edits", json={"kind": "text", "lines": [0], "text": "  "}).status_code == 400


def test_cloud_spelling_fixes_need_no_other_transcript_and_can_be_kept_or_reverted(client):
    c, _config, _pipeline, original = client
    line = 1
    fixed = original[line]["text"].replace(original[line]["text"].split()[0], "Fixed", 1)
    body = {"model": "claude-opus-5-5", "label": "Claude Opus 5.5", "source": "cloud_review",
            "changes": [{"line": line, "before": original[line]["text"], "text": fixed, "reason": "spelling"}]}
    correction = c.post(f"/transcripts/{TID}/corrections/model", json=body).json()
    change = next(ch for ch in correction["changes"] if ch["kind"] == "model")
    assert change["sources"] == ["Claude Opus 5.5"] and correction["layers"]["cloud_review"]["changes"]
    assert c.get(f"/transcripts/{TID}").json()["segments"][line]["text"] == fixed
    kept = c.post(f"/transcripts/{TID}/corrections/{change['id']}/keep").json()
    assert kept["kept"] == [change["id"]]
    c.post(f"/transcripts/{TID}/corrections/{change['id']}/revert")
    assert c.get(f"/transcripts/{TID}").json()["segments"][line]["text"] == original[line]["text"]
    # a review layer and the cloud layer live side by side; dropping one keeps the other
    c.post(f"/transcripts/{TID}/references", json={"source": "claude", "text": claude_text()})
    c.post(f"/transcripts/{TID}/corrections/{change['id']}/restore")
    c.delete(f"/transcripts/{TID}/corrections/model", params={"source": "review"})
    assert "cloud_review" in c.get(f"/transcripts/{TID}").json()["correction"]["layers"]
    c.delete(f"/transcripts/{TID}/corrections/model", params={"source": "cloud_review"})
    assert c.get(f"/transcripts/{TID}").json()["correction"]["layers"] == {}
