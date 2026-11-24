"""Every test gets its own memory store, and the one-time migration can only ever read files
inside that test's tmp_path, never the real data/ folder."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def private_memory_store(tmp_path, monkeypatch):
    """Runs for every test: gives it its own memory store, cloud-provider settings and secrets file inside the
    test's temporary folder, so tests never touch the real data in ./data or the Keychain.
    """
    import memory_store

    monkeypatch.setattr(memory_store, "MEMORY_DIR", tmp_path / "memory")
    real = memory_store._legacy_paths

    def inside(path) -> bool:
        """True for a path inside this test's temporary folder."""
        return str(path).startswith(str(tmp_path))

    def legacy_paths():
        """The old stores the migration may read, limited to files inside the test's temporary folder (anything
        else is replaced by a path that doesn't exist).
        """
        paths = real()
        return {"people": paths["people"] if inside(paths["people"]) else tmp_path / "no-people.json",
                "imported": paths["imported"] if inside(paths["imported"]) else tmp_path / "no-memory.json",
                "projects": [p for p in paths["projects"] if inside(p)]}

    monkeypatch.setattr(memory_store, "_legacy_paths", legacy_paths)

    # Cloud providers and their keys: never the real config or the macOS Keychain.
    import cloud_providers
    monkeypatch.setattr(cloud_providers, "CONFIG_FILE", tmp_path / "cloud_providers.json")
    monkeypatch.setenv("SECRETS_BACKEND", "file")
    monkeypatch.setenv("SECRETS_FILE", str(tmp_path / "secrets.json"))
    yield


@pytest.fixture(autouse=True)
def no_ollama_capability_lookups(monkeypatch):
    """Tests never ask the real Ollama which models can see images."""
    import model_registry

    vision = ("gemma4", "gemma3", "-vl", "llava", "minicpm-v", "moondream")
    monkeypatch.setattr(model_registry, "capabilities",
                        lambda name: ["completion"] + (["vision"] if any(v in name for v in vision) else []))


@pytest.fixture(autouse=True)
def no_real_transcriber(monkeypatch):
    """Tests never reach the transcriber running on this Mac (a closed port; tests that need one fake it)."""
    import meetings

    monkeypatch.setattr(meetings, "TRANSCRIBER_URL", "http://127.0.0.1:9")


@pytest.fixture(autouse=True)
def no_real_gaia(request, monkeypatch):
    """Tests never reach the real GAIA service (the cloud model is the default chat model): a closed
    port instead. test_cloud_regression keeps the real address, because it pins it and fakes the network."""
    if request.module.__name__.endswith("test_cloud_regression"):
        return
    import llm_backends

    monkeypatch.setattr(llm_backends, "GAIA_ENDPOINT", "http://127.0.0.1:9/v1/chat/completions")


@pytest.fixture(autouse=True)
def no_real_ollama_window_check(monkeypatch):
    """The check of the loaded model's window (GET /api/ps) would reach the real Ollama: it reports nothing loaded.
    tests/test_llm_backends.py tests the real check against a faked network."""
    import llm_backends

    monkeypatch.setattr(llm_backends, "_loaded_window", lambda model: None)


@pytest.fixture(autouse=True)
def no_real_meeting_files(tmp_path, monkeypatch):
    """The API's per-meeting files (attendee edits, labels, summaries) go to the test's folder, never ./data."""
    import sys

    main = sys.modules.get("api.main")
    if main is not None:
        for name, folder in (("ATTENDEES_DIR", "attendees"), ("LABELS_DIR", "labels"), ("SUMMARIES_DIR", "summaries")):
            monkeypatch.setattr(main, name, tmp_path / folder)
