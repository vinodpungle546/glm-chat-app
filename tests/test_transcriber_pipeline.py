#!/usr/bin/env python3
"""Pure helpers of the local transcriber (no Whisper/pyannote needed)."""

from transcriber import pipeline as p


def w(word, start, end):
    return {"word": word, "start": start, "end": end}


def test_format_timestamp():
    assert p.format_timestamp(0) == "00:00:00"
    assert p.format_timestamp(3725.9) == "01:02:05"


def test_plan_windows_covers_duration_without_gaps():
    windows = p.plan_windows(1530, 600)
    assert windows == [(0, 600), (600, 1200), (1200, 1530)]
    assert p.plan_windows(0, 600) == []


def test_diarization_single_pass_for_short_audio():
    assert p.plan_diarization_chunks(80 * 60) == [(0.0, 80 * 60)]


def test_diarization_chunks_overlap_and_cover_long_audio():
    chunks = p.plan_diarization_chunks(3 * 3600, chunk=3600, overlap=120, single_pass_max=5400)
    assert chunks[0][0] == 0 and chunks[-1][1] == 3 * 3600
    for (s1, e1), (s2, _e2) in zip(chunks, chunks[1:]):
        assert e1 - s2 == 120  # overlapping pieces


def test_assign_speakers_by_overlap():
    words = [w(" Hi", 0.0, 0.5), w(" there", 0.6, 1.0), w(" Yes", 2.1, 2.4)]
    turns = [{"start": 0.0, "end": 1.1, "speaker": "A"}, {"start": 2.0, "end": 3.0, "speaker": "B"}]
    assert [x["speaker"] for x in p.assign_speakers(words, turns)] == ["A", "A", "B"]


def test_assign_speakers_gap_uses_nearest_or_previous():
    words = [w(" a", 0.0, 0.4), w(" b", 1.5, 1.7), w(" c", 9.0, 9.2)]
    turns = [{"start": 0.0, "end": 1.0, "speaker": "A"}, {"start": 2.0, "end": 3.0, "speaker": "B"}]
    labels = [x["speaker"] for x in p.assign_speakers(words, turns)]
    assert labels[0] == "A"
    assert labels[1] in ("A", "B")  # within max_gap of a turn
    assert labels[2] == labels[1]  # far from any turn -> previous speaker


def test_group_segments_splits_on_speaker_change():
    words = [dict(w(" Hello.", 0, 1), speaker="A"), dict(w(" Hi", 1.2, 1.5), speaker="B"),
             dict(w(" there.", 1.6, 2.0), speaker="B")]
    segs = p.group_segments(words)
    assert [(s["speaker"], s["text"]) for s in segs] == [("A", "Hello."), ("B", "Hi there.")]


def test_group_segments_splits_on_pause_after_sentence():
    words = [dict(w(" One.", 0, 1), speaker="A"), dict(w(" Two.", 5, 6), speaker="A")]
    assert len(p.group_segments(words)) == 2


def test_stitch_turns_no_duplicates_in_overlap():
    pieces = [
        {"start": 0, "end": 100, "turns": [{"start": 0, "end": 100, "speaker": "S0"}]},
        {"start": 90, "end": 200, "turns": [{"start": 90, "end": 200, "speaker": "S0"}]},
    ]
    turns = p.stitch_turns(pieces)
    covered = sum(t["end"] - t["start"] for t in turns)
    assert covered == 200
    assert turns[0]["end"] == turns[1]["start"] == 95


def test_match_speakers_keeps_same_voice_across_pieces():
    known = {}
    first = p.match_speakers(known, {"A": [1.0, 0.0, 0.0], "B": [0.0, 1.0, 0.0]})
    assert set(first.values()) == {"SPEAKER_00", "SPEAKER_01"}
    # Next piece: labels swapped by pyannote, plus a brand-new voice.
    second = p.match_speakers(known, {"X": [0.0, 0.98, 0.1], "Y": [0.97, 0.05, 0.0],
                                      "Z": [0.0, 0.0, 1.0]})
    assert second["Y"] == first["A"]
    assert second["X"] == first["B"]
    assert second["Z"] == "SPEAKER_02"
    assert len(known) == 3


def test_match_speakers_is_one_to_one():
    known = {"SPEAKER_00": [1.0, 0.0]}
    mapping = p.match_speakers(known, {"a": [1.0, 0.0], "b": [0.99, 0.01]})
    assert sorted(mapping.values()) == ["SPEAKER_00", "SPEAKER_01"]


def test_markdown_uses_speaker_names_and_handles_no_diarization():
    t = {"title": "T", "source_name": "x.m4a", "duration": 61, "language": "en", "diarization": True,
         "speaker_names": {"SPEAKER_00": "Priya"},
         "segments": [{"speaker": "SPEAKER_00", "start": 3, "end": 4, "text": "Hello."},
                      {"speaker": "SPEAKER_01", "start": 5, "end": 6, "text": "Hi."}]}
    md = p.to_markdown(t)
    assert "[00:00:03] Priya: Hello." in md
    assert "[00:00:05] SPEAKER_01: Hi." in md
    t["diarization"] = False
    md = p.to_markdown(t)
    assert "[00:00:03] Hello." in md and "unavailable" in md


def test_estimate_and_speed_factor_update():
    rng = p.estimate_seconds(3600, 0.1)
    assert rng["low"] < 380 < rng["high"]
    assert p.updated_speed_factor(0.1, 0.2) == 0.15


def test_estimate_covers_measured_3h_run():
    # Measured on the M4 Air: 3 h without speakers took ~1675 s at a short-file factor of 0.115.
    rng = p.estimate_seconds(10800, 0.115)
    assert rng["low"] < 1675 <= rng["high"]


def test_speed_factor_stored_per_variant(tmp_path, monkeypatch):
    from transcriber import config
    monkeypatch.setattr(config, "TRANSCRIPTS_DIR", tmp_path)
    assert p.load_speed_factor(False) == config.DEFAULT_SPEED_FACTOR
    p.save_speed_factor(0.2, diarization=False)
    assert p.load_speed_factor(False) == round((config.DEFAULT_SPEED_FACTOR + 0.2) / 2, 4)
    # Unmeasured variant derives from the measured one.
    assert p.load_speed_factor(True) == round(p.load_speed_factor(False) * config.DIARIZATION_COST_FACTOR, 4)
    p.save_speed_factor(0.3, diarization=True)
    assert p.load_speed_factor(False) != p.load_speed_factor(True)


def test_quietest_point_finds_silence():
    loud, quiet = [0.5, -0.5] * 400, [0.0] * 800
    samples = loud + quiet + loud  # 16 kHz, 50 ms frames of 800 samples
    offset = p.quietest_point(samples)
    assert 0.05 <= offset <= 0.1


def test_estimate_covers_measured_11min_run():
    rng = p.estimate_seconds(655, 0.115)  # measured: 72 s
    assert rng["low"] <= 72 <= rng["high"]


def test_markdown_explains_failed_speaker_labels():
    t = {"title": "T", "duration": 5, "diarization": False, "diarization_error": "LocalEntryNotFoundError: x",
         "segments": [{"speaker": "SPEAKER_00", "start": 1, "end": 2, "text": "Hello."}]}
    md = p.to_markdown(t)
    assert "Speaker labels failed: LocalEntryNotFoundError" in md
    assert "not installed" not in md


def test_run_keeps_transcript_when_speaker_labelling_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(p.config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(p.config, "TRANSCRIPTS_DIR", tmp_path / "out")
    monkeypatch.setattr(p, "probe_media", lambda _src: {"duration": 120.0, "audio_tracks": 1})
    monkeypatch.setattr(p, "unload_ollama_models", lambda: None)
    monkeypatch.setattr(p, "extract_audio", lambda *_a: None)
    monkeypatch.setattr(p, "transcribe_words",
                        lambda *_a: ([{"start": 1.0, "end": 1.5, "word": " Hello."}], "en"))
    monkeypatch.setattr(p, "diarization_available", lambda: True)

    def broken_diarize(*_a):
        raise RuntimeError("PLDA file missing")

    monkeypatch.setattr(p, "diarize", broken_diarize)
    saved = []
    monkeypatch.setattr(p, "save_speed_factor", lambda *a: saved.append(a))
    src = tmp_path / "m.m4a"
    src.write_bytes(b"x")
    t = p.run(src, lambda _u: None)
    assert t["diarization"] is False
    assert t["diarization_error"] == "RuntimeError: PLDA file missing"
    assert "Hello." in (tmp_path / "out" / f"{t['id']}.md").read_text()
    assert saved == []  # a failed run doesn't calibrate the speed estimate
    # Whisper's words are kept for corrections with other transcripts (not in the transcript itself).
    assert p.load_words(t["id"]) == [{"word": " Hello.", "start": 1.0, "end": 1.5, "p": None}]


def test_probe_media_counts_audio_tracks_of_screen_recordings(monkeypatch):
    import json
    import subprocess

    def fake_run(cmd, **_kw):
        streams = [{"index": 0, "codec_type": "video", "duration": "30.29"}]
        if "with-mic.mov" in cmd[-1]:
            streams += [{"index": 1, "codec_type": "audio"}, {"index": 2, "codec_type": "audio"}]
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"streams": streams, "format": {}}), "")

    monkeypatch.setattr(p.subprocess, "run", fake_run)
    assert p.probe_media(p.Path("Screen Recording.mov")) == {"duration": 30.29, "audio_tracks": 0, "video_tracks": 1, "audio_titles": []}
    assert p.probe_media(p.Path("with-mic.mov"))["audio_tracks"] == 2


def test_several_audio_tracks_are_mixed():
    assert p.audio_map_args(1) == ["-map", "0:a:0"]
    args = p.audio_map_args(2)
    assert args[1] == "[0:a:0][0:a:1]amix=inputs=2:duration=longest[mix]" and args[-1] == "[mix]"


def test_recording_without_sound_fails_with_clear_message(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(p.config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(p, "probe_media", lambda _src: {"duration": 30.0, "audio_tracks": 0})
    monkeypatch.setattr(p, "extract_audio", lambda *_a: pytest.fail("ffmpeg must not run"))
    src = tmp_path / "Screen Recording.mov"
    src.write_bytes(b"x")
    with pytest.raises(p.NoAudioError, match="Options → Microphone"):
        p.run(src, lambda _u: None)


def test_partial_segments_reads_finished_windows(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(p.config, "WORK_DIR", tmp_path)
    src = tmp_path / "m.m4a"
    src.write_bytes(b"x")
    work = tmp_path / p.source_id(src)
    work.mkdir()
    (work / "whisper_0000.json").write_text(json.dumps({"words": [
        {"word": " Hello", "start": 0.0, "end": 0.4}, {"word": " there.", "start": 0.4, "end": 0.8},
        {"word": " Next", "start": 3.0, "end": 3.3}, {"word": " topic.", "start": 3.3, "end": 3.6}]}))
    out = p.partial_segments(src)
    assert out["windows"] == 1 and [s["text"] for s in out["segments"]] == ["Hello there.", "Next topic."]


def test_run_saves_voice_fingerprints_under_final_labels(tmp_path, monkeypatch):
    monkeypatch.setattr(p.config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(p.config, "TRANSCRIPTS_DIR", tmp_path / "out")
    monkeypatch.setattr(p, "probe_media", lambda _src: {"duration": 30.0, "audio_tracks": 1})
    monkeypatch.setattr(p, "unload_ollama_models", lambda: None)
    monkeypatch.setattr(p, "extract_audio", lambda *_a: None)
    monkeypatch.setattr(p, "transcribe_words", lambda *_a: ([
        {"start": 1.0, "end": 1.5, "word": " Hi."}, {"start": 3.0, "end": 3.5, "word": " Hello."}], "en"))
    monkeypatch.setattr(p, "diarization_available", lambda: True)
    monkeypatch.setattr(p, "save_speed_factor", lambda *a: None)

    def fake_diarize(_wav, _d, _w, _p, _s, vectors_out):
        vectors_out.update({"SPEAKER_07": [1.0, 0.0], "SPEAKER_03": [0.0, 1.0]})
        return [{"start": 0.5, "end": 2.0, "speaker": "SPEAKER_07"},
                {"start": 2.5, "end": 4.0, "speaker": "SPEAKER_03"}]

    monkeypatch.setattr(p, "diarize", fake_diarize)
    src = tmp_path / "m.m4a"
    src.write_bytes(b"x")
    t = p.run(src, lambda _u: None)
    assert [s["speaker"] for s in t["segments"]] == ["SPEAKER_00", "SPEAKER_01"]
    assert t["speaker_embeddings"] == {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]}


# --- Quorum call recordings: the call and your microphone on separate tracks ----------------------------------

def test_track_roles_from_the_layout_file_or_the_track_titles(tmp_path):
    rec = tmp_path / "Call.mov"
    rec.write_bytes(b"x")
    two = {"audio_tracks": 2, "audio_titles": ["", ""]}
    assert p.track_roles(rec, two) == {}  # any other recording: mixed, as before
    (tmp_path / "Call.mov.quorum.json").write_text('{"recorder": "quorum-record", "audio_tracks": ["system", "mic"]}')
    assert p.track_roles(rec, two) == {"system": 0, "mic": 1}
    assert p.track_roles(rec, {"audio_tracks": 1}) == {}  # one track can't be split
    other = tmp_path / "other.mov"
    assert p.track_roles(other, {"audio_tracks": 2, "audio_titles": ["Microphone", "System audio"]}) == {"mic": 0, "system": 1}
    assert p.track_roles(other, {"audio_tracks": 2, "audio_titles": ["Core Media Audio", "Core Media Audio"]}) == {}


def test_silent_windows_are_recognised():
    import numpy as np
    rate = 16000
    quiet = np.random.default_rng(1).normal(0, 0.003, rate * 10)  # room noise
    assert p.is_silent(quiet)
    speech = quiet.copy()
    speech[rate * 2: rate * 4] += 0.2 * np.sin(np.arange(rate * 2) * 0.05)  # two seconds of voice
    assert not p.is_silent(speech)
    blip = quiet.copy()
    blip[rate: rate + 3200] += 0.3  # a 0.2 s click
    assert p.is_silent(blip)


def test_the_call_leaking_into_the_mic_is_dropped_but_your_words_stay():
    system = [w(" Can", 10.0, 10.2), w(" we", 10.2, 10.4), w(" ship", 10.4, 10.7), w(" Friday?", 10.7, 11.1)]
    mic = [w(" can", 10.3, 10.5), w(" we", 10.5, 10.7), w(" ship", 10.7, 11.0), w(" Friday", 11.0, 11.4),  # echo
           w(" Yes,", 13.0, 13.3), w(" Friday", 13.3, 13.7), w(" works.", 13.7, 14.0)]                      # you
    kept = p.drop_echo(mic, system)
    assert [x["word"] for x in kept] == [" Yes,", " Friday", " works."]
    assert p.drop_echo(mic, []) == mic  # headphones: nothing to compare, nothing dropped


def test_the_speaker_hint_for_the_call_leaves_you_out():
    assert p.others_hint({"num": 4}) == {"num": 3}
    assert p.others_hint({"min": 2, "max": 5}) == {"min": 1, "max": 4}
    assert p.others_hint({"num": 1}) == {"num": 1}
    assert p.others_hint(None) is None


def test_merging_tracks_keeps_overlapping_speech_whole_and_labels_you():
    system = [dict(w(" Release", 0.0, 0.5), speaker="A"), dict(w(" is", 0.5, 0.7), speaker="A"),
              dict(w(" Friday.", 0.7, 1.2), speaker="A"), dict(w(" Agreed.", 6.0, 6.5), speaker="B")]
    mic = [w(" Wait,", 0.6, 0.9), w(" which", 0.9, 1.1), w(" Friday?", 1.1, 1.6)]  # talks over A
    segments, order = p.merge_tracks(mic, system)
    assert [(s["speaker"], s["text"]) for s in segments] == [
        ("SPEAKER_00", "Release is Friday."), ("SPEAKER_01", "Wait, which Friday?"), ("SPEAKER_02", "Agreed.")]
    assert order == {"A": "SPEAKER_00", p.MIC_SPEAKER: "SPEAKER_01", "B": "SPEAKER_02"}


def test_a_quorum_call_recording_marks_your_microphone_as_a_confirmed_speaker(tmp_path, monkeypatch):
    monkeypatch.setattr(p.config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(p.config, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
    src = tmp_path / "Call 2026-11-22 14.05.mov"
    src.write_bytes(b"x")
    (tmp_path / "Call 2026-11-22 14.05.mov.quorum.json").write_text(
        '{"recorder": "quorum-record", "audio_tracks": ["system", "mic"], "source": {"kind": "app", "id": "us.zoom.xos"}}')
    monkeypatch.setattr(p, "probe_media", lambda _s: {"duration": 120.0, "audio_tracks": 2, "video_tracks": 0,
                                                      "audio_titles": ["System audio", "Microphone"]})
    monkeypatch.setattr(p, "wait_for_ollama_idle", lambda *_a, **_k: True)
    monkeypatch.setattr(p, "diarization_available", lambda: True)
    extracted = []
    monkeypatch.setattr(p, "extract_audio", lambda src, wav, dur, prog, tracks=1, track=None: extracted.append((wav.name, track)))
    heard = {"whisper_system": [w(" Is", 1.0, 1.2), w(" the", 1.2, 1.3), w(" build", 1.3, 1.6), w(" green?", 1.6, 2.0),
                                w(" Thanks.", 9.0, 9.5)],
             "whisper_mic": [w(" Yes,", 3.0, 3.3), w(" all", 3.3, 3.5), w(" green.", 3.5, 4.0)]}
    names = []
    monkeypatch.setattr(p, "transcribe_words", lambda wav, dur, work, prog, name="whisper", skip_silent=False, free=True:
                        names.append((name, skip_silent)) or (heard[name], "en"))
    hints = []

    def diarize(wav, dur, work, prog, speakers=None, vectors=None):
        hints.append((wav.name, speakers))
        vectors.update({"spk_a": [1.0, 0.0], "spk_b": [0.0, 1.0]})
        return [{"start": 0.5, "end": 2.5, "speaker": "spk_a"}, {"start": 8.5, "end": 10.0, "speaker": "spk_b"}]

    monkeypatch.setattr(p, "diarize", diarize)
    monkeypatch.setattr(p, "voice_fingerprint", lambda wav, segs, spk: [0.5, 0.5])
    t = p.run(src, lambda _e: None, speakers={"num": 3})
    assert extracted == [("system.wav", 0), ("mic.wav", 1)]
    assert names == [("whisper_system", False), ("whisper_mic", True)]  # silent mic windows skipped
    assert hints == [("system.wav", {"num": 2})]                          # the others only
    assert [(s["speaker"], s["text"]) for s in t["segments"]] == [
        ("SPEAKER_00", "Is the build green?"), ("SPEAKER_01", "Yes, all green."), ("SPEAKER_02", "Thanks.")]
    assert t["confirmed_speakers"] == {"SPEAKER_01": {"source": "microphone"}}
    assert t["speaker_embeddings"] == {"SPEAKER_00": [1.0, 0.0], "SPEAKER_02": [0.0, 1.0], "SPEAKER_01": [0.5, 0.5]}
    assert t["diarization"] is True and t["audio_tracks"] == {"mic": True, "system": True}
    assert t["recorded_by_quorum"]["source"] == {"kind": "app", "id": "us.zoom.xos"}
