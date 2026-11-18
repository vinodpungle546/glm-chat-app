#!/usr/bin/env python3
"""Regression tests: structured skills persist correctly per project."""

import projects as pr
import tools as tl


def test_structured_skill_survives_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "PROJECTS_DIR", tmp_path / "prj")
    project = pr.create_project("P")
    project["skills"].append(
        {
            "name": "BarChart",
            "description": "Use when data can be charted",
            "content": "Create a bar chart from context data",
        }
    )
    pr.save_project(project)

    reloaded = pr.load_project(project["id"])
    assert reloaded["skills"][0]["name"] == "BarChart"
    assert reloaded["skills"][0]["description"] == "Use when data can be charted"
    assert reloaded["skills"][0]["content"] == "Create a bar chart from context data"


def test_available_skills_keeps_structured_fields():
    project = {"skills": [{"name": "S1", "description": "when", "content": "how"}, "legacy"]}
    skills = tl_available = __import__("tools").available_skills(project)
    assert skills[0] == {"name": "S1", "description": "when", "content": "how"}
    assert skills[1]["name"] == "legacy" and skills[1]["content"] == "legacy"


def test_skills_normalize_mixed_legacy_and_structured():
    project = {"skills": [{"name": "A", "description": "d", "content": "c"}, "legacy"]}
    skills = __import__("tools").available_skills(project)
    assert [s["name"] for s in skills] == ["A", "legacy"]
