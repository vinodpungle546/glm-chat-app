"""The live draft (transcriber/live.py) on synthetic PCM, with Whisper and the OCR replaced by fakes.

    transcriber/.venv/bin/python -m pytest transcriber/tests -q
"""

import threading
import time

import numpy as np

from transcriber import live

RATE = 16000


def pcm(seconds, bursts):
    """Room noise with loud bursts at the given (start, end) seconds, as 16-bit PCM bytes."""
    rng = np.random.default_rng(7)
    audio = rng.normal(0, 0.002, int(seconds * RATE))
    for a, b in bursts:
        n = int((b - a) * RATE)
        audio[int(a * RATE): int(a * RATE) + n] += 0.3 * np.sin(np.arange(n) * 0.07)
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


# What each track "says" (fake Whisper reads it off by time).
CALL = [("Release", 5.0), ("is", 5.4), ("Friday.", 5.6), ("Agreed,", 40.0), ("ship", 40.4), ("it.", 40.7),
        ("Thanks", 66.0), ("all.", 66.4)]
MIC = [("release", 5.3), ("is", 5.7), ("friday", 5.9),            # the call leaking into the mic (echo)
       ("Which", 12.0), ("Friday?", 12.4), ("Bye.", 67.5)]       # you


def make_folder(tmp_path, seconds=70):
    folder = tmp_path / "live"
    (folder / "frames").mkdir(parents=True)
    (folder / "system.pcm").write_bytes(pcm(seconds, [(4.5, 6.5), (39.5, 41.5), (65.5, 67.0)]))
    (folder / "mic.pcm").write_bytes(pcm(seconds, [(5.0, 6.5), (11.5, 13.0), (39.0, 40.0), (67.0, 68.0)]))
    for second in (4, 40, 66):
        (folder / "frames" / f"{second:06d}.jpg").write_bytes(b"jpg")
    return folder


def fake_whisper():
    calls = []

    def transcribe(samples, offset):
        end = offset + len(samples) / RATE
        # Calls alternate: the call track first, then the mic, in every piece that has sound on both.
        track = CALL if len(calls) % 2 == 0 else MIC
        calls.append(offset)
        return [{"word": " " + w, "start": t, "end": t + 0.3} for w, t in track if offset <= t < end]
    return transcribe, calls


def names(paths):
    return {str(p): ("Priya Sharma" if p.name == "000004.jpg" else None) for p in paths}


def test_pieces_are_cut_at_pauses_and_labelled(tmp_path):
    folder = make_folder(tmp_path)
    transcribe, calls = fake_whisper()
    draft = live.LiveDraft("abc123abc123", folder, video=True, transcribe=transcribe, read_names=names,
                           wait_for_ollama=False)
    assert draft.step() and 27 <= draft.done <= 33          # first piece, cut at a pause near 30 s
    first = draft.done
    assert draft.step() and first + 27 <= draft.done <= first + 33  # second piece
    assert not draft.step()                                 # not enough new audio yet
    assert draft.step(final=True) and draft.done == 70      # at Stop: the rest
    lines = [(s["speaker"], s["text"]) for s in draft.segments]
    assert lines == [("Priya Sharma", "Release is Friday."),  # named from the frame at 4 s
                     ("You", "Which Friday?"),               # your mic; the echo at 5 s was dropped
                     ("Others", "Agreed, ship it."),          # no readable name at 40 s
                     ("Others", "Thanks all."),
                     ("You", "Bye.")]
    assert calls[0] == 0.0 and all(c >= 26 for c in calls[2:])  # pieces overlap by 1 s at most
    md = live.to_markdown(draft.snapshot(), "Release sync")
    assert "[00:00:05] Priya Sharma: Release is Friday." in md and "# Live transcript: Release sync" in md


def test_memory_pressure_pauses_the_draft_and_it_catches_up(tmp_path):
    folder = make_folder(tmp_path)
    transcribe, _calls = fake_whisper()
    readings = iter([2, 2, 4] + [1] * 100)
    seen = []
    draft = live.LiveDraft("abc123abc123", folder, video=False, transcribe=transcribe, read_names=names,
                           pressure=lambda: next(readings), wait_for_ollama=False)
    original_wait = draft.stop_event.wait
    draft.stop_event.wait = lambda t=None: (seen.append(draft.state), original_wait(0.01))[1]
    draft.start()
    deadline = time.time() + 10
    while draft.done < 55 and time.time() < deadline:
        time.sleep(0.02)
    assert "paused_memory" in seen and draft.done >= 55
    draft.stop()
    draft.thread.join(5)
    assert draft.state == "done" and draft.done == 70
    assert [s["speaker"] for s in draft.segments][0] == "Others"  # audio only: no names from the screen
    assert (folder / "live.json").exists()


def test_a_restarted_transcriber_resumes_the_draft(tmp_path):
    folder = make_folder(tmp_path)
    transcribe, _calls = fake_whisper()
    first = live.LiveDraft("abc123abc123", folder, transcribe=transcribe, read_names=names, wait_for_ollama=False)
    first.step()
    first.save()
    transcribe2, calls2 = fake_whisper()
    again = live.LiveDraft("abc123abc123", folder, transcribe=transcribe2, read_names=names, wait_for_ollama=False)
    assert again.done == first.done and len(again.segments) == len(first.segments)
    again.step()
    assert min(calls2) >= first.done - 1.01  # carries on where it stopped


def test_a_draft_far_behind_doesnt_hold_up_the_final_transcript(tmp_path, monkeypatch):
    folder = make_folder(tmp_path, seconds=70)
    monkeypatch.setattr(live, "FLUSH_MAX_BEHIND", 20)
    transcribe, calls = fake_whisper()
    draft = live.LiveDraft("abc123abc123", folder, transcribe=transcribe, read_names=names,
                           pressure=lambda: 4, wait_for_ollama=False)  # paused the whole call
    draft.stop()
    draft.run()
    assert calls == [] and draft.state == "done"


def test_frames_are_matched_to_the_moment_of_each_line(tmp_path):
    frames = {4: tmp_path / "a", 40: tmp_path / "b"}
    assert live.nearest_frame(frames, 5.5) == tmp_path / "a"
    assert live.nearest_frame(frames, 20) is None  # nothing within 3 s
    assert live.nearest_frame({}, 3) is None


def test_a_check_with_a_local_model_pauses_the_draft_then_it_carries_on(tmp_path):
    folder = make_folder(tmp_path)
    transcribe, calls = fake_whisper()
    draft = live.LiveDraft("abc123abc123", folder, video=False, transcribe=transcribe, read_names=names,
                           pressure=lambda: 1, wait_for_ollama=False)
    draft.hold(30)
    draft.start()
    deadline = time.time() + 5
    while draft.state != "paused_check" and time.time() < deadline:
        time.sleep(0.02)
    assert draft.state == "paused_check" and calls == []
    draft.resume()
    while draft.done < 55 and time.time() < deadline + 5:
        time.sleep(0.02)
    assert draft.done >= 55
    draft.stop()
    draft.thread.join(5)
