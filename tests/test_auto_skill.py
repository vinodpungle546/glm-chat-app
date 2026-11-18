#!/usr/bin/env python3
"""Agent autonomously decides to load a skill based on its description."""

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


def test_auto_skill_without_slash(monkeypatch):
    state = FakeState(
        web_search_enabled=False,
        messages=[{"role": "user", "content": "chart the release burndown"}],
        project={
            "id": "p1",
            "name": "T",
            "instructions": "",
            "skills": [
                {
                    "name": "BarChart",
                    "description": "Use when data should be presented as a bar chart",
                    "content": "CREATE A BAR CHART",
                }
            ],
            "tools": [],
            "memory": [],
            "documents": [],
        },
    )
    monkeypatch.setattr(app_module.st, "session_state", state, raising=False)

    def scripted_reply(messages):
        if any("Full content of skill" in m["content"] for m in messages):
            return "Here is your bar chart."
        return '{"skill": "BarChart"}'

    with patch.object(app_module, "stream_assistant_reply", side_effect=scripted_reply):
        reply = app_module.generate_reply("chart the release burndown")

    assert reply == "Here is your bar chart."


def test_prompt_tells_model_to_decide_autonomously():
    import tools as tl

    project = {
        "instructions": "",
        "skills": [{"name": "S", "description": "d", "content": "c"}],
        "tools": [],
        "memory": [],
    }
    prompt = tl.build_agent_system_prompt("Base", project, "", "")
    assert "decide autonomously" in prompt
