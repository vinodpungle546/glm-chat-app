"""The unified memory store: migration (with backup), placing imported facts, editing, export and
re-import without duplicates, relevance for chat. All data is synthetic."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

import claude_import
import imports
import memory_store
import people as people_store
import projects


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """Older separate stores, inside tmp_path (the conftest only lets the migration read these)."""
    monkeypatch.setattr(people_store, "PEOPLE_FILE", tmp_path / "people.json")
    monkeypatch.setattr(imports, "IMPORTS_DIR", tmp_path / "imports")
    monkeypatch.setattr(projects, "PROJECTS_DIR", tmp_path / "projects")
    (tmp_path / "people.json").write_text(json.dumps([
        {"id": "p1", "name": "Owen Hart", "role": "QA tester", "aliases": ["Owen"], "notes": "", "meetings": ["t1"],
         "voiceprints": [{"tid": "t1", "speaker": "SPEAKER_01", "vector": [0.1, 0.2]}], "updated_at": "2026-10-01T10:00:00"},
        {"id": "p2", "name": "Cian Byrne", "role": "Release manager", "aliases": [], "notes": "", "meetings": [],
         "voiceprints": [], "source": "claude.ai", "imported_at": "2026-11-01T09:00:00", "user_edited": ["role"],
         "evidence": ["Cian Byrne is the release manager for Project Atlas."]}]))
    (tmp_path / "imports").mkdir()
    (tmp_path / "imports" / "memory.json").write_text(json.dumps([
        {"id": "m1", "key": "k1", "text": "Prefers concise answers with tables.", "section": "Profile › Preferences",
         "project": "", "origin": "memory/profile.md", "imported_at": "2026-11-01T09:00:00", "edited": False},
        {"id": "m2", "key": "k2", "text": "Leads the release train every second Tuesday.", "section": "People › Cian Byrne",
         "project": "", "origin": "memory/people/cian-byrne.md", "imported_at": "2026-11-01T09:00:00", "edited": True},
        {"id": "m3", "key": "k3", "text": "Atlas ships the refund flow in Q1.", "section": "Atlas › Roadmap",
         "project": "Atlas", "origin": "memory/projects/Atlas/areas/roadmap.md", "imported_at": "2026-11-01T09:00:00"},
        {"id": "m4", "key": "k4", "text": "Weekly 1:1s happen on Thursdays.", "section": "Job search",
         "project": "", "origin": "memory/areas/job-search.md", "imported_at": "2026-11-01T09:00:00"}]))
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "abc.json").write_text(json.dumps(
        {"id": "abc", "name": "Payments team", "memory": ["Sprint reviews are on Fridays"], "updated_at": "2026-11-02"}))
    return tmp_path


def test_migration_folds_old_stores_and_backs_them_up(legacy):
    store = memory_store.load_store()
    backup = memory_store.MEMORY_DIR / store["backup"].split("/")[-1]
    assert (backup / "people.json").exists() and (backup / "imports" / "memory.json").exists()
    assert (backup / "projects" / "abc.json").exists()
    assert (legacy / "people.json").exists()  # old files are left as they were
    by = {(e["kind"], e["name"]): e for e in store["entries"]}
    owen = by[("person", "Owen Hart")]
    assert owen["voiceprints"] and owen["meetings"] == ["t1"] and owen["aliases"] == ["Owen"]
    cian = by[("person", "Cian Byrne")]
    assert cian["user_edited"] == ["role"] and cian["role"] == "Release manager"
    edited = next(f for f in cian["facts"] if "release train" in f["text"])
    assert edited["edited"] and edited["source"] == "claude.ai"
    assert by[("profile", "Profile")]["facts"][0]["heading"] == "Preferences"
    roadmap = by[("area", "Roadmap")]
    assert roadmap["project"] == "Atlas" and roadmap["facts"][0]["text"].startswith("Atlas ships")
    assert by[("area", "Job search")]["facts"][0]["text"] == "Weekly 1:1s happen on Thursdays."
    payments = by[("project", "Payments team")]
    assert payments["quorum_project"] == "abc" and payments["facts"][0]["source"] == "chat"


def test_migration_runs_once(legacy):
    memory_store.load_store()
    (legacy / "people.json").write_text("[]")
    assert any(e["name"] == "Owen Hart" for e in memory_store.entries("person"))


def test_people_module_reads_and_writes_person_entries():
    person = people_store.learn("t9", "SPEAKER_00", "Mei Lin Chen", "Designer", [0.3, 0.4], source="screen")
    entry = memory_store.get(person["id"])
    assert entry["kind"] == "person" and entry["voiceprints"][0]["source"] == "screen"
    memory_store.create("area", "Hiring")
    people_store.update(person["id"], {"team": "Design"})
    assert {e["name"] for e in memory_store.entries()} == {"Mei Lin Chen", "Hiring"}
    assert people_store.forget_voice(person["id"])["voiceprints"] == []


def test_facts_keep_source_and_date_and_dedupe():
    entry = memory_store.new_entry("area", "Hiring")
    fact, status = memory_store.add_fact(entry, "Interviews happen on Wednesdays.", source="chat")
    assert status == "added" and fact["source"] == "chat" and fact["date"]
    assert memory_store.add_fact(entry, "Interviews happen on Wednesdays.")[1] == "unchanged"
    assert memory_store.add_fact(entry, "Interviews happen on Wednesdays and Fridays.")[1] == "updated"
    fact["edited"] = True
    assert memory_store.add_fact(entry, "Interviews happen on Wednesdays and Thursdays.")[1] == "kept"


def test_edit_merge_delete_and_search():
    a = memory_store.create("person", "Priya")
    b = memory_store.create("person", "Priya Shah")
    memory_store.add_user_fact(a["id"], "Leads Mobile QA.")
    memory_store.add_user_fact(b["id"], "Works on Project Atlas.")
    assert memory_store.duplicates()["entries"] == [[a["id"], b["id"]]]
    merged = memory_store.merge(b["id"], [a["id"]])
    assert merged["aliases"] == ["Priya"] and len(merged["facts"]) == 2
    fact = merged["facts"][0]
    memory_store.update_fact(merged["id"], fact["id"], "Leads the Mobile QA team.")
    assert memory_store.search("mobile qa")[0]["name"] == "Priya Shah"
    assert memory_store.delete_facts(merged["id"], [fact["id"]]) == 1
    with pytest.raises(ValueError):
        memory_store.create("person", "priya shah")
    assert memory_store.delete(merged["id"])


def test_relevant_facts_for_a_chat_message():
    profile = memory_store.create("profile", "Profile")
    memory_store.add_user_fact(profile["id"], "Prefers concise answers with tables.")
    cian = memory_store.create("person", "Cian Byrne")
    memory_store.update(cian["id"], {"role": "Release manager"})
    memory_store.add_user_fact(cian["id"], "Runs the release train every second Tuesday.")
    other = memory_store.create("area", "Gardening")
    memory_store.add_user_fact(other["id"], "Tomatoes need full sun.")
    found = [r["text"] for r in memory_store.relevant("When is Cian's next release train?")]
    assert "About me: Prefers concise answers with tables." in found
    assert "Cian Byrne: Release manager" in found
    assert any("release train" in t for t in found)
    assert not any("Tomatoes" in t for t in found)


def test_export_is_claude_format_and_reimport_adds_no_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(imports, "IMPORTS_DIR", tmp_path / "imports")
    profile = memory_store.create("profile", "Profile", "About the user")
    memory_store.add_user_fact(profile["id"], "Prefers concise answers with tables.")
    owen = people_store.learn("t1", "SPEAKER_01", "Owen Hart", "QA tester", [0.5, 0.5])
    memory_store.add_user_fact(owen["id"], "Owns the regression suite.", "Work")
    atlas = memory_store.create("project", "Atlas", "Payments rebuild")
    memory_store.add_user_fact(atlas["id"], "Ships the refund flow in Q1.")
    roadmap = memory_store.create("area", "Roadmap", project="Atlas")
    memory_store.add_user_fact(roadmap["id"], "Beta opens in February.")
    data = memory_store.export_zip()
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert set(names) == {"memory/profile.md", "memory/people/owen-hart.md", "memory/projects/Atlas/index.md",
                          "memory/projects/Atlas/areas/roadmap.md"}
    text = zipfile.ZipFile(io.BytesIO(data)).read("memory/people/owen-hart.md").decode()
    assert "role: QA tester" in text and "## Work" in text and "0.5" not in text  # no voice vectors
    before = memory_store.counts()
    preview = imports.preview([("export.zip", data)])
    assert {m["status"] for m in preview["memory"]} == {"unchanged"}
    imports.commit(preview["preview_id"], None, None, None)
    after = memory_store.counts()
    assert after["facts"] == before["facts"] and after["person"] == before["person"]
    assert len(memory_store.entries("project")) == 1 and len(memory_store.entries("area")) == 1


def test_place_maps_claude_sections():
    assert memory_store.place("People › Cian Byrne › Notes", "people/cian.md") == ("person", "Cian Byrne", "", "Notes")
    assert memory_store.place("Profile › Work", "profile.md") == ("profile", "Profile", "", "Work")
    assert memory_store.place("Atlas › Roadmap › Q3", "projects/Atlas/areas/roadmap.md", "Atlas") == \
        ("area", "Roadmap", "Atlas", "Q3")
    assert memory_store.place("Atlas › Atlas", "projects/Atlas/index.md", "Atlas")[:2] == ("project", "Atlas")
    assert memory_store.place("Job search › Leads", "areas/job-search.md") == ("area", "Job search", "", "Leads")


def test_facts_for_meeting_only_mention_people_and_projects_in_it():
    owen = people_store.learn("t1", "S", "Owen Hart", "QA tester")
    memory_store.add_user_fact(owen["id"], "Owns the regression suite.")
    memory_store.create("person", "Zed Quill")
    lines = memory_store.facts_for_meeting("Owen, can you run the regression suite today?", [])
    assert "Owen Hart: QA tester" in lines and not any("Zed" in line for line in lines)


def test_public_hides_voice_vectors():
    person = people_store.learn("t1", "S", "Owen Hart", "", [0.1, 0.2], source="screen")
    out = memory_store.public(memory_store.get(person["id"]))
    assert "voiceprints" not in out and out["voiceprint_count"] == 1 and out["voiceprint_sources"] == {"screen": 1}
    assert claude_import.SOURCE == "claude.ai"


def test_reimport_moves_facts_an_older_import_filed_without_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(imports, "IMPORTS_DIR", tmp_path / "imports")
    store = memory_store.load_store()
    old_area = memory_store.new_entry("area", "Atlas")
    old_area["source"] = "claude.ai"
    memory_store.add_fact(old_area, "Atlas ships the refund flow in Q1.", "Roadmap", "claude.ai",
                          key=claude_import.key_for("Atlas ships the refund flow in Q1."))
    store["entries"].append(old_area)
    memory_store.save_store(store)
    item = {"key": claude_import.key_for("Atlas ships the refund flow in Q1."), "text": "Atlas ships the refund flow in Q1.",
            "section": "Atlas › Roadmap", "origin": "memory/projects/Atlas/areas/roadmap.md", "project": "Atlas"}
    store = memory_store.load_store()
    fact, status = memory_store.add_imported(store, item, "2026-11-20")
    assert status == "moved" and fact["text"].startswith("Atlas ships")
    kinds = {(e["kind"], e["name"], e.get("project", "")) for e in store["entries"]}
    assert ("area", "Roadmap", "Atlas") in kinds and ("area", "Atlas", "") not in kinds
    assert sum(len(e["facts"]) for e in store["entries"]) == 1


def _export(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(f"claude-memory-2026-11-20/{name}", text)
    return buf.getvalue()


def test_reimport_shows_moves_uses_front_matter_and_tidies_header_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(imports, "IMPORTS_DIR", tmp_path / "imports")
    store = memory_store.load_store()
    # What an older import left: header lines stored as facts, and a fact filed without its folder.
    junk = memory_store.new_entry("area", "Memory")
    junk["source"] = "claude.ai"
    for line in ['name: "roadmap"', "description: Atlas roadmap and release dates.", "aliases: [Plan, Dates]"]:
        memory_store.add_fact(junk, line, source="claude.ai", key=claude_import.key_for(line))
    flat = memory_store.new_entry("area", "Roadmap")
    flat["source"] = "claude.ai"
    memory_store.add_fact(flat, "Beta opens in February.", source="claude.ai",
                          key=claude_import.key_for("Beta opens in February."))
    profile = memory_store.ensure(store, "profile", "Profile")
    memory_store.add_fact(profile, "Name: Ana Ruiz; based in Lisbon.", source="claude.ai",
                          key=claude_import.key_for("Name: Ana Ruiz; based in Lisbon."))
    store["entries"] += [junk, flat]
    memory_store.save_store(store)

    data = _export({
        "profile.md": "---\nname: \"profile\"\ndescription: \"Ana Ruiz, product lead in Lisbon.\"\n---\n\n"
                      "- Name: Ana Ruiz; based in Lisbon.\n",
        "projects/Atlas/areas/roadmap.md": "---\nname: roadmap\ndescription: Atlas roadmap and release dates.\n"
                                           "aliases: [Plan, Dates]\n---\n\n- Beta opens in February.\n",
    })
    preview = imports.preview([("export.zip", data)])
    statuses = {m["text"]: m["status"] for m in preview["memory"]}
    assert statuses["Beta opens in February."] == "move"  # selected by default in the UI
    assert statuses["Name: Ana Ruiz; based in Lisbon."] == "unchanged"
    result = imports.commit(preview["preview_id"], None, None, None)
    assert result["memory_moved"] == 1 and result["memory_tidied"] == 3

    by = {(e["kind"], e["name"], e.get("project", "")): e for e in memory_store.entries()}
    assert ("area", "Memory", "") not in by and ("area", "Roadmap", "") not in by
    roadmap = by[("area", "Roadmap", "Atlas")]
    assert roadmap["description"] == "Atlas roadmap and release dates." and roadmap["aliases"] == ["Plan", "Dates"]
    assert "Beta opens in February." in [f["text"] for f in roadmap["facts"]]
    assert ("project", "Atlas", "") in by  # a project folder without index.md is still a project
    me = by[("profile", "Profile", "")]
    assert me["description"] == "Ana Ruiz, product lead in Lisbon."
    assert "Name: Ana Ruiz; based in Lisbon." in [f["text"] for f in me["facts"]]  # a real fact is kept


def test_front_matter_never_replaces_what_the_user_wrote(tmp_path, monkeypatch):
    monkeypatch.setattr(imports, "IMPORTS_DIR", tmp_path / "imports")
    area = memory_store.create("area", "Roadmap", "My own words.", project="Atlas")
    memory_store.update(area["id"], {"aliases": ["Plan"]})
    data = _export({"projects/Atlas/areas/roadmap.md": "---\nname: roadmap\ndescription: Imported text.\n"
                                                       "aliases: [Dates]\n---\n\n- Beta opens in February.\n"})
    imports.commit(imports.preview([("export.zip", data)])["preview_id"], None, None, None)
    entry = memory_store.get(area["id"])
    assert entry["description"] == "My own words." and entry["aliases"] == ["Plan"]
