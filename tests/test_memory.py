#!/usr/bin/env python3
"""Tests for project memory: persistence + agent auto-update from chat."""

from unittest.mock import patch

import projects as pr


def test_memory_entries_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "PROJECTS_DIR", tmp_path / "prj")
    project = pr.create_project("P")
    project["memory"].append("User prefers Python")
    project["memory"].append("Deploy target is GCP")
    pr.save_project(project)

    reloaded = pr.load_project(project["id"])
    assert "User prefers Python" in reloaded["memory"]
    assert "Deploy target is GCP" in reloaded["memory"]


def test_memory_entry_update_and_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "PROJECTS_DIR", tmp_path / "prj")
    project = pr.create_project("P")
    project["memory"].append("Old fact")
    pr.save_project(project)

    project = pr.load_project(project["id"])
    project["memory"][0] = "Updated fact"
    pr.save_project(project)
    assert pr.load_project(project["id"])["memory"] == ["Updated fact"]

    project = pr.load_project(project["id"])
    project["memory"].pop(0)
    pr.save_project(project)
    assert pr.load_project(project["id"])["memory"] == []


def test_merge_new_memories_dedup():
    import app as app_module

    project = {"memory": ["User prefers Python"]}
    added = app_module.merge_new_memories(project, ["User prefers Python", "Uses macOS"])
    assert added == ["Uses macOS"]
    assert project["memory"] == ["User prefers Python", "Uses macOS"]


def test_merge_new_memories_ignores_non_strings():
    import app as app_module

    project = {"memory": []}
    added = app_module.merge_new_memories(project, [42, None, "Real fact", "  "])
    assert added == ["Real fact"]
    assert project["memory"] == ["Real fact"]


def test_update_memory_saves_new_facts(monkeypatch, tmp_path):
    import app as app_module

    monkeypatch.setattr(pr, "PROJECTS_DIR", tmp_path / "prj")
    project = pr.create_project("P")

    class FakeState(dict):
        def __getattr__(self, name):
            try:
                return self[name]
            except KeyError as exc:
                raise AttributeError(name) from exc

        def __setattr__(self, key, value):
            self[key] = value

    state = FakeState(project=project, messages=[
        {"role": "user", "content": "I only deploy to GCP"},
        {"role": "assistant", "content": "Got it."},
    ])
    monkeypatch.setattr(app_module.st, "session_state", state, raising=False)

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '["Deploy target is GCP"]'}}]}

    with patch.object(app_module.requests, "post", return_value=FakeResponse()):
        app_module.update_memory_from_conversation()

    reloaded = pr.load_project(project["id"])
    assert "Deploy target is GCP" in reloaded["memory"]
