"""On-screen names: tile detection, name reading, sampling, aggregation, the vision fallback and
the pipeline stage. All frames are synthetic (transcriber/tests/synthetic_meeting.py)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from transcriber import config, pipeline, screen_names as sn
from transcriber.tests import synthetic_meeting as synth
from transcriber.tests.test_server import client  # noqa: F401  (shared fixture)

NAMES = ["Priya Sharma", "Daniel Okafor", "Mei Lin Chen"]


def frame(tmp_path: Path, name: str = "f.png", **kwargs) -> Path:
    path = tmp_path / name
    synth.draw_frame(NAMES, **kwargs).save(path)
    return path


def ocr_lines_for(labels: list[str], rects=None, size=synth.SIZE) -> list[dict]:
    """What the OCR helper would return for the name labels of a gallery frame."""
    rects = rects or synth.tile_rects(len(labels))
    w, h = size
    return [{"text": label, "confidence": 0.99,
             "box": [(r[0] + 16) / w, (r[3] - 36) / h, 160 / w, 26 / h]} for label, r in zip(labels, rects)]


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Priya Sharma", ("Priya Sharma", False)),
    ("Daniel Okafor (Guest)", ("Daniel Okafor", False)),
    ("🎤 Mei Lin Chen (External)", ("Mei Lin Chen", False)),
    ("Priya Shar…", ("Priya Shar", True)),
    ("Tomás Rivera...", ("Tomás Rivera", True)),
    ("Owen | Presenter", ("Owen", False)),
    ("José María-López", ("José María-López", False)),
    ("O'Brien", ("O'Brien", False)),
])
def test_clean_name_keeps_names(raw, expected):
    assert sn.clean_name(raw) == expected


@pytest.mark.parametrize("raw", ["Mute", "Recording", "AB", "12:04", "", "Share screen", "Leave",
                                 "this is a sentence that is far too long to be a participant name label"])
def test_clean_name_rejects_ui_text(raw):
    assert sn.clean_name(raw) is None


def test_truncated_label_matches_full_name():
    assert sn.same_person("Priya Shar", "Priya Sharma")
    assert not sn.same_person("Pri", "Priya Sharma")
    assert not sn.same_person("Daniel", "Priya Sharma")


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------

def test_one_highlighted_tile_is_found(tmp_path):
    rgb = sn.load_rgb(frame(tmp_path, active=1))
    tiles = sn.find_highlighted_tiles(rgb)
    assert len(tiles) == 1
    expected = synth.tile_rects(3)[1]
    x0, y0, x1, y1 = tiles[0]
    assert abs(x0 - expected[0] / 1280) < 0.02 and abs(y1 - expected[3] / 720) < 0.02


def test_no_highlight_no_tiles(tmp_path):
    assert sn.find_highlighted_tiles(sn.load_rgb(frame(tmp_path, active=None))) == []


def test_gallery_frame_reads_the_active_name(tmp_path):
    rgb = sn.load_rgb(frame(tmp_path, active=2))
    status, found, _ = sn.read_frame(rgb, ocr_lines_for(NAMES))
    assert status == "ocr" and found == ("Mei Lin Chen", False)


def test_several_highlighted_tiles_are_ambiguous(tmp_path):
    rgb = sn.load_rgb(frame(tmp_path, active=0, highlight_all=True))
    status, found, tiles = sn.read_frame(rgb, ocr_lines_for(NAMES))
    assert status == "ambiguous" and found is None and len(tiles) == 3


def test_screen_share_is_recognised(tmp_path):
    rgb = sn.load_rgb(frame(tmp_path, active=0, mode="share"))
    lines = [{"text": f"Roadmap item {i}", "confidence": 0.9, "box": [0.05, 0.05 + i * 0.04, 0.6, 0.03]}
             for i in range(18)]
    assert sn.read_frame(rgb, lines)[0] == "screen_share"


def test_speaker_view_reads_the_bottom_left_label(tmp_path):
    rgb = sn.load_rgb(frame(tmp_path, active=1, mode="speaker"))
    lines = [{"text": "Daniel Okafor", "confidence": 0.95, "box": [0.05, 0.85, 0.15, 0.03]}]
    assert sn.read_frame(rgb, lines)[:2] == ("speaker_view", ("Daniel Okafor", False))


def test_name_outside_the_tile_is_ignored(tmp_path):
    rgb = sn.load_rgb(frame(tmp_path, active=0))
    lines = ocr_lines_for(["", "Daniel Okafor", "Mei Lin Chen"])  # label of the active tile unreadable
    assert sn.read_frame(rgb, lines)[0] == "unresolved"


# ---------------------------------------------------------------------------
# Sampling and aggregation
# ---------------------------------------------------------------------------

def test_samples_spread_over_each_speakers_turns_and_skip_short_ones():
    segments = [{"start": i * 10.0, "end": i * 10 + 9.0, "speaker": f"S{i % 2}"} for i in range(60)]
    segments.append({"start": 700.0, "end": 700.8, "speaker": "S2"})  # too short
    samples = sn.plan_samples(segments, per_speaker=5)
    speakers = {s for _, s in samples}
    assert speakers == {"S0", "S1"}
    assert sum(1 for _, s in samples if s == "S0") == 10  # 5 turns x (early + mid-turn)
    assert samples == sorted(samples)
    assert len(sn.plan_samples(segments, per_speaker=50, max_frames=7)) == 7


def test_aggregate_counts_names_and_folds_truncated_labels():
    results = [sn.FrameResult(1, "SPEAKER_00", "ocr", "Priya Sharma"),
               sn.FrameResult(5, "SPEAKER_00", "ocr", "Priya Shar", truncated=True),
               sn.FrameResult(9, "SPEAKER_00", "vision", "Priya Sharma"),
               sn.FrameResult(3, "SPEAKER_01", "ocr", "Daniel Okafor"),
               sn.FrameResult(7, "SPEAKER_01", "screen_share")]
    out = sn.aggregate(results)
    priya = out["SPEAKER_00"]["names"][0]
    assert priya["name"] == "Priya Sharma" and priya["count"] == 3
    assert priya["ocr"] == 2 and priya["vision"] == 1 and priya["times"] == [1, 5, 9]
    assert out["SPEAKER_01"] == {"names": [dict(out["SPEAKER_01"]["names"][0])], "frames": 2, "resolved": 1}


def test_review_covers_every_speaker_including_screen_share_and_read_frames():
    results = ([sn.FrameResult(i, "SPEAKER_00", "unresolved", path="f") for i in range(5)]
               + [sn.FrameResult(10, "SPEAKER_00", "ocr", "Priya Sharma", path="f")]
               + [sn.FrameResult(20 + i, "SPEAKER_01", "ambiguous", path="f") for i in range(5)]
               + [sn.FrameResult(30, "SPEAKER_02", "screen_share", path="f")]
               + [sn.FrameResult(40, "SPEAKER_03", "ocr", "Mei Lin Chen", path="f")]
               + [sn.FrameResult(50, "SPEAKER_04", "error", path="f")])
    picked = sn.pick_for_review(results, 4)
    assert len(picked) == 4
    assert [r.speaker for r in picked] == ["SPEAKER_01", "SPEAKER_02", "SPEAKER_00", "SPEAKER_03"]  # unnamed first
    assert picked[2].status == "unresolved"  # what the text reader couldn't settle before what it read
    everything = sn.pick_for_review(results, 100)
    assert len(everything) == 13 and all(r.status != "error" for r in everything)
    assert sn.pick_for_review(results, 0) == []


def test_review_budget_is_four_frames_per_speaker_within_limits():
    assert [sn.review_budget(n) for n in (1, 3, 5, 12, 30)] == [12, 12, 20, 48, 48]
    assert sn.review_budget(5, 30) == 30


def test_a_review_confirms_corrects_or_fills():
    r = sn.FrameResult(1, "SPEAKER_00", "ocr", "Priya Sharma")
    assert sn.review_frame(r, {"name": "Priya Sharma"}) == "confirmed" and r.name == "Priya Sharma"
    r = sn.FrameResult(2, "SPEAKER_00", "ocr", "Priya Sharma")
    assert sn.review_frame(r, {"name": "Daniel Okafor"}) == "corrected"
    assert r.name == "Priya Sharma" and r.vision_name == "Daniel Okafor"  # both kept as evidence
    r = sn.FrameResult(3, "SPEAKER_01", "screen_share")
    assert sn.review_frame(r, {"name": "Mei Lin Chen", "truncated": False}) == "filled"
    assert (r.status, r.name) == ("vision", "Mei Lin Chen")
    r = sn.FrameResult(4, "SPEAKER_01", "unresolved")
    assert sn.review_frame(r, {"name": "", "screen_share": True}) == "no_answer" and r.status == "screen_share"
    out = sn.aggregate([sn.FrameResult(1, "SPEAKER_00", "ocr", "Priya Sharma", review="confirmed"),
                        sn.FrameResult(2, "SPEAKER_00", "ocr", "Priya Sharma", review="corrected",
                                       vision_name="Daniel Okafor")])
    names = {e["name"]: e for e in out["SPEAKER_00"]["names"]}
    assert names["Priya Sharma"]["count"] == 2 and names["Priya Sharma"]["confirmed"] == 1
    assert names["Daniel Okafor"]["vision"] == 1
    assert out["SPEAKER_00"]["conflicts"] == [{"t": 2, "ocr": "Priya Sharma", "vision": "Daniel Okafor"}]


def test_vision_reply_parsing_reads_names_only():
    ok = sn.parse_vision_reply('{"screen_share": false, "highlighted": true, "name": "Daniel Okafor (Guest)"}')
    assert ok["name"] == "Daniel Okafor"
    assert sn.parse_vision_reply('{"highlighted": false, "name": "Daniel"}')["name"] == ""
    assert sn.parse_vision_reply("I can't tell")["name"] == ""
    assert sn.parse_vision_reply('```json\n{"screen_share": true, "highlighted": false, "name": null}\n```')["screen_share"]


def test_vision_reply_with_quoted_booleans():
    reply = sn.parse_vision_reply('{"screen_share": "false", "highlighted": "false", "name": null}')
    assert reply == {"screen_share": False, "highlighted": False, "name": "", "truncated": False, "list_name": ""}
    assert sn.parse_vision_reply('{"screen_share": "false", "highlighted": "true", "name": "Mei Lin Chen"}')["name"] == "Mei Lin Chen"


def test_white_highlight_border_is_found_when_there_is_little_text(tmp_path):
    from PIL import ImageDraw
    im = synth.draw_frame(NAMES, None)
    ImageDraw.Draw(im).rectangle(synth.tile_rects(3)[2], outline=(245, 245, 245), width=5)
    path = tmp_path / "white.png"
    im.save(path)
    status, found, _ = sn.read_frame(sn.load_rgb(path), ocr_lines_for(NAMES))
    assert status == "ocr" and found == ("Mei Lin Chen", False)
    lots_of_text = ocr_lines_for(NAMES) + [{"text": f"row {i}", "confidence": 0.9, "box": [0.1, 0.9, 0.1, 0.01]}
                                            for i in range(20)]
    assert sn.read_frame(sn.load_rgb(path), lots_of_text)[0] == "screen_share"


def test_vision_answer_must_be_a_label_on_screen():
    prompt = sn.vision_prompt(["Priya Sharma", "Daniel Okafor"])
    assert "Priya Sharma; Daniel Okafor" in prompt and "one of the listed names" in prompt


def test_vision_prompt_never_asks_about_faces():
    prompt = sn.VISION_PROMPT.lower()
    assert "do not describe, recognise or identify anyone's face" in prompt
    assert "name label" in prompt


def test_estimate_adds_vision_time():
    base = sn.estimate_seconds(600)
    with_vision = sn.estimate_seconds(600, vision_cap=10)
    assert base["frames"] == 40 and with_vision["high"] - base["high"] == round(10 * sn.SECONDS_PER_VISION_FRAME)


# ---------------------------------------------------------------------------
# The stage on a real (synthetic) video
# ---------------------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def fake_ocr_for(names: list[str]):
    """OCR stand-in: every gallery frame shows these name labels on their tiles."""
    def ocr(paths, on_line=None):
        out = {}
        for p in paths:
            out[str(p)] = ocr_lines_for(names)
            if on_line:
                on_line()
        return out
    return ocr


fake_ocr = fake_ocr_for(NAMES)


@needs_ffmpeg
def test_stage_names_each_speaker_and_uses_vision_for_unhighlighted_turns(tmp_path):
    turns = [(0, 0, 6), (1, 6.5, 12), (2, 12.5, 18), (0, 18.5, 24), (1, 24.5, 30), (2, 30.5, 36)]
    video = synth.make_video(tmp_path / "call.mp4", turns, NAMES, modes={5: "none"})
    asked = []

    def vision(image, names):
        asked.append(image)
        return {"screen_share": False, "highlighted": True, "name": "Mei Lin Chen", "truncated": False}

    found, info = sn.run(video, synth.segments_for(turns), tmp_path / "work", lambda f: None,
                         {"vision_model": "gemma4:e4b", "vision_cap": 1}, ocr=fake_ocr, vision=vision)
    assert found["SPEAKER_00"]["names"][0]["name"] == "Priya Sharma"
    assert found["SPEAKER_01"]["names"][0]["name"] == "Daniel Okafor"
    assert found["SPEAKER_02"]["names"][0]["name"] == "Mei Lin Chen"
    assert len(asked) == 1 and info["vision_frames"] == 1 and info["vision_model"] == "gemma4:e4b"
    assert info["status"] == "done" and info["frames"] >= 6 and info["reviewed"] == 1


@needs_ffmpeg
def test_stage_without_vision_model_never_calls_it(tmp_path):
    turns = [(0, 0, 6), (1, 6.5, 12)]
    video = synth.make_video(tmp_path / "call.mp4", turns, NAMES[:2], modes={1: "none"})
    found, info = sn.run(video, synth.segments_for(turns), tmp_path / "w", lambda f: None, {"vision_cap": 5},
                         ocr=fake_ocr_for(NAMES[:2]), vision=lambda image, names: pytest.fail("vision called"))
    assert info["vision_frames"] == 0 and info["unresolved"] >= 1
    assert info["review_reason"].startswith("No vision model is installed")
    assert found["SPEAKER_01"]["resolved"] == 0


@needs_ffmpeg
def test_screen_share_with_a_thumbnail_strip_is_reviewed_and_named(tmp_path):
    """A shared screen with the participants in a side strip: the text reader calls every frame a screen share
    (lots of text, no tile border), so before the review no names were found. The vision model reviews those
    frames and names each speaker; its answers must be labels the text reader saw."""
    turns = [(0, 0, 6), (1, 6.5, 12), (2, 12.5, 18), (0, 18.5, 24), (1, 24.5, 30), (2, 30.5, 36)]
    video = synth.make_video(tmp_path / "share.mp4", turns, NAMES, modes={i: "share_strip" for i in range(6)})
    strip = [{"text": n, "confidence": 0.99, "box": [1030 / 1280, (40 + i * 150 + 104) / 720, 0.15, 0.03]}
             for i, n in enumerate(NAMES)]
    rows = [{"text": f"Release checklist step {i + 1}: verify the payment flow", "confidence": 0.9,
             "box": [0.03, (36 + i * 32) / 720, 0.7, 0.03]} for i in range(20)]

    def ocr(paths, on_line=None):
        return {str(p): rows + strip for p in paths}


    def vision(image, names):  # reads the ring in the strip: who talks at that frame's time
        t = float(image.stem)
        spk = next(s for s, start, end in turns if start <= t < end)
        assert NAMES[spk] in names
        return {"screen_share": True, "highlighted": True, "name": NAMES[spk], "truncated": False}

    old, old_info = sn.run(video, synth.segments_for(turns), tmp_path / "old", lambda f: None,
                           {"vision_model": ""}, ocr=ocr)
    assert old_info["screen_share"] == old_info["frames"] and all(v["resolved"] == 0 for v in old.values())
    found, info = sn.run(video, synth.segments_for(turns), tmp_path / "new", lambda f: None,
                         {"vision_model": "qwen3-vl:4b-instruct"}, ocr=ocr, vision=vision)
    assert {spk: v["names"][0]["name"] for spk, v in found.items()} == {
        "SPEAKER_00": "Priya Sharma", "SPEAKER_01": "Daniel Okafor", "SPEAKER_02": "Mei Lin Chen"}
    assert info["reviewed"] == info["filled"] == info["frames"] and info["review_reason"] == ""


@needs_ffmpeg
def test_stage_is_cancellable(tmp_path):
    turns = [(0, 0, 6), (1, 6.5, 12)]
    video = synth.make_video(tmp_path / "call.mp4", turns, NAMES[:2])

    def progress(fraction):
        raise pipeline.JobCancelled()

    with pytest.raises(pipeline.JobCancelled):
        sn.run(video, synth.segments_for(turns), tmp_path / "w", progress, {}, ocr=fake_ocr_for(NAMES[:2]))


def test_audio_only_and_switched_off_skip_quietly(tmp_path):
    assert sn.run(tmp_path / "x.m4a", [], tmp_path, lambda f: None, {}, has_video=False)[1]["status"] == "no_video"
    assert sn.run(tmp_path / "x.mp4", [], tmp_path, lambda f: None, {"enabled": False})[1]["status"] == "off"


@needs_ffmpeg
def test_probe_counts_video_and_ignores_cover_art(tmp_path):
    video = synth.make_video(tmp_path / "call.mp4", [(0, 0, 3)], NAMES[:1])
    assert pipeline.probe_media(video)["video_tracks"] == 1
    audio = tmp_path / "a.m4a"
    import subprocess
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", str(audio)], check=True)
    assert pipeline.probe_media(audio)["video_tracks"] == 0


def test_stage_reporter_skips_missing_stages():
    seen = []
    report = pipeline.stage_reporter(lambda u: seen.append(u["progress"]), {"diarize", "screen"})
    report("transcribe", 1.0)
    report("finish", 1.0)
    assert seen[-1] == 1.0 and 0.9 < seen[0] < 1.0


@pytest.mark.skipif(not (shutil.which("swiftc") or sn.OCR_BINARY.exists()), reason="needs the Vision OCR helper")
def test_real_ocr_reads_a_synthetic_frame(tmp_path):
    path = frame(tmp_path, active=1)
    lines = sn.run_ocr([path])[str(path)]
    status, found, _ = sn.read_frame(sn.load_rgb(path), lines)
    assert status == "ocr" and found[0] == "Daniel Okafor"


# ---------------------------------------------------------------------------
# Voice fingerprint backfill helpers and the server endpoints
# ---------------------------------------------------------------------------

def test_backfill_uses_the_longest_turns_cut_to_ten_seconds():
    segments = [{"start": 0, "end": 30, "speaker": "A"}, {"start": 40, "end": 41, "speaker": "A"},
                {"start": 50, "end": 55, "speaker": "A"}, {"start": 60, "end": 70, "speaker": "B"}]
    assert pipeline.backfill_crops(segments, "A") == [(0, 10.0), (50, 55)]


def test_mean_vector_normalises_each_print():
    assert pipeline.mean_vector([[2.0, 0.0], [0.0, 3.0]]) == [0.5, 0.5]
    assert pipeline.mean_vector([[float("nan"), 1.0]]) is None


def _save(tid: str, source: Path | None, **extra) -> None:
    config.TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    (config.TRANSCRIPTS_DIR / f"{tid}.json").write_text(json.dumps({
        "id": tid, "title": "t", "source_path": str(source or "/nope/x.mp4"), "duration": 60,
        "segments": [{"start": 0, "end": 5, "speaker": "SPEAKER_00", "text": "hi"}], **extra}))


def test_screen_and_voiceprint_jobs_need_the_recording(client, monkeypatch):
    from transcriber import server

    client, _ = client
    rec = config.UPLOADS_DIR / "call.mp4"
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_bytes(b"x")
    _save("0123456789abcdef", rec)
    _save("fedcba9876543210", None)
    monkeypatch.setattr(server.JOB_QUEUE, "put", lambda job_id: None)  # don't run it
    job = client.post("/transcripts/0123456789abcdef/screen-names", json={"screen": {"vision_cap": 3}}).json()
    assert job["kind"] == "screen" and job["transcript_id"] == "0123456789abcdef" and job["screen"]["vision_cap"] == 3
    again = client.post("/transcripts/0123456789abcdef/screen-names", json={}).json()
    assert again["id"] == job["id"]  # not queued twice
    assert client.post("/transcripts/fedcba9876543210/screen-names", json={}).status_code == 404
    server.app.state.diarization = False
    assert client.post("/transcripts/0123456789abcdef/voiceprints").status_code == 409
    server.app.state.diarization = True
    assert client.post("/transcripts/0123456789abcdef/voiceprints").json()["kind"] == "voiceprints"
