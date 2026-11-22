#!/usr/bin/env python3
"""Minutes from transcripts: one call when it fits, map-reduce (chunk → notes → merge) when long."""

import long_context
import summaries


def transcript(lines: int, words: int = 30) -> str:
    return "".join(f"[00:{i // 60:02d}:{i % 60:02d}] SPEAKER_0{i % 3}: " + "word " * words + f"line{i}.\n"
                   for i in range(lines))


def fake_model():
    calls = {"complete": [], "stream": []}

    def complete(messages):
        part = messages[-1]["content"]
        calls["complete"].append(part)
        ids = [w for w in part.split() if w.startswith("line")]
        return f"- notes covering {ids[0]}..{ids[-1]}"

    def stream(messages):
        calls["stream"].append(messages[-1]["content"])
        yield "## Summary\n"
        yield "Done."

    return calls, complete, stream


def test_short_transcript_is_summarised_in_one_streamed_call():
    calls, complete, stream = fake_model()
    events = []
    out = summaries.summarize(transcript(20), "Standup", 32768, stream, complete, events.append)
    assert out == "## Summary\nDone."
    assert calls["complete"] == [] and len(calls["stream"]) == 1
    assert "The full transcript" in calls["stream"][0] and "line19." in calls["stream"][0]
    assert [e["text"] for e in events if e["type"] == "delta"] == ["## Summary\n", "Done."]


def test_long_transcript_is_chunked_on_turns_and_every_part_reaches_the_minutes():
    text = transcript(900)  # ~38k tokens: larger than an 8k window
    calls, complete, stream = fake_model()
    events = []
    out = summaries.summarize(text, "Board", 8192, stream, complete, events.append)
    parts = calls["complete"]
    assert len(parts) > 3
    # Chunks split on line (speaker-turn) boundaries and together cover the whole meeting.
    joined = "".join(p.split("\n\n", 1)[1] if "\n\n" in p else p for p in parts)
    for i in (0, 450, 899):
        assert f"line{i}." in joined
    final = calls["stream"][0]
    assert "Notes taken from every part" in final
    assert final.count("### Part ") == len(parts)  # merge sees every part's notes
    assert "line0" in final and "line899" in final
    assert out.startswith("## Summary")
    progress = [e for e in events if e["type"] == "progress"]
    assert progress[-1]["text"] == "Writing the minutes"
    assert any(e["type"] == "notice" for e in events)


def test_finished_parts_are_cached_for_a_retry():
    text = transcript(900)
    calls, complete, stream = fake_model()
    cache = {}
    summaries.summarize(text, "Board", 8192, stream, complete, cache=cache)
    first = len(calls["complete"])
    summaries.summarize(text, "Board", 8192, stream, complete, cache=cache)
    assert len(calls["complete"]) == first  # second run reuses every part


def test_split_lines_is_lossless():
    text = transcript(300)
    pieces = long_context.split_lines(text, 1000)
    assert "".join(pieces) == text and all(p.endswith("\n") for p in pieces)
