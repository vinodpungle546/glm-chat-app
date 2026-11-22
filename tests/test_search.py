#!/usr/bin/env python3
"""Search: passages, keyword + semantic ranking, filters, the background indexer, web provider
adapters (HTTP mocked), missing key / invalid key / rate limit / offline fallbacks."""

from unittest.mock import MagicMock, patch

import pytest
import requests

import search_index as si
import secrets_store
import web_providers as wp
from tests.test_api import fake_embed


def chunk(text, title="t", source="chats", kind="chat", date="2026-11-10", doc="d"):
    return si.Chunk(source, kind, doc, title, text, date, f"/c/{doc}")


CORPUS = [
    chunk("We agreed the refund endpoint fix ships to staging on Friday.", "Release sync", doc="a"),
    chunk("Lunch options near the office: pizza, sushi and salads.", "Lunch", doc="b", date="2026-10-01"),
    chunk("Owen reported two iOS payment bugs during regression testing.", "QA standup", doc="c", source="transcripts", kind="transcript"),
    chunk("Cian is the release manager for Project Atlas.", "Memory", doc="m", source="memory", kind="imported_fact"),
]


def test_keyword_ranking_prefers_the_right_passage():
    hits = si.search("refund endpoint staging", CORPUS)
    assert hits[0]["chunk"].doc_id == "a" and hits[0]["method"] == "keywords"
    assert "refund endpoint" in hits[0]["snippet"]
    assert si.search("zebra", CORPUS) == []


def test_semantic_results_fuse_with_keywords():
    vectors = {c.hash: v for c, v in zip(CORPUS, fake_embed([c.text for c in CORPUS]))}
    query = "payment bugs found by QA"
    hits = si.search(query, CORPUS, vectors, fake_embed([query])[0], min_similarity=0.1)
    assert hits[0]["chunk"].doc_id == "c" and hits[0]["method"] == "both"


def test_one_result_per_document_and_date_filter():
    two = [chunk("refund refund", doc="a"), chunk("refund again", doc="a")]
    assert len(si.search("refund", two)) == 1
    recent = [c for c in CORPUS if si.in_range(c.date, "2026-11-01", "2026-11-30")]
    assert {c.doc_id for c in recent} == {"a", "c", "m"}


def test_chat_and_transcript_passages_carry_links_and_timestamps():
    record = {"id": "abc", "title": "Imported", "updated_at": "2026-11-10T09:00:00",
              "imported": {"source": "claude.ai"}, "messages": [{"role": "user", "content": "Hi"},
                                                               {"role": "assistant", "content": "Hello"}]}
    c = si.chat_chunks(record)[0]
    assert c.kind == "imported_chat" and c.link == "/c/abc" and "Claude: Hello" in c.text
    t = si.transcript_chunks({"id": "t1", "title": "Standup", "created_at": "2026-11-18T10:00:00"},
                             [{"speaker": "S0", "start": 75, "text": "Retest refunds"}], {"S0": "Owen"})[0]
    assert t.link == "/meetings/t1?t=75" and t.meta["timestamp"] == "00:01:15" and "Owen: Retest refunds" in t.text


def test_indexer_embeds_in_the_background_and_reports_errors(tmp_path):
    store = si.VectorStore(tmp_path / "v.json")
    indexer = si.Indexer(store)
    state = indexer.start(CORPUS, "nomic-embed-text", fake_embed)
    indexer.thread.join(5)
    assert state["total"] == 4 and indexer.snapshot()["state"] == "done"
    assert len(store.get("nomic-embed-text")) == 4 and (tmp_path / "v.json").exists()
    assert indexer.start(CORPUS, "nomic-embed-text", fake_embed)["total"] == 0  # nothing new

    def down(_texts):
        raise requests.ConnectionError("refused")
    broken = si.Indexer(si.VectorStore(tmp_path / "w.json"))
    broken.start(CORPUS, "nomic-embed-text", down)
    broken.thread.join(5)
    assert broken.snapshot()["state"] == "error" and "keywords only" in broken.snapshot()["error"]


# ---------------------------------------------------------------------------
# Web providers
# ---------------------------------------------------------------------------

@pytest.fixture
def keys(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "file")
    monkeypatch.setenv("SECRETS_FILE", str(tmp_path / "secrets.json"))
    for provider in ("tavily", "brave", "exa"):
        secrets_store.set(wp.key_name(provider), f"{provider}-key-123456")
    return tmp_path


def response(status=200, body=None):
    r = MagicMock()
    r.status_code, r.ok = status, status < 400
    r.json.return_value = body or {}
    return r


@pytest.mark.parametrize("provider,body,header", [
    ("tavily", {"results": [{"title": "T", "url": "https://t.example", "content": "snippet", "published_date": "2026-11-01"}]},
     ("Authorization", "Bearer tavily-key-123456")),
    ("brave", {"web": {"results": [{"title": "T", "url": "https://t.example", "description": "snippet", "page_age": "2026-11-01"}]}},
     ("X-Subscription-Token", "brave-key-123456")),
    ("exa", {"results": [{"title": "T", "url": "https://t.example", "text": "snippet", "publishedDate": "2026-11-01"}]},
     ("x-api-key", "exa-key-123456")),
])
def test_adapters_normalise_results_and_send_the_key(keys, provider, body, header):
    with patch.object(wp.requests, "request", return_value=response(200, body)) as call:
        results = wp.search(provider, "release checklist", 3)
    assert results == [{"title": "T", "url": "https://t.example", "snippet": "snippet", "date": "2026-11-01"}]
    assert call.call_args.kwargs["headers"][header[0]] == header[1]


@pytest.mark.parametrize("status,kind", [(401, "invalid_key"), (403, "invalid_key"), (429, "rate_limited"), (500, "error")])
def test_http_errors_map_to_kinds(keys, status, kind):
    with patch.object(wp.requests, "request", return_value=response(status)):
        with pytest.raises(wp.WebSearchError) as err:
            wp.search("tavily", "q")
    assert err.value.kind == kind


def test_offline_and_missing_key(keys):
    with patch.object(wp.requests, "request", side_effect=requests.ConnectionError("no network")):
        with pytest.raises(wp.WebSearchError) as err:
            wp.search("brave", "q")
    assert err.value.kind == "offline"
    secrets_store.delete(wp.key_name("exa"))
    with patch.object(wp.requests, "request") as call:
        with pytest.raises(wp.WebSearchError) as err:
            wp.search("exa", "q")
    assert err.value.kind == "missing_key" and call.call_count == 0


def test_keys_are_stored_privately(keys):
    import os
    assert oct(os.stat(keys / "secrets.json").st_mode)[-3:] == "600"
    assert secrets_store.masked("tavily-key-123456") == "••••3456"
