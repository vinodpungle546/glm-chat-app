#!/usr/bin/env python3
"""Conversation text -> transcript (Teams/Zoom captions, chat logs, labelled notes)."""

import pytest

from transcriber import pipeline, text_import as ti


def names(t):
    return [t["speaker_names"].get(s["speaker"], s["speaker"]) for s in t["segments"]]


def test_teams_vtt_voice_tags_merge_consecutive_cues():
    vtt = ("WEBVTT\n\nab/1-0\n00:00:01.000 --> 00:00:04.500\n<v Vinod Pungle>Good morning.</v>\n\n"
           "ab/2-0\n00:00:04.500 --> 00:00:07.000\n<v Vinod Pungle>First the budget.</v>\n\n"
           "ab/3-0\n00:00:07.200 --> 00:00:10.000\n<v Priya Shah>Revenue grew 12%.</v>\n")
    t = ti.build_transcript("Budget", vtt, "budget.vtt")
    assert names(t) == ["Vinod Pungle", "Priya Shah"]
    assert t["segments"][0]["text"] == "Good morning. First the budget."
    assert t["segments"][1]["start"] == 7.2 and t["timestamps"] and t["diarization"]


def test_srt_and_zoom_style_name_prefix():
    srt = "1\n00:00:01,000 --> 00:00:03,000\nAnna: Can you hear me?\n\n2\n00:00:03,500 --> 00:00:05,000\nTom: Yes.\n"
    t = ti.build_transcript("", srt, "call.srt")
    assert names(t) == ["Anna", "Tom"] and t["title"] == "call"


def test_header_lines_like_teams_docx_export():
    text = ("Vinod Pungle   0:03\nGood morning.\n\nPriya Shah   0:15\nSure.\nWe should cut events.\n\n"
            "Vinod Pungle   1:02\nAgreed. The meeting starts at 10:00\n")
    t = ti.build_transcript("x", text)
    assert names(t) == ["Vinod Pungle", "Priya Shah", "Vinod Pungle"]
    assert t["segments"][1]["text"] == "Sure. We should cut events."
    assert t["segments"][2]["start"] == 62


def test_many_speakers_labelled_lines_without_times():
    people = ["Vinod", "Priya", "Raj", "Mai", "Anna", "Tom", "Kriya"]
    text = "Meeting notes\n" + "".join(f"{p}: point from {p}.\n" for p in people) + "Budget: still pending\n"
    t = ti.build_transcript("x", text)
    assert len([s for s in t["speakers"] if s != "UNKNOWN"]) == 7
    assert t["timestamps"] is False
    assert t["segments"][-1]["text"].endswith("Budget: still pending")  # a label, not a speaker
    md = pipeline.to_markdown(t)
    assert "Kriya: point from Kriya." in md and "[00:00:00]" not in md


def test_timestamped_lines_and_chat_export():
    t = ti.build_transcript("x", "[00:00:03] Vinod: Hello.\n00:01:02 - Raj: Morning.\n")
    assert [s["start"] for s in t["segments"]] == [3, 62]
    chat = "12/03/2024, 10:15 - Vinod: Did you send it?\n12/03/2024, 10:16 - Priya: Yes.\n"
    assert names(ti.build_transcript("x", chat)) == ["Vinod", "Priya"]


def test_plain_text_has_no_speakers():
    t = ti.build_transcript("x", "We discussed the roadmap.\nAll agreed.\n\nSecond paragraph.")
    assert t["diarization"] is False and len(t["segments"]) == 2
    assert "No speaker names found" in pipeline.to_markdown(t)


def test_reimport_of_our_own_markdown_keeps_generic_labels():
    t = ti.build_transcript("x", "[00:00:00] SPEAKER_00: Hi.\n[00:00:05] SPEAKER_01: Hello.\n")
    assert t["speakers"] == ["SPEAKER_00", "SPEAKER_01"] and t["speaker_names"] == {}


def test_empty_text_rejected():
    with pytest.raises(ValueError):
        ti.build_transcript("x", "  \n ")


def test_speaker_count_hints_for_pyannote():
    assert pipeline.speaker_kwargs(None, 1) == {}
    assert pipeline.speaker_kwargs({"num": 6}, 1) == {"num_speakers": 6}
    assert pipeline.speaker_kwargs({"num": 6}, 3) == {"max_speakers": 6}  # a piece may hear fewer
    assert pipeline.speaker_kwargs({"min": 2, "max": 8}, 1) == {"min_speakers": 2, "max_speakers": 8}
    assert pipeline.speaker_kwargs({"min": 2, "max": 8}, 2) == {"max_speakers": 8}
