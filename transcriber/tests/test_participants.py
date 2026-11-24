"""The participants list in a call recording: the panel or the screen-share strip is found in the text reader's
lines, the speaking row is found from its colour rising above its own usual level (coloured avatars don't count),
attendees are summarised (cut-off and accent-less spellings merged, invited people kept apart), list speakers are
matched to diarized speakers, and the on-screen names stage counts tiles, the list and both. Synthetic frames only."""

from __future__ import annotations

import shutil
from collections import Counter

import pytest

from transcriber import participants as pt
from transcriber import screen_names as sn
from transcriber.tests import synthetic_meeting as synth

NAMES = synth.NAMES
W, H = synth.SIZE
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def panel_lines(names=NAMES, invited=("Sam Lee",), tiles=NAMES) -> list[dict]:
    """What the text reader returns for a `panel` frame (positions as synthetic_meeting.draw_panel draws them)."""
    lines = [{"text": n, "confidence": 0.95, "box": [(r[0] + 16) / W, (r[3] - 36) / H, 0.12, 26 / H]}
             for n, r in zip(tiles, synth.tile_rects(len(tiles), (920, 720)))]
    lines.append({"text": f"Participants ({len(names)})", "confidence": 0.99, "box": [950 / W, 24 / H, 0.15, 26 / H]})
    y = 80
    for n in names:
        lines.append({"text": n, "confidence": 0.95, "box": [992 / W, (y + 3) / H, 0.15, 24 / H]})
        y += 48
    if invited:
        y += 16
        lines.append({"text": f"Invited ({len(invited)})", "confidence": 0.99, "box": [950 / W, y / H, 0.12, 26 / H]})
        y += 44
        for n in invited:
            lines.append({"text": n, "confidence": 0.95, "box": [992 / W, (y + 3) / H, 0.12, 24 / H]})
            y += 48
    return lines


def test_the_panel_gives_names_count_and_invited_and_skips_roles_and_tiles():
    lines = panel_lines() + [{"text": "Organizer", "confidence": 0.9, "box": [992 / W, 104 / H, 0.06, 0.02]}]
    panel = pt.find_list(lines)
    assert panel.kind == "panel" and panel.count == 4
    assert [r.name for r in panel.rows] == NAMES  # not the tile labels on the left, not "Organizer"
    assert panel.invited == ["Sam Lee"]


def test_a_strip_needs_evenly_spaced_names_on_the_right_and_a_document_is_not_one():
    strip = [{"text": n, "confidence": 0.99, "box": [0.805, (144 + i * 150) / H, 0.15, 0.03]} for i, n in enumerate(NAMES)]
    assert [r.name for r in pt.find_list(strip).rows] == NAMES
    doc = [{"text": f"Release Plan {i}", "confidence": 0.9, "box": [0.80, 0.1 + i * 0.03, 0.15, 0.02]} for i in range(8)]
    assert pt.find_list(doc) is None


def _readings(sequence, style="ring"):
    """Readings for frames where the list marks these rows as speaking (None: nobody)."""
    out = []
    for i, talking in enumerate(sequence):
        rgb = __import__("numpy").asarray(synth.draw_frame(NAMES, 0, "panel", list_talking=talking,
                                                           panel_style=style).convert("RGB").resize((640, 360)),
                                          dtype="float32") / 255.0
        panel = pt.find_list(panel_lines(invited=()))
        pt.measure(rgb, panel)
        out.append(pt.Reading(float(i), panel))
    return out


@pytest.mark.parametrize("style", ["ring", "mic"])
def test_the_speaking_row_rises_above_its_own_colour_and_avatars_dont_count(style):
    readings = _readings([0, 1, None, 2, 3, None, 1], style)
    assert pt.speaking(readings) == {0.0: "Priya Sharma", 1.0: "Daniel Okafor", 3.0: "Mei Lin Chen",
                                     4.0: "Tomás Rivera", 6.0: "Daniel Okafor"}


def test_attendees_merge_spellings_keep_invited_apart_and_count_who_spoke():
    def reading(t, names, invited=()):
        return pt.Reading(t, pt.Panel("panel", [pt.Row(n, False, (0.8, 0.1 + i * 0.05, 0.1, 0.03))
                                               for i, n in enumerate(names)], list(invited), len(names)))

    readings = [reading(0, ["Priya Sharma", "Tomas Rivera"], ["Sam Lee"]), reading(1, ["Priya Shar", "Tomás Rivera"], ["Sam Lee"]),
                reading(60, ["Priya Sharma", "Tomás Rivera", "Owen Hart"]), reading(61, ["Priya Sharma", "Owen Hart"]),
                reading(62, ["Priya Sharma", "Glitch Name"])]
    summary = pt.summarise(readings, {0: "Priya Sharma", 60: "Owen Hart"}, scanned=120)
    assert [(e["name"], e["frames"], e["first_seen"], e["spoke"]) for e in summary["names"]] == [
        ("Priya Sharma", 5, 0.0, 1), ("Tomás Rivera", 3, 0.0, 0), ("Owen Hart", 2, 60.0, 1)]  # one glitch read: out
    assert summary["invited"] == ["Sam Lee"] and summary["frames_with_list"] == 5 and summary["frames_scanned"] == 120


def test_list_speakers_are_matched_to_the_diarized_speaker_talking_then():
    segments = [{"start": 0, "end": 5, "speaker": "SPEAKER_00"}, {"start": 5.2, "end": 6, "speaker": "SPEAKER_01"},
                {"start": 6, "end": 12, "speaker": "SPEAKER_01"}]
    talking = {0.2: "Priya Sharma", 2: "Priya Sharma", 3: "Priya Shar", 5.5: "Owen Hart", 8: "Owen Hart"}
    got = pt.list_evidence(talking, segments, {"Priya Shar": "Priya Sharma"})
    # 0.2 s: too early in the turn; 5.5 s: a turn under 1.5 s
    assert got == {"SPEAKER_00": Counter({"Priya Sharma": 2}), "SPEAKER_01": Counter({"Owen Hart": 1})}


def test_the_vision_prompt_asks_for_the_list_speaker_and_only_listed_names_are_kept():
    prompt = sn.vision_prompt(["Priya Sharma"], ["Priya Sharma", "Owen Hart"])
    assert "participants list: Priya Sharma; Owen Hart" in prompt and '"list_name"' in prompt
    reply = sn.parse_vision_reply('{"highlighted": true, "name": "Priya Sharma", "list_name": "Owen Hart"}')
    assert sn.restrict(reply, ["Priya Sharma"], ["Priya Sharma", "Owen Hart"])["list_name"] == "Owen Hart"
    invented = sn.parse_vision_reply('{"highlighted": false, "name": null, "list_name": "Zed Zulu"}')
    assert sn.restrict(invented, [], ["Owen Hart"])["list_name"] == ""


def panel_ocr_for(names):
    """OCR stand-in for `panel` videos: tiles on the left, the participants panel on the right."""
    def ocr(paths, on_line=None):
        out = {str(p): panel_lines(names, tiles=names) for p in paths}
        if on_line:
            for _ in paths:
                on_line()
        return out
    return ocr


def panel_ocr(paths, on_line=None):
    """OCR stand-in for `panel` videos of the four synthetic people."""
    out = {str(p): panel_lines() for p in paths}
    if on_line:
        for _ in paths:
            on_line()
    return out


@needs_ffmpeg
def test_stage_counts_tiles_the_list_and_both_and_lists_attendees(tmp_path):
    turns = [(0, 0, 8), (1, 8.5, 16), (2, 16.5, 24), (3, 24.5, 32), (0, 32.5, 40), (1, 40.5, 48)]
    video = synth.make_video(tmp_path / "panel.mp4", turns, NAMES, modes={i: "panel" for i in range(6)},
                             frame_options={"invited": ["Sam Lee"]})
    found, info = sn.run(video, synth.segments_for(turns), tmp_path / "w", lambda f: None, {"vision_model": ""},
                         ocr=panel_ocr)
    tops = {spk: v["names"][0] for spk, v in found.items()}
    assert {spk: e["name"] for spk, e in tops.items()} == {f"SPEAKER_{i:02d}": NAMES[i] for i in range(4)}
    assert all(e["ocr"] >= 1 and e["list"] >= 3 and e["both"] >= 1 for e in tops.values())
    people = info["participants"]
    assert [e["name"] for e in people["names"]] == NAMES and people["invited"] == ["Sam Lee"]
    assert people["count"] == 4 and people["frames_with_list"] >= 40 and info["list_conflicts"] == 0


@needs_ffmpeg
def test_when_the_list_and_the_tile_disagree_it_is_a_conflict(tmp_path):
    turns = [(0, 0, 8), (1, 8.5, 16), (0, 16.5, 24), (1, 24.5, 32)]
    video = synth.make_video(tmp_path / "odd.mp4", turns, NAMES[:3],
                             modes={i: "panel_other" for i in range(4)})  # the list marks the next person
    found, info = sn.run(video, synth.segments_for(turns), tmp_path / "w", lambda f: None, {"vision_model": ""},
                         ocr=panel_ocr_for(NAMES[:3]))
    first = found["SPEAKER_00"]["conflicts"][0]
    assert first["ocr"] == "Priya Sharma" and first["list"] == "Daniel Okafor" and info["list_conflicts"] >= 2


@needs_ffmpeg
def test_the_scan_can_be_switched_off(tmp_path):
    turns = [(0, 0, 6), (1, 6.5, 12)]
    video = synth.make_video(tmp_path / "p.mp4", turns, NAMES, modes={0: "panel", 1: "panel"})
    found, info = sn.run(video, synth.segments_for(turns), tmp_path / "w", lambda f: None,
                         {"vision_model": "", "participants": False}, ocr=panel_ocr)
    assert info["participants"] == {"status": "off"} and "list_frames" not in found["SPEAKER_00"]


@pytest.mark.skipif(not (shutil.which("swiftc") or sn.OCR_BINARY.exists()), reason="needs the Vision OCR helper")
def test_the_real_text_reader_finds_the_panel(tmp_path):
    path = tmp_path / "panel.jpg"
    synth.draw_frame(NAMES, 1, "panel", invited=["Sam Lee"]).save(path, quality=90)
    panel = pt.find_list(sn.run_ocr([path])[str(path)])
    assert [pt.fold(r.name) for r in panel.rows] == [pt.fold(n) for n in NAMES]
    assert panel.invited == ["Sam Lee"] and panel.count == 4


def test_the_toolbar_people_button_and_text_under_it_are_not_a_list():
    """Teams' toolbar has a People button; the shared screen under it has words in no list order."""
    toolbar = [{"text": t, "confidence": 0.99, "box": [0.40 + i * 0.04, 0.02, 0.03, 0.02]}
               for i, t in enumerate(["Chat", "People", "Raise", "React", "View", "More"])]
    stray = [{"text": t, "confidence": 0.9, "box": [x, 0.1 + i * 0.06, 0.1, 0.02]}
             for i, (t, x) in enumerate([("Recommended", 0.47), ("Settings", 0.5), ("Speaker Identification", 0.58),
                                         ("Owen Hart", 0.54)])]
    assert pt.find_list(toolbar + stray) is None
    # a heading with names left-aligned right under it is a list
    heading = [{"text": "People", "confidence": 0.99, "box": [0.75, 0.05, 0.06, 0.02]}]
    rows = [{"text": n, "confidence": 0.9, "box": [0.78, 0.1 + i * 0.05, 0.12, 0.02]} for i, n in enumerate(NAMES)]
    assert [r.name for r in pt.find_list(heading + rows).rows] == NAMES
    # names far below the heading, or not aligned, are not
    assert pt.find_list(heading + [dict(r, box=[0.78, 0.6 + i * 0.05, 0.12, 0.02]) for i, r in enumerate(rows)]) is None
    assert pt.find_list(heading + [dict(r, box=[0.76 + i * 0.03, 0.1 + i * 0.05, 0.12, 0.02]) for i, r in enumerate(rows)]) is None


def test_two_lines_at_the_right_of_a_shared_screen_are_not_a_strip():
    two = [{"text": n, "confidence": 0.99, "box": [0.805, (144 + i * 150) / H, 0.15, 0.03]} for i, n in enumerate(NAMES[:2])]
    assert pt.find_list(two) is None
