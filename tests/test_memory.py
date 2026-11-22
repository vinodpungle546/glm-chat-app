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
