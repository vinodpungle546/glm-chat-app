#!/usr/bin/env python3
"""Tests for projects.py: project CRUD, normalization, scoping."""

import json

import projects as pr


def make_projects_dir(tmp_path, monkeypatch):
    root = tmp_path / "projects_root"
    monkeypatch.setattr(pr, "PROJECTS_DIR", root)
    return root


def test_create_project_persists_config(tmp_path, monkeypatch):
    make_projects_dir(tmp_path, monkeypatch)
    project = pr.create_project("Analytics", "Be terse.")

    loaded = pr.load_project(project["id"])
    assert loaded["name"] == "Analytics"
    assert loaded["instructions"] == "Be terse."
    assert loaded["tools"] == pr.DEFAULT_TOOLS
    assert loaded["documents"] == []
    assert loaded["memory"] == []
    assert loaded["skills"] == []


def test_create_project_rejects_blank_name(tmp_path, monkeypatch):
    make_projects_dir(tmp_path, monkeypatch)
    try:
        pr.create_project("   ")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_list_projects_sorted_newest_first(tmp_path, monkeypatch):
    make_projects_dir(tmp_path, monkeypatch)
    first = pr.create_project("First")
    second = pr.create_project("Second")
    # Stamp distinct updated_at values directly so the order is deterministic
    stamps = {first["id"]: "2026-01-01T10:00:00", second["id"]: "2026-01-02T10:00:00"}
    for project_id, stamp in stamps.items():
        record = json.loads(pr.project_path(project_id).read_text(encoding="utf-8"))
        record["updated_at"] = stamp
        pr.project_path(project_id).write_text(json.dumps(record), encoding="utf-8")

    listed = pr.list_projects()

    assert [p["id"] for p in listed] == [second["id"], first["id"]]


def test_load_project_missing_returns_none(tmp_path, monkeypatch):
    make_projects_dir(tmp_path, monkeypatch)
    assert pr.load_project("missing-id") is None


def test_load_project_corrupt_json_returns_none(tmp_path, monkeypatch):
    root = make_projects_dir(tmp_path, monkeypatch)
    (root / "broken").mkdir(parents=True)
    (root / "broken" / "project.json").write_text("{oops", encoding="utf-8")
    assert pr.load_project("broken") is None


def test_save_project_roundtrip_memory(tmp_path, monkeypatch):
    make_projects_dir(tmp_path, monkeypatch)
    project = pr.create_project("P1")

    project["memory"].append("remembers things")
    pr.save_project(project)

    reloaded = pr.load_project(project["id"])
    assert reloaded["memory"] == ["remembers things"]
    assert reloaded["updated_at"] >= project["updated_at"]


def test_delete_project_removes_directory(tmp_path, monkeypatch):
    make_projects_dir(tmp_path, monkeypatch)
    project = pr.create_project("Doomed")

    pr.delete_project(project["id"])

    assert pr.load_project(project["id"]) is None
    assert pr.list_projects() == []


def test_default_project_cannot_be_deleted(tmp_path, monkeypatch):
    make_projects_dir(tmp_path, monkeypatch)
    pr.get_or_create_default_project()
    try:
        pr.delete_project(pr.DEFAULT_PROJECT_ID)
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_get_or_create_default_project_is_idempotent(tmp_path, monkeypatch):
    make_projects_dir(tmp_path, monkeypatch)
    first = pr.get_or_create_default_project()
    second = pr.get_or_create_default_project()
    assert first["id"] == second["id"] == pr.DEFAULT_PROJECT_ID


def test_sessions_dir_default_project_uses_legacy_dir(tmp_path, monkeypatch):
    make_projects_dir(tmp_path, monkeypatch)
    legacy = tmp_path / "sessions"
    project = pr.get_or_create_default_project()
    assert pr.sessions_dir_for(project, legacy) == legacy


def test_sessions_dir_other_project_is_scoped(tmp_path, monkeypatch):
    make_projects_dir(tmp_path, monkeypatch)
    project = pr.create_project("Scoped")
    expected = pr.PROJECTS_DIR / project["id"] / "sessions"
    assert pr.sessions_dir_for(project, tmp_path) == expected


def test_normalize_repairs_partial_data(tmp_path, monkeypatch):
    make_projects_dir(tmp_path, monkeypatch)
    (pr.PROJECTS_DIR / "half").mkdir(parents=True)
    (pr.PROJECTS_DIR / "half" / "project.json").write_text(
        json.dumps({"name": "Partial", "memory": "not-a-list"}), encoding="utf-8"
    )

    project = pr.load_project("half")

    assert project["name"] == "Partial"
    assert project["memory"] == []  # repaired to a list
    assert project["tools"] == pr.DEFAULT_TOOLS
