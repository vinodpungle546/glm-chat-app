#!/usr/bin/env python3
"""Tests for app.py: session persistence and helpers.

app.py imports streamlit at module level, which is safe to import (it only
configures when running as a script). Session functions now take an explicit
sessions_dir argument, so tests pass tmp_path directly.
"""

import json

import app as app_module


def test_session_title_from_first_user_message():
    messages = [
        {"role": "user", "content": "What is the capital of France?"},
        {"role": "assistant", "content": "Paris."},
    ]
    assert app_module.session_title(messages) == "What is the capital of France?"


def test_session_title_truncates_long_messages():
    long_text = "x" * 100
    title = app_module.session_title([{"role": "user", "content": long_text}])
    assert len(title) == 41
    assert title.endswith("\u2026")


def test_session_title_empty_conversation():
    assert app_module.session_title([]) == "Untitled chat"


def test_save_and_load_session_roundtrip(tmp_path):
    messages = [{"role": "user", "content": "hello"}]

    app_module.save_session(tmp_path, "abc123", "hello", messages)

    assert (tmp_path / "abc123.json").exists()
    assert app_module.load_session(tmp_path, "abc123") == messages


def test_save_session_without_id_is_noop(tmp_path):
    app_module.save_session(tmp_path, "", "title", [])
    assert list(tmp_path.glob("*.json")) == []


def test_load_session_missing_returns_none(tmp_path):
    assert app_module.load_session(tmp_path, "does-not-exist") is None


def test_load_session_corrupt_json_returns_none(tmp_path):
    (tmp_path / "bad.json").write_text("not valid json", encoding="utf-8")
    assert app_module.load_session(tmp_path, "bad") is None


def test_delete_session_removes_file(tmp_path):
    app_module.save_session(tmp_path, "abc", "t", [])

    app_module.delete_session(tmp_path, "abc")

    assert not (tmp_path / "abc.json").exists()


def test_delete_session_missing_is_safe(tmp_path):
    app_module.delete_session(tmp_path, "nope")  # must not raise


def test_list_sessions_sorted_newest_first(tmp_path):
    for session_id, updated in [("a", "2026-01-01T10:00:00"), ("b", "2026-01-02T10:00:00")]:
        record = {"id": session_id, "title": session_id, "updated_at": updated, "messages": []}
        (tmp_path / f"{session_id}.json").write_text(json.dumps(record), encoding="utf-8")

    sessions = app_module.list_sessions(tmp_path)

    assert [s["id"] for s in sessions] == ["b", "a"]


def test_list_sessions_empty_dir_returns_empty(tmp_path):
    assert app_module.list_sessions(tmp_path) == []


def test_list_sessions_missing_dir_returns_empty(tmp_path):
    assert app_module.list_sessions(tmp_path / "nope") == []


def test_list_sessions_skips_corrupt_files(tmp_path):
    (tmp_path / "bad.json").write_text("{broken", encoding="utf-8")
    assert app_module.list_sessions(tmp_path) == []
