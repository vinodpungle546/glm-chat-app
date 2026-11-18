#!/usr/bin/env python3
"""Tests for web_search.py: decision parsing, result formatting, search."""

from unittest.mock import patch

import web_search as ws


# ---------------------------------------------------------------------------
# parse_search_decision
# ---------------------------------------------------------------------------

def test_parse_decision_needs_search():
    text = '{"needs_search": true, "query": "latest python version"}'
    assert ws.parse_search_decision(text) == {
        "needs_search": True,
        "query": "latest python version",
    }


def test_parse_decision_no_search():
    text = '{"needs_search": false, "reply": "Paris is the capital of France."}'
    decision = ws.parse_search_decision(text)
    assert decision["needs_search"] is False
    assert "Paris" in decision["reply"]


def test_parse_decision_json_wrapped_in_prose():
    text = 'Sure! Here is my decision: {"needs_search": true, "query": "btc price"}'
    assert ws.parse_search_decision(text)["query"] == "btc price"


def test_parse_decision_plain_text_defaults_to_no_search():
    decision = ws.parse_search_decision("The capital of France is Paris.")
    assert decision["needs_search"] is False


def test_parse_decision_empty_text_defaults_to_no_search():
    assert ws.parse_search_decision("")["needs_search"] is False


def test_parse_decision_broken_json_defaults_to_no_search():
    assert ws.parse_search_decision("{needs_search: true, no quotes}")["needs_search"] is False


def test_parse_decision_ignores_non_string_query():
    text = '{"needs_search": true, "query": 42}'
    assert ws.parse_search_decision(text)["needs_search"] is False


# ---------------------------------------------------------------------------
# build_search_decision_messages
# ---------------------------------------------------------------------------

def test_build_decision_messages_includes_recent_transcript():
    recent = [
        {"role": "user", "content": "What is Python?"},
        {"role": "assistant", "content": "A programming language."},
        {"role": "user", "content": "Who released version 3.14?"},
    ]
    messages = ws.build_search_decision_messages(recent)
    assert messages[0]["role"] == "system"
    assert "needs_search" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert "Who released version 3.14?" in messages[1]["content"]


def test_build_decision_messages_limits_transcript_to_six():
    many = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
    transcript = ws.build_search_decision_messages(many)[1]["content"]
    assert "msg 3" not in transcript  # older than the last 6 messages
    assert "msg 4" in transcript
    assert "msg 9" in transcript


# ---------------------------------------------------------------------------
# format_results
# ---------------------------------------------------------------------------

def test_format_results_lists_all_entries():
    results = [
        {"title": "A", "url": "https://a", "snippet": "first"},
        {"title": "B", "url": "https://b", "snippet": "second"},
    ]
    text = ws.format_results(results)
    assert "[1] A" in text and "https://a" in text
    assert "[2] B" in text and "https://b" in text


def test_format_results_empty():
    assert "No web results" in ws.format_results([])


# ---------------------------------------------------------------------------
# search_web
# ---------------------------------------------------------------------------

def test_search_web_blank_query_returns_empty():
    assert ws.search_web("   ") == []


def test_search_web_normalizes_ddg_fields():
    fake_raw = [{"title": "T", "href": "https://x", "body": "snippet text"}]
    with patch.object(ws, "DDGS") as mock_ddgs:
        mock_ddgs.return_value.__enter__.return_value.text.return_value = fake_raw
        results = ws.search_web("test query")

    assert results == [{"title": "T", "url": "https://x", "snippet": "snippet text"}]


def test_search_web_returns_error_entry_on_failure():
    with patch.object(ws, "DDGS", side_effect=RuntimeError("network down")):
        results = ws.search_web("test query")

    assert len(results) == 1
    assert results[0]["title"] == "Search error"
    assert "network down" in results[0]["snippet"]
