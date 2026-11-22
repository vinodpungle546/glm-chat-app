#!/usr/bin/env python3
"""API keys for web search providers. Never stored in code, settings JSON, or sent to the browser.

Backends (SECRETS_BACKEND):
  keychain  macOS Keychain via /usr/bin/security (default on macOS)
  file      APP_DATA_DIR/secrets.json with 0600 permissions (default elsewhere; used by tests)
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import threading
from pathlib import Path

SERVICE_PREFIX = "Quorum search"
ACCOUNT = "quorum"
_LOCK = threading.Lock()


def backend() -> str:
    """Where secrets are kept: 'keychain' (macOS Keychain, the default on a Mac) or 'file' (a JSON file; used by
    tests and other systems). SECRETS_BACKEND overrides it.
    """
    default = "keychain" if platform.system() == "Darwin" and Path("/usr/bin/security").exists() else "file"
    return os.environ.get("SECRETS_BACKEND", default)


def _file() -> Path:
    """The path of the secrets file when the 'file' backend is used."""
    return Path(os.environ.get("SECRETS_FILE", str(Path(os.environ.get("APP_DATA_DIR", "./data")) / "secrets.json")))


def _read_file() -> dict:
    """All secrets from the file backend ({} if the file is missing or broken)."""
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_file(data: dict) -> None:
    """Saves the secrets file so only this user can read it (permissions 600), writing to a temporary file first
    so a crash never leaves a half-written file.
    """
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    tmp.replace(path)


def get(name: str) -> str:
    """The stored value of secret `name`, or '' when it isn't set."""
    if backend() == "keychain":
        result = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", ACCOUNT,
                                 "-s", f"{SERVICE_PREFIX}: {name}", "-w"], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else ""
    with _LOCK:
        return str(_read_file().get(name, ""))


def set(name: str, value: str) -> None:  # noqa: A001 (mirrors get/delete)
    """Stores secret `name` (an empty value deletes it)."""
    value = value.strip()
    if not value:
        delete(name)
        return
    if backend() == "keychain":
        # -U updates an existing item. The key is passed as an argument to a local process only.
        subprocess.run(["/usr/bin/security", "add-generic-password", "-a", ACCOUNT, "-s",
                        f"{SERVICE_PREFIX}: {name}", "-w", value, "-U"], check=True, capture_output=True)
        return
    with _LOCK:
        data = _read_file()
        data[name] = value
        _write_file(data)


def delete(name: str) -> None:
    """Removes secret `name` (no error if it doesn't exist)."""
    if backend() == "keychain":
        subprocess.run(["/usr/bin/security", "delete-generic-password", "-a", ACCOUNT, "-s",
                        f"{SERVICE_PREFIX}: {name}"], capture_output=True)
        return
    with _LOCK:
        data = _read_file()
        if data.pop(name, None) is not None:
            _write_file(data)


def masked(value: str) -> str:
    """A secret for display: only its last 4 characters, e.g. '••••a1b2'. Keys are never shown in full."""
    return f"••••{value[-4:]}" if len(value) >= 8 else ("••••" if value else "")
