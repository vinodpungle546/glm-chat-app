#!/usr/bin/env python3
"""Chat paperclip attachments are session-scoped and preferred by the model."""

from unittest.mock import patch

import app as app_module


class FakeState(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, key, value):
        self[key] = value


def test_generate_reply_prefers_chat_attachments(monkeypatch):
    state = FakeState({
        "web_search_enabled": False,
        "messages": [{"role": "user", "content": "summarize"}],
        "project": {
            "id": "p1", "name": "T", "instructions": "", "skills": [],
            "tools": [], "memory": [],
            "documents": [{"name": "project_doc.txt", "text": "PROJECT DOC CONTENT"}],
        },
        "chat_attachments": [
            {"name": "chat_file.txt", "text": "CHAT FILE SECRET-XYZ"}
        ],
    })
    monkeypatch.setattr(app_module.st, "session_state", state, raising=False)
    captured = {}

    def fake_stream(messages):
        captured["messages"] = messages
        return "ok"

    with patch.object(app_module, "stream_assistant_reply", side_effect=fake_stream):
        app_module.generate_reply("summarize")

    system_content = captured["messages"][0]["content"]
    assert "Chat attachment: chat_file.txt" in system_content
    assert "Prefer them over project context" in system_content
    assert "SECRET-XYZ" in system_content
    assert "PROJECT DOC CONTENT" in system_content  # still available


def test_chat_attachment_context_empty_without_files(monkeypatch):
    state = {"chat_attachments": []}
    monkeypatch.setattr(app_module.st, "session_state", state, raising=False)
    assert app_module.build_chat_attachment_context() == ""
