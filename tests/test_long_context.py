#!/usr/bin/env python3
"""Long transcripts with local models: planning, lossless chunking, map-reduce, retrieval."""

import requests

import long_context as lc


def transcript(lines: int) -> str:
    return "".join(f"[00:{i // 60:02d}:{i % 60:02d}] SPEAKER_0{i % 3}: line {i} about topic {i % 7}.\n"
                   for i in range(lines))


def test_split_lines_is_lossless_and_respects_limit():
    text = transcript(500) + ("x" * 5000) + "\nend\n"
    pieces = lc.split_lines(text, 300)
    assert "".join(pieces) == text
    assert all(len(p) <= 300 * lc.CHARS_PER_TOKEN for p in pieces)


def test_plan_direct_when_it_fits():
    p = lc.plan("what did Priya say?", "short transcript", other_tokens=500, context_length=32768)
    assert p["mode"] == "direct" and p["parts"] == 1


def test_plan_map_reduce_for_summary_of_big_transcript():
    big = "a" * (60_000 * lc.CHARS_PER_TOKEN)
    p = lc.plan("Write the meeting minutes", big, other_tokens=1000, context_length=32768)
    assert p["mode"] == "map_reduce"
    assert p["parts"] >= 3
    assert "larger than the local model's 32k window" in p["notice"]
    assert p["confirm"] is True


def test_plan_retrieval_for_specific_question_and_smaller_window():
    big = "a" * (20_000 * lc.CHARS_PER_TOKEN)
    assert lc.plan("q", big, 500, 32768)["mode"] == "direct"
    p = lc.plan("what did Raj say about budget?", big, 500, 8192)  # smaller model window
    assert p["mode"] == "retrieve"
    assert "8k window" in p["notice"]


def test_map_reduce_covers_every_part():
    text = transcript(3000)
    seen = []

    def fake_complete(messages):
        seen.append(messages[1]["content"])
        return f"notes {len(seen)}"

    progress = []
    notes = lc.map_reduce_notes(text, "minutes", budget_tokens=6000, complete=fake_complete,
                                progress=lambda d, t: progress.append((d, t)))
    assert "".join(seen) == text  # nothing skipped
    assert len(notes) == len(seen) and notes[0].startswith("### Part 1 of")
    assert progress[-1][0] == progress[-1][1]


def test_retrieve_with_embeddings_returns_ordered_relevant_passages():
    text = transcript(2000)
    chunks = lc.split_lines(text, lc.RETRIEVAL_CHUNK_TOKENS)
    target = len(chunks) // 2

    def fake_embed(texts):
        if texts[0].startswith("search_query"):
            return [[1.0, 0.0]]
        return [[1.0, 0.0] if i in (target, 1) else [0.0, 1.0] for i in range(len(texts))]

    cache = {}
    passages, method = lc.retrieve(text, "budget?", cache=cache, top_k=2, embed_fn=fake_embed)
    assert method == "embeddings"
    assert passages == [chunks[1], chunks[target]]  # transcript order
    assert len(cache) == 1  # embeddings cached per transcript


def test_retrieve_falls_back_to_keywords_without_embedding_model():
    text = "".join(f"[00:00:{i:02d}] A: filler text {i}\n" for i in range(60)) * 40
    text += "[01:00:00] B: the budget decision is final\n"

    def broken_embed(_texts):
        raise requests.ConnectionError("no model")

    passages, method = lc.retrieve(text, "budget decision", top_k=1, embed_fn=broken_embed)
    assert method == "keywords"
    assert "budget decision" in passages[0]


def test_summary_detection():
    assert lc.is_summary_request("Give me the action items")
    assert lc.is_summary_request("summarize the meeting")
    assert not lc.is_summary_request("What did Priya say about hiring?")


def test_map_reduce_cache_skips_finished_parts():
    text = transcript(3000)
    cache, calls = {}, []

    def flaky(messages):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("timeout")
        return "notes"

    try:
        lc.map_reduce_notes(text, "minutes", 6000, flaky, cache=cache)
    except RuntimeError:
        pass
    assert len(cache) == 1  # part 1 kept
    calls.clear()
    notes = lc.map_reduce_notes(text, "minutes", 6000, lambda m: calls.append(1) or "notes", cache=cache)
    assert len(calls) == len(notes) - 1  # part 1 not re-run


def test_plan_uses_measured_speeds():
    big = "a" * (20_000 * lc.CHARS_PER_TOKEN)
    fast = lc.plan("q", big, 500, 32768, {"prefill_tps": 400, "gen_tps": 20})
    slow = lc.plan("q", big, 500, 32768, {"prefill_tps": 35, "gen_tps": 5})
    assert slow["seconds_high"] > 10 * fast["seconds_high"]
