#!/usr/bin/env python3
"""Guessing speakers from unlabelled text with the local model (model replies faked)."""

import speaker_guess as sg


def sentences(n=6):
    return [{"text": f"Sentence number {i}.", "start": float(i)} for i in range(n)]


def test_split_sentences_keeps_segment_start():
    out = sg.split_sentences([{"text": "Hi Lauren. Yes, I'm good! Are you?  ok then", "start": 5.0}])
    assert [s["text"] for s in out] == ["Hi Lauren.", "Yes, I'm good!", "Are you? ok then"]
    assert all(s["start"] == 5.0 for s in out)


def test_parts_cover_every_sentence_once():
    sents = [{"text": "x" * 500, "start": 0.0} for _ in range(20)]
    parts = sg.plan_parts(sents, part_chars=1600)
    covered = [i for a, b in parts for i in range(a, b)]
    assert covered == list(range(20)) and len(parts) > 1


def test_parse_script_rejects_sentence_starts_as_labels():
    reply = ("Hi everyone: can you hear me okay?\n**Raj:** Yes. Loud and clear.\n"
             "Hi Lauren: yes I can hear you too.\nspeaker 2: Will do.\n")
    assert sg.parse_script(reply) == [
        ("Speaker 1", "Hi everyone: can you hear me okay?"), ("Raj", "Yes."), ("Raj", "Loud and clear."),
        ("Raj", "Hi Lauren: yes I can hear you too."), ("Speaker 2", "Will do.")]


def test_align_keeps_original_words_and_tolerates_edits():
    sents = [{"text": t, "start": 0.0} for t in ["Hi everyone.", "Yes, loud and clear.", "Great stuff here.",
                                                   "Something the model dropped entirely."]]
    script = [("Lauren", "Hi everyone."), ("Raj", "Yes loud and clear"), ("Lauren", "Great stuff here!")]
    assert sg.align(sents, script, "Speaker 1") == ["Lauren", "Raj", "Lauren", "Lauren"]


def test_guess_carries_speakers_across_parts_and_builds_segments():
    sents = [{"text": f"Line {i} " + "x" * 300 + ".", "start": float(i * 10)} for i in range(12)]
    prompts = []

    def fake(messages):
        prompts.append(messages[1]["content"])
        part = messages[1]["content"].rsplit("Transcript: ", 1)[1].rsplit("\nScript:", 1)[0]
        lines = sg.split_sentences([{"text": part, "start": 0}])
        if len(prompts) == 1:
            return "\n".join(f"{'Speaker 1' if i < 2 else 'Lauren'}: {s['text']}" for i, s in enumerate(lines))
        return ""  # model gave nothing usable: the previous speaker continues

    labels = sg.guess(sents, fake, progress=None)
    assert len(prompts) > 1
    assert labels[:2] == ["Speaker 1", "Speaker 1"] and set(labels[2:]) == {"Lauren"}
    assert "Speakers found so far: Speaker 1, Lauren" in prompts[1]
    assert "Just before this part, Lauren said" in prompts[1]
    segments, names = sg.to_segments(sents, labels, timed=True)
    assert [s["speaker"] for s in segments] == ["SPEAKER_00", "SPEAKER_01"]
    assert names == {"SPEAKER_00": "Speaker 1", "SPEAKER_01": "Lauren"}
    assert segments[0]["end"] == segments[1]["start"] == 20.0


def test_addressed_person_is_not_the_speaker():
    sents = [{"text": t, "start": 0} for t in ["Great.", "Raj, can you start?", "Sure.", "Over to you, Mei."]]
    assert sg.fix_addressed(sents, ["Lauren", "Raj", "Raj", "Mei"]) == ["Lauren", "Lauren", "Raj", "Raj"]


def test_candidate_names_finds_people_not_sentence_starters():
    text = ("Yeah, morning. Hello. I know Sandra is still on another call. I am not sure about Lorraine. "
            "Hey, Lorraine. Hi, Iz. Owen, can you check Android? Thanks, Owen. Okay. yeah okay. "
            "The November release is in Ireland. Apple was down.")
    names = sg.candidate_names(text)
    assert {"Sandra", "Lorraine", "Owen", "Iz"} <= set(names)
    assert not {"Yeah", "Okay", "November", "Android", "Apple", "Ireland", "Hello"} & set(names)


def test_generic_speakers_get_names_from_clues():
    texts = ["Owen, can you give the update?", "Sure, the build is green.", "Thanks, Owen.",
             "Mai, anything from you?", "Nothing from me.", "Hi Mai, sorry I'm late.", "I'm Sam, from QA."]
    labels = ["Speaker 1", "Speaker 2", "Speaker 1", "Speaker 1", "Speaker 3", "Speaker 1", "Speaker 4"]
    sents = [{"text": t, "start": 0} for t in texts]
    named = sg.name_generic_speakers(sents, labels, ["Owen", "Mai", "Sam"])
    assert named == ["Speaker 1", "Owen", "Speaker 1", "Speaker 1", "Mai", "Speaker 1", "Sam"]


def test_prompt_lists_people_named_in_the_meeting():
    sents = [{"text": "Hey Lorraine.", "start": 0}]
    prompt = sg.build_prompt(sents, 0, 1, [], None, ["Lorraine", "Owen"])
    assert "People named in this meeting (not all of them may speak): Lorraine, Owen" in prompt
    assert "as few speakers" not in prompt


def test_example_names_copied_by_the_model_become_generic():
    labels = ["Lauren", "Tom", "Speaker 1", "Ben", "Tom"]
    assert sg.drop_example_names(labels, "Lauren and Raj talk.") == [
        "Lauren", "Speaker 2", "Speaker 1", "Speaker 3", "Speaker 2"]
    assert sg.drop_example_names(["Tom"], "Thanks Tom.") == ["Tom"]  # a real Tom stays


def test_untimed_text_gets_zero_times():
    segments, _ = sg.to_segments(sentences(3), ["A", "B", "C"], False)
    assert tuple(s["start"] for s in segments) == (0.0, 0.0, 0.0)


def test_greetings_are_not_names():
    found = sg.candidate_names("Welcome, Hana. I'm Tom, the hiring manager. Thanks Tom. Perfect, Hana.")
    assert "Welcome" not in found and "Perfect" not in found and {"Hana", "Tom"} <= set(found)
