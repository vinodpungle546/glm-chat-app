#!/usr/bin/env python3
"""Project storage for the GLM agent chat app.

Each project groups its own chat sessions, instructions, context documents,
skills, tools, and memory:

    PROJECTS_DIR/<project_id>/project.json   project config
    PROJECTS_DIR/<project_id>/sessions/      one JSON file per chat session

The built-in "default" project keeps its sessions in the legacy SESSIONS_DIR
so pre-existing conversations remain visible.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", "/app/projects"))
DEFAULT_PROJECT_ID = "default"
DEFAULT_PROJECT_NAME = "General"

DEFAULT_TOOLS = ["web_search", "datetime"]

PROJECT_FIELDS = {
    "instructions": "",
    "skills": [],
    "tools": DEFAULT_TOOLS,
    "memory": [],
    "documents": [],
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize(data: dict, project_id: str) -> dict:
    """Fill in missing fields so old/partial project files still load."""
    project = {"id": project_id}
    for field, default in PROJECT_FIELDS.items():
        value = data.get(field, None)
        if field == "instructions":
            value = str(value) if value is not None else default
        elif isinstance(value, list):
            value = list(value)  # copy so projects never share list instances
        else:
            value = list(default) if isinstance(default, list) else default
        project[field] = value
    project["name"] = str(data.get("name", "Untitled project"))
    project["created_at"] = data.get("created_at", _now())
    project["updated_at"] = data.get("updated_at", _now())
    return project


def project_path(project_id: str) -> Path:
    return PROJECTS_DIR / project_id / "project.json"


def create_project(name: str, instructions: str = "") -> dict:
    """Create a new project and return it."""
    name = name.strip()
    if not name:
        raise ValueError("Project name cannot be empty.")
    project_id = uuid.uuid4().hex
    project = _normalize(
        {"name": name, "instructions": instructions, "created_at": _now()}, project_id
    )
    save_project(project)
    return project


def get_or_create_default_project() -> dict:
    """Return the default project, creating it on first run."""
    existing = load_project(DEFAULT_PROJECT_ID)
    if existing is not None:
        return existing
    project = _normalize({"name": DEFAULT_PROJECT_NAME, "created_at": _now()}, DEFAULT_PROJECT_ID)
    save_project(project)
    return project


def list_projects() -> list[dict]:
    """Return all projects as light summaries, newest activity first."""
    if not PROJECTS_DIR.exists():
        return []
    projects = []
    for path in PROJECTS_DIR.glob("*/project.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        projects.append(
            {
                "id": path.parent.name,
                "name": data.get("name", "Untitled project"),
                "updated_at": data.get("updated_at", ""),
            }
        )
    projects.sort(key=lambda p: p["updated_at"], reverse=True)
    return projects


def load_project(project_id: str) -> dict | None:
    """Load one project (normalized), or None if it does not exist."""
    path = project_path(project_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return _normalize(data if isinstance(data, dict) else {}, project_id)


def save_project(project: dict) -> None:
    """Persist one project to disk."""
    project["updated_at"] = _now()
    path = project_path(project["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def delete_project(project_id: str) -> None:
    """Delete a project directory (config + sessions). The default is protected."""
    if project_id == DEFAULT_PROJECT_ID:
        raise ValueError("The default project cannot be deleted.")
    root = PROJECTS_DIR / project_id
    if root.exists():
        shutil.rmtree(root)


def sessions_dir_for(project: dict, default_dir: Path) -> Path:
    """Session directory for a project; the default project uses the legacy dir."""
    if project["id"] == DEFAULT_PROJECT_ID:
        return default_dir
    return PROJECTS_DIR / project["id"] / "sessions"
