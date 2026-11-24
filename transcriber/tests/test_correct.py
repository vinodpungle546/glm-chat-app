"""Correcting a transcript with other transcripts of the same meeting (transcriber/correct.py). Synthetic meeting
only: a script, Quorum's version with Whisper-style mistakes, and references (Voice Memos text and audio, a
claude.ai transcript, a VTT with names) each with mistakes of their own.

    transcriber/.venv/bin/python -m pytest transcriber/tests/test_correct.py -q
"""

import pytest

from transcriber import correct

SCRIPT = [
    ("Priya Sharma", "Good morning everyone, let's start the release sync for Atlas."),
    ("Owen Hart", "Thanks Priya. The build is green and QA finished the regression run yesterday."),
    ("You", "Great. Owen, can we still release on the 14th as planned?"),
    ("Owen Hart", "Yes, the 14th works if Mei signs off the security review by Thursday."),
    ("Priya Sharma", "Mei is away until Wednesday, so the sign-off may slip to Friday."),
    ("You", "Then I will raise it with the stakeholders and move the go/no-go call to Monday."),
    ("Owen Hart", "The vendor API is still rate limiting us in staging, about eighty requests per minute."),
    ("Priya Sharma", "Let's open a Jira ticket for the rate limit and ask the vendor for a higher quota."),
    ("You", "I'll own the ticket. Anything else for the release checklist?"),
    ("Owen Hart", "Only the rollback plan, which Priya is writing this week."),
    ("Priya Sharma", "I'll share the rollback plan on Tuesday so everyone can review it before Monday."),
    ("You", "Perfect, thanks both. Let's meet again on Monday at ten for the go/no-go decision."),
]
SPEAKERS = {"Priya Sharma": "SPEAKER_00", "Owen Hart": "SPEAKER_01", "You": "SPEAKER_02"}
WORD_SECONDS = 0.4


def build(script, mistakes=None, start=0.0):
    """Segments (and Whisper-like words with times) for `script`, with `mistakes` {(line, word index): new}."""
    mistakes = mistakes or {}
    segments, words, t = [], [], start
    for li, (who, text) in enumerate(script):
        parts = text.split()
        out = []
        seg_start = t
        for wi, part in enumerate(parts):
            new = mistakes.get((li, wi), part)
            if new:
                out.append(new)
                words.append({"word": " " + new, "start": round(t, 2), "end": round(t + 0.35, 2),
                              "p": 0.95 if (li, wi) not in mistakes else 0.3})
            t += WORD_SECONDS
        segments.append({"speaker": SPEAKERS.get(who, "SPEAKER_09"), "start": round(seg_start, 2), "end": round(t, 2),
                         "text": " ".join(out)})
        t += 1.0
    return segments, words


def text_ref(rid, source, script, mistakes=None, names=False):
    segs, _ = build(script, mistakes)
    if names:
        lines = [f"{who}: {s['text']}" for (who, _), s in zip(script, segs)]
    else:
        lines = [s["text"] for s in segs]
    from transcriber import text_import
    parsed, timed = text_import.parse("\n".join(lines))
    return {"id": rid, "name": rid, "source": source, "recording": source, "segments": parsed, "timed": timed}


def audio_ref(rid, script, mistakes=None, start=40.0):
    _, words = build(script, mistakes, start=start)
    return {"id": rid, "name": rid, "source": "voice_memos", "recording": "voice_memos",
            "words": [{"word": w["word"], "start": w["start"], "end": w["end"]} for w in words]}


# Quorum's mistakes (Whisper on the call): names, a number, a term, plus "Thank you." invented at the end.
QUORUM_MISTAKES = {(1, 1): "Pria.", (2, 8): "40th", (7, 3): "Gira", (3, 5): "Mae", (6, 11): "eighteen"}


def quorum(low_confidence=True):
    segs, words = build(SCRIPT, QUORUM_MISTAKES)
    segs.append({"speaker": "SPEAKER_01", "start": segs[-1]["end"] + 5, "end": segs[-1]["end"] + 6,
                 "text": "Thank you."})
    if not low_confidence:
        for w in words:
            w["p"] = 0.95
    return segs, words


def text_of(result):
    return " ".join(s["text"] for s in result["segments"])


def test_nothing_extra_changes_nothing():
    segs, words = quorum()
    out = correct.correct(segs, [], words)
    assert out["segments"] == segs and out["changes"] == [] and out["stats"]["words"] == 0


def test_only_a_claude_transcript_fixes_well_aligned_words():
    segs, words = quorum()
    claude = text_ref("claude", "claude", SCRIPT, {(5, 3): "rays"})  # its own mistake
    out = correct.correct(segs, [claude], words)
    text = text_of(out)
    assert "Thanks Priya." in text and "on the 14th as planned" in text and "Jira ticket" in text
    assert "if Mei signs" in text and "about eighty requests" in text
    assert "will raise it" in text  # Quorum was right; a single reference's mistake on a sure word loses
    assert out["refs"][0]["applied"] and out["refs"][0]["coverage"] > 0.8
    assert {c["kind"] for c in out["changes"]} <= {"word", "delete", "insert"}


def test_only_voice_memos_audio_is_lined_up_in_time():
    segs, words = quorum()
    memo = audio_ref("memo audio", SCRIPT, {(4, 0): "May"}, start=40.0)  # memo started 40 s before the call
    out = correct.correct(segs, [memo], words)
    ref = out["refs"][0]
    assert ref["kind"] == "audio" and abs(ref["offset"] + 40) < 1.5
    assert "Thanks Priya." in text_of(out) and "Mei is away" in text_of(out)  # Quorum sure there: kept


def test_a_single_reference_never_overrides_a_word_whisper_was_sure_of():
    segs, words = quorum(low_confidence=False)
    claude = text_ref("claude", "claude", SCRIPT)
    out = correct.correct(segs, [claude], words)
    assert "Pria." in text_of(out) and "40th" in text_of(out)  # sure (p 0.95): a 1-1 tie keeps Quorum


def test_voice_memos_text_and_audio_are_one_vote():
    segs, words = quorum(low_confidence=False)
    memo_text = text_ref("memo text", "voice_memos", SCRIPT)
    memo_audio = audio_ref("memo audio", SCRIPT)
    out = correct.correct(segs, [memo_text, memo_audio], words)
    assert "Pria." in text_of(out)  # both agree, but Whisper was sure: still a tie of one vote each
    segs, words = quorum(low_confidence=True)
    out = correct.correct(segs, [memo_text, memo_audio], words)
    assert "Thanks Priya." in text_of(out)
    assert sorted(out["changes"][0]["sources"]) in (["memo audio", "memo text"], ["memo audio"], ["memo text"])


def test_two_recordings_outvote_quorum_and_quorum_plus_one_outvotes_a_reference():
    segs, words = quorum(low_confidence=False)
    claude = text_ref("claude", "claude", SCRIPT, {(5, 3): "rays", (9, 1): "a"})
    memo = text_ref("memo text", "voice_memos", SCRIPT, {(9, 1): "a"})
    out = correct.correct(segs, [claude, memo], words)
    text = text_of(out)
    assert "Thanks Priya." in text and "the 14th as planned" in text  # 2 against 1, even where Whisper was sure
    assert "will raise it" in text  # claude alone was wrong: Quorum + memo keep it
    assert "Only a rollback" in text  # both references say "a": 2 against 1


def test_the_order_sources_are_added_in_doesnt_matter():
    segs, words = quorum()
    refs = [text_ref("claude", "claude", SCRIPT, {(5, 3): "rays"}),
            text_ref("memo text", "voice_memos", SCRIPT, {(8, 2): "on"}),
            audio_ref("memo audio", SCRIPT, {(4, 0): "May"})]
    a = correct.correct(segs, refs, words)
    b = correct.correct(segs, refs[::-1], words)
    assert a["segments"] == b["segments"] and [c["id"] for c in a["changes"]] == [c["id"] for c in b["changes"]]


def test_a_reference_covering_half_the_meeting_corrects_only_that_half():
    segs, words = quorum()
    half = text_ref("claude", "claude", SCRIPT[6:])
    out = correct.correct(segs, [half], words)
    text = text_of(out)
    assert "Thanks Pria." in text and "40th" in text  # first half untouched
    assert "Jira ticket" in text and "eighty requests" in text
    assert out["refs"][0]["share"] < 0.6


def test_unrelated_text_is_not_applied():
    segs, words = quorum()
    other = [("Someone", "The quarterly marketing budget review covers paid search, events and the new brand "
                         "guidelines that the design team prepared for the spring campaign launch in April."),
             ("Else", "We should also revisit agency fees and the influencer program before the board meeting.")]
    out = correct.correct(segs, [text_ref("wrong", "other", other)], words)
    assert out["refs"][0]["applied"] is False and out["refs"][0]["reason"] == "doesn't match this meeting"
    assert out["segments"] == segs


def test_invented_thank_you_is_removed_where_a_reference_covers_it():
    segs, words = quorum()
    pause = {"speaker": "SPEAKER_01", "start": segs[5]["end"] + 0.5, "end": segs[5]["end"] + 1, "text": "Thank you."}
    middle = segs[:6] + [pause] + segs[6:]
    out = correct.correct(middle, [text_ref("claude", "claude", SCRIPT)], words)
    assert [s["text"] for s in out["segments"]].count("Thank you.") == 1  # the one at the very end isn't covered
    assert any(c["kind"] == "delete" and c["from"] == "Thank you." for c in out["changes"])
    # A real "thank you" that the reference also has stays.
    out = correct.correct(segs, [text_ref("claude", "claude", SCRIPT)], words)
    assert "Perfect, thanks both." in text_of(out)


def test_names_from_a_vtt_and_a_turn_boundary_moved_but_never_the_microphone():
    segs, words = quorum()
    # Quorum put the first two words of line 4 ("Mei is") on Owen's turn.
    moved = [dict(s) for s in segs]
    w = moved[4]["text"].split()
    moved[3]["text"] += " " + " ".join(w[:2])
    moved[4]["text"] = " ".join(w[2:])
    vtt = text_ref("teams", "other", SCRIPT, names=True)
    out = correct.correct(moved, [vtt], words, fixed_speakers={"SPEAKER_02"})
    assert out["names"]["SPEAKER_00"]["name"] == "Priya Sharma" and out["names"]["SPEAKER_01"]["name"] == "Owen Hart"
    turn = [c for c in out["changes"] if c["kind"] == "turn"]
    assert len(turn) == 1
    assert out["segments"][4]["text"].startswith("Mei is away") and out["segments"][3]["text"].endswith("Thursday.")
    # Your microphone's turns are never moved, even when a reference says otherwise.
    mic = [dict(s) for s in segs]
    w = mic[2]["text"].split()
    mic[1]["text"] += " " + w[0]
    mic[2]["text"] = " ".join(w[1:])
    out = correct.correct(mic, [vtt], words, fixed_speakers={"SPEAKER_02"})
    assert not [c for c in out["changes"] if c["kind"] == "turn"]


def test_known_names_win_a_tie():
    segs, words = quorum(low_confidence=False)
    memo = text_ref("memo", "voice_memos", SCRIPT)
    claude = text_ref("claude", "claude", SCRIPT, {(3, 5): "Mae"})  # agrees with Quorum's misspelling
    out = correct.correct(segs, [memo, claude], words, known_terms=["Mei Lin", "Priya Sharma"])
    assert "if Mei signs" not in text_of(out)  # Quorum + claude 2 vs memo 1
    out = correct.correct(segs, [memo], words, known_terms=["Mei Lin"])
    assert "if Mei signs" in text_of(out)  # 1 vs 1, but "Mei" is a known name


def test_revert_one_change_and_undo_everything():
    segs, words = quorum()
    claude = text_ref("claude", "claude", SCRIPT)
    out = correct.correct(segs, [claude], words)
    first = out["changes"][0]
    again = correct.correct(segs, [claude], words, reverted={first["id"]})
    assert [c["id"] for c in again["changes"] if c.get("reverted")] == [first["id"]]  # listed, to bring back
    assert len([c for c in again["changes"] if not c.get("reverted")]) == len(out["changes"]) - 1
    every = correct.correct(segs, [claude], words, reverted={c["id"] for c in out["changes"]})
    assert every["segments"] == segs and every["stats"]["words"] == 0


def test_marks_point_at_the_corrected_words():
    segs, words = quorum()
    out = correct.correct(segs, [text_ref("claude", "claude", SCRIPT)], words)
    for index, spans in out["marks"].items():
        text = out["segments"][int(index)]["text"]
        for start, end, cid in spans:
            change = next(c for c in out["changes"] if c["id"] == cid)
            assert text[start:end].strip(".,") .lower() in change["to"].lower() or change["to"].lower() in text[start:end].lower()


@pytest.mark.parametrize("old,new,expected", [("pria.", "Priya", "Priya."), ("Gira", "jira", "Jira"),
                                              ("(40th,", "14th", "(14th,")])
def test_punctuation_and_capitals_are_kept(old, new, expected):
    assert correct._restyle(new, old) == expected


def test_the_first_and_last_words_of_the_meeting_are_corrected_too():
    segs, words = build(SCRIPT, {(0, 0): "Could", (11, 13): "discussion."})
    out = correct.correct(segs, [text_ref("claude", "claude", SCRIPT)], words)
    assert out["segments"][0]["text"].startswith("Good morning")
    assert out["segments"][-1]["text"].endswith("go/no-go decision.")


def test_whisper_word_pieces_are_joined():
    ref = {"words": [{"word": " the", "start": 1.0, "p": 0.9}, {"word": " go", "start": 1.2, "p": 0.9},
                     {"word": "-no", "start": 1.3, "p": 0.7}, {"word": "-go", "start": 1.4, "p": 0.95}]}
    tokens = correct.reference_tokens(ref)
    assert [(t["text"], t["p"]) for t in tokens] == [("the", 0.9), ("go-no-go", 0.7)]


# ---------------------------------------------------------------------------
# Who said what, and whole sentences
# ---------------------------------------------------------------------------

def test_a_line_said_by_someone_else_moves_to_their_speaker_but_never_to_or_from_the_microphone():
    segs, words = quorum()
    segs[3]["speaker"] = "SPEAKER_00"   # Owen's "Yes, the 14th works…" was given to Priya
    segs[5]["speaker"] = "SPEAKER_00"   # your "Then I will raise it…" too (but you are the microphone)
    vtt = text_ref("teams", "other", SCRIPT, names=True)
    out = correct.correct(segs, [vtt], words, fixed_speakers={"SPEAKER_02"})
    assert out["segments"][3]["speaker"] == "SPEAKER_01"
    moved = [c for c in out["changes"] if c["kind"] == "speaker"]
    assert [(c["from"], c["to"], c["label"], c["seg"]) for c in moved] == [("Priya Sharma", "Owen Hart", "SPEAKER_01", 3)]
    assert out["segments"][5]["speaker"] == "SPEAKER_00"  # never moved to the microphone speaker
    assert out["stats"]["speakers"] == 1
    sentence = next(s for s in out["sentences"] if s["seg"] == 3)
    assert sentence["speaker_from"] == "Priya Sharma" and sentence["speaker_to"] == "Owen Hart"
    # Reverting it puts the line back.
    again = correct.correct(segs, [vtt], words, fixed_speakers={"SPEAKER_02"}, reverted={moved[0]["id"]})
    assert again["segments"][3]["speaker"] == "SPEAKER_00"
    assert any(c["id"] == moved[0]["id"] and c.get("reverted") for c in again["changes"])


def test_missing_words_inside_a_well_aligned_sentence_are_restored():
    drop = {(7, k): "" for k in range(9, 14)}  # "and ask the vendor for" missing
    segs, words = build(SCRIPT, {**QUORUM_MISTAKES, **drop})
    claude = text_ref("claude", "claude", SCRIPT)
    out = correct.correct(segs, [claude], words)
    assert "the rate limit and ask the vendor for a higher quota." in text_of(out)
    insert = next(c for c in out["changes"] if c["kind"] == "insert")
    assert insert["to"] == "and ask the vendor for"


def test_an_extra_word_is_removed_from_a_well_aligned_sentence():
    segs, words = build(SCRIPT, {**QUORUM_MISTAKES, (1, 2): "The the"})
    claude = text_ref("claude", "claude", SCRIPT)
    out = correct.correct(segs, [claude], words)
    assert "Priya. The build is green" in text_of(out) and "The the" not in text_of(out)


def test_sentences_whisper_merged_or_split_follow_the_reference():
    segs, words = build(SCRIPT, {**QUORUM_MISTAKES, (8, 3): "ticket", (8, 4): "anything",
                                 (9, 3): "plan.", (9, 4): "Which"})
    claude = text_ref("claude", "claude", SCRIPT)
    out = correct.correct(segs, [claude], words)
    text = text_of(out)
    assert "I'll own the ticket. Anything else" in text      # split again
    assert "Only the rollback plan, which Priya" in text      # joined again
    punct = [c for c in out["changes"] if c["kind"] == "punct"]
    assert len(punct) == 2 and out["stats"]["sentences"] == 2
    line = next(s for s in out["sentences"] if s["after"].startswith("I'll own"))
    assert line["before"] == "I'll own the ticket anything else for the release checklist?"
    # A second reference that keeps Quorum's punctuation stops a single one from changing it.
    other = text_ref("memo", "voice_memos", SCRIPT, {(8, 3): "ticket", (8, 4): "anything"})
    both = correct.correct(segs, [claude, other], words)
    assert "the ticket anything else" in text_of(both)
