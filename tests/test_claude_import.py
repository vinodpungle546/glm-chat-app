#!/usr/bin/env python3
"""claude.ai export parser (synthetic exports only), entity extraction, import store and dedup."""

import io
import json
import zipfile

import pytest

import claude_import as ci
import imports
import people as people_store

MEMORY_TEXT = """**Work context**
Vinod Pungle works as a QA lead at Globant on the Payments app for Acme Bank.
- Cian Byrne is the release manager for Project Atlas.
- Priya Shah leads the Mobile QA team and works with Vinod on Project Atlas.
- Mai Tran (backend developer) owns the refund service.
- Owen reports to Priya.

**Personal context**
Prefers concise answers with tables.
"""


def conversation(uuid="c-1", name="Release checklist", updated="2026-11-10T09:00:00Z", extra_messages=0):
    messages = [
        {"uuid": "m1", "sender": "human", "text": "What goes on a release checklist for Atlas?",
         "content": [{"type": "text", "text": "What goes on a release checklist for Atlas?"}],
         "created_at": "2026-11-10T08:59:00Z", "attachments": [], "files": []},
        {"uuid": "m2", "sender": "assistant", "text": "",
         "content": [{"type": "text", "text": "Start with regression, sign-offs and a rollback plan."},
                     {"type": "tool_use", "name": "artifacts", "input": {"title": "Checklist", "content": "- Regression\n- Rollback"}}],
         "created_at": "2026-11-10T09:00:00Z"},
    ]
    for i in range(extra_messages):
        messages.append({"sender": "human", "text": f"Follow-up {i}", "created_at": f"2026-11-11T09:0{i}:00Z"})
    return {"uuid": uuid, "name": name, "created_at": "2026-11-10T08:59:00Z", "updated_at": updated,
            "account": {"uuid": "acc"}, "chat_messages": messages, "some_future_field": {"x": 1}}


def make_zip(files: dict[str, object]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, value in files.items():
            z.writestr(name, value if isinstance(value, (str, bytes)) else json.dumps(value))
    return buf.getvalue()


FULL_EXPORT = {
    "conversations.json": [conversation(), conversation("c-2", "Interview prep", "2026-11-12T10:00:00Z")],
    "projects.json": [{"uuid": "p-atlas", "name": "Atlas", "description": "Payments programme", "prompt_template": "x",
                       "docs": [{"filename": "a.md", "content": "..."}], "is_starter_project": False}],
    "users.json": [{"uuid": "u", "full_name": "Vinod Pungle", "email_address": "user@example.com"}],
    "memories.json": [{"conversations_memory": MEMORY_TEXT,
                       "project_memories": {"p-atlas": "Atlas go-live is planned for January."},
                       "account_uuid": "acc"}],
    "files/abc": b"\x89PNG binary",
}


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def test_full_zip_export_is_recognised_by_content():
    parsed = ci.parse_upload([("data-2026-11-20.zip", make_zip(FULL_EXPORT))])
    assert len(parsed.conversations) == 2
    assert parsed.account_name == "Vinod Pungle"
    assert [p["name"] for p in parsed.projects] == ["Atlas"]
    kinds = {f["name"]: f["kind"] for f in parsed.files}
    assert kinds == {"conversations.json": "conversations", "projects.json": "projects", "users.json": "account",
                     "memories.json": "memory"}
    assert any("Skipped 1 file" in w for w in parsed.warnings)  # the binary attachment
    atlas = [m for m in parsed.memory if m["project"] == "Atlas"]
    assert atlas and atlas[0]["text"] == "Atlas go-live is planned for January."


def test_conversation_fields_content_blocks_and_artifacts():
    conv = ci.parse_upload([("conversations.json", json.dumps([conversation()]).encode())]).conversations[0]
    assert conv["title"] == "Release checklist" and conv["uuid"] == "c-1"
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assert "rollback plan" in conv["messages"][1]["content"] and "**Checklist**" in conv["messages"][1]["content"]
    assert conv["created_at"].startswith("2026-11-10")


@pytest.mark.parametrize("shape", ["wrapped", "messages_key", "role_key", "string_content", "no_title"])
def test_schema_variations(shape):
    conv = conversation()
    if shape == "wrapped":
        payload = {"conversations": [conv]}
    elif shape == "messages_key":
        conv["messages"] = conv.pop("chat_messages")
        payload = [conv]
    elif shape == "role_key":
        for m in conv["chat_messages"]:
            m["role"] = {"human": "user", "assistant": "assistant"}[m.pop("sender")]
        payload = [conv]
    elif shape == "string_content":
        for m in conv["chat_messages"]:
            m["content"] = m["text"] or "An answer"
        payload = [conv]
    else:
        conv.pop("name")
        payload = [conv]
    parsed = ci.parse_upload([("x.json", json.dumps(payload).encode())])
    assert len(parsed.conversations) == 1 and len(parsed.conversations[0]["messages"]) == 2
    assert parsed.conversations[0]["title"]


def test_memory_json_shapes_and_text_and_paste():
    as_list = [{"content": "Owen is a QA tester on the Payments team."}]
    parsed = ci.parse_upload([("memory.json", json.dumps({"memories": as_list}).encode()),
                              ("claude-memory.md", b"## Team\n- Cian is the release manager.\n")],
                             pasted_text="Leah prefers async updates.")
    texts = [m["text"] for m in parsed.memory]
    assert "Owen is a QA tester on the Payments team." in texts
    assert "Cian is the release manager." in texts and "Leah prefers async updates." in texts
    assert {m["section"] for m in parsed.memory} >= {"Team"}


def test_corrupted_and_malformed_files_become_warnings():
    parsed = ci.parse_upload([("broken.zip", b"PK\x03\x04garbage"), ("conversations.json", b"[{\"uuid\": ")])
    assert any("not a valid ZIP" in w for w in parsed.warnings)
    assert any("not valid JSON" in w for w in parsed.warnings)
    assert any("No conversations or memory" in w for w in parsed.warnings)


def test_unknown_json_and_bad_conversations_are_skipped():
    payload = [{"uuid": "x", "name": "no messages", "created_at": "2026-01-01", "chat_messages": "oops"},
               conversation()]
    parsed = ci.parse_upload([("conversations.json", json.dumps(payload).encode()),
                              ("stats.json", json.dumps({"weekly_tokens": 3}).encode())])
    assert len(parsed.conversations) == 1
    assert any("without readable messages" in w for w in parsed.warnings)
    assert any("stats.json: not recognised" in w for w in parsed.warnings)


def test_same_conversation_twice_is_kept_once():
    data = json.dumps([conversation(), conversation()]).encode()
    assert len(ci.parse_upload([("a.json", data), ("b.json", data)]).conversations) == 1


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

def test_people_roles_teams_projects_relationships():
    parsed = ci.parse_upload([("users.json", json.dumps([{"full_name": "Vinod Pungle"}]).encode())], MEMORY_TEXT)
    people = {p["name"]: p for p in parsed.people}
    assert people["Cian Byrne"]["role"] == "release manager" and "Atlas" in people["Cian Byrne"]["projects"]
    assert people["Priya Shah"]["team"] == "Mobile QA" and "works with Vinod" in people["Priya Shah"]["relationships"]
    assert people["Mai Tran"]["role"] == "backend developer"
    assert people["Owen"]["relationships"] == ["reports to Priya"]
    assert people["Vinod Pungle"]["role"] == "QA lead" and people["Vinod Pungle"]["is_user"]
    assert "Acme Bank" not in people and "Globant" not in people  # organisations aren't people


def test_first_name_and_full_name_are_one_person():
    people = ci.extract_people(ci.memory_entries("Cian is the release manager.\nCian Byrne works on Project Atlas and reports to Grace.", "m"), [])
    assert [p["name"] for p in people if p["name"].startswith("Cian")] == ["Cian Byrne"]


# ---------------------------------------------------------------------------
# Import store: preview, selective commit, re-import, delete all
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(imports, "IMPORTS_DIR", tmp_path / "imports")
    monkeypatch.setattr(people_store, "PEOPLE_FILE", tmp_path / "people.json")
    return tmp_path


def test_preview_then_selective_commit(store):
    preview = imports.preview([("export.zip", make_zip(FULL_EXPORT))])
    assert preview["counts"]["conversations"] == 2 and preview["date_range"][0].startswith("2026-11-10")
    assert all(c["status"] == "new" for c in preview["conversations"])
    keep_memory = [m["key"] for m in preview["memory"][:2]]
    result = imports.commit(preview["preview_id"], keep_memory, [], ["c-1"])
    assert result["memory_added"] == 2 and result["conversations_added"] == 1 and result["people_added"] == 0
    convs = imports.list_conversations()
    assert len(convs) == 1 and convs[0]["read_only"] and convs[0]["imported"]["source"] == "claude.ai"
    assert convs[0]["title"] == "Release checklist" and convs[0]["updated_at"].startswith("2026-11-10")
    with pytest.raises(KeyError):
        imports.commit(preview["preview_id"], None, None, None)  # a preview is used once


def test_reimport_updates_instead_of_duplicating(store):
    imports.commit(imports.preview([("export.zip", make_zip(FULL_EXPORT))])["preview_id"], None, None, None)
    counts = imports.status()
    newer = dict(FULL_EXPORT)
    newer["conversations.json"] = [conversation(updated="2026-11-15T09:00:00Z", extra_messages=2),
                                   conversation("c-2", "Interview prep", "2026-11-12T10:00:00Z")]
    newer["memories.json"] = [{"conversations_memory": MEMORY_TEXT.replace(
        "Cian Byrne is the release manager for Project Atlas.",
        "Cian Byrne is the release manager for Project Atlas and Project Orion.")}]
    preview = imports.preview([("export-newer.zip", make_zip(newer))])
    status = {c["uuid"]: c["status"] for c in preview["conversations"]}
    assert status == {"c-1": "update", "c-2": "unchanged"}
    assert any(m["status"] == "update" and "Orion" in m["text"] for m in preview["memory"])
    result = imports.commit(preview["preview_id"], None, None, None)
    assert result["conversations_added"] == 0 and result["conversations_updated"] == 1
    assert result["memory_added"] == 0 and result["memory_updated"] == 1
    after = imports.status()
    assert after["conversations"] == counts["conversations"] and after["memory"] == counts["memory"]
    assert after["people"] == counts["people"]
    cian = people_store.find_by_name(people_store.load(), "Cian Byrne")
    assert "Orion" in cian["projects"]
    priya = people_store.find_by_name(people_store.load(), "Priya Shah")
    assert priya["team"] == "Mobile QA" and priya["role"] == "Mobile QA lead" and priya["source"] == "claude.ai"


def test_user_edits_survive_reimport(store):
    imports.commit(imports.preview([], MEMORY_TEXT)["preview_id"], None, None, None)
    entry = next(e for e in imports.load_memory() if e["text"].startswith("Cian"))
    imports.update_memory_entry(entry["id"], "Cian Byrne is our release manager (edited).")
    cian = people_store.find_by_name(people_store.load(), "Cian Byrne")
    people_store.update(cian["id"], {"role": "Head of releases"})
    imports.commit(imports.preview([], MEMORY_TEXT.replace("for Project Atlas", "for Project Atlas and Orion"))["preview_id"],
                   None, None, None)
    texts = [e["text"] for e in imports.load_memory()]
    assert "Cian Byrne is our release manager (edited)." in texts
    assert people_store.find_by_name(people_store.load(), "Cian Byrne")["role"] == "Head of releases"


def test_existing_meeting_person_gets_imported_fields(store):
    people_store.learn("t1", "SPEAKER_00", "Cian", "Release lead", [0.1, 0.2])
    imports.commit(imports.preview([], MEMORY_TEXT)["preview_id"], None, None, None)
    matches = [p for p in people_store.load() if p["name"].startswith("Cian")]
    assert len(matches) == 1 and matches[0]["voiceprints"] and "Atlas" in matches[0]["projects"]


def test_delete_all_imported_data(store):
    people_store.learn("t1", "SPEAKER_00", "Owen", "QA", None)
    imports.commit(imports.preview([("export.zip", make_zip(FULL_EXPORT))])["preview_id"], None, None, None)
    deleted = imports.delete_all()["deleted"]
    assert deleted["conversations"] == 2 and deleted["memory"] > 0
    assert imports.status() == {"memory": 0, "conversations": 0, "people": 0, "last_import": None}
    owen = people_store.find_by_name(people_store.load(), "Owen")
    assert owen is not None and "source" not in owen  # met in a meeting: kept, imported fields removed
    assert people_store.find_by_name(people_store.load(), "Mai Tran") is None


def test_merge_duplicates(store):
    imports.commit(imports.preview([], "## Work\nOwen tests the Android app.\n## Team\nOwen tests the Android app every day.")["preview_id"],
                   None, None, None)
    groups = imports.memory_duplicates()
    assert len(groups) == 1
    merged = imports.merge_memory_entries(groups[0])
    assert merged["text"] == "Owen tests the Android app every day." and len(imports.load_memory()) == 1
    a = people_store.upsert("Cian")
    b = people_store.upsert("Cian Byrne", "Release manager")
    assert people_store.duplicate_groups(people_store.load()) == [[a["id"], b["id"]]]
    kept = people_store.merge(b["id"], [a["id"]])
    assert kept["aliases"] == ["Cian"] and len(people_store.load()) == 1


# ---------------------------------------------------------------------------
# Memory-only export: a ZIP of Markdown files with front matter (as claude.ai produces it)
# ---------------------------------------------------------------------------

FOLDER_ZIP = (__import__("pathlib").Path(__file__).parent / "fixtures" / "claude-memory-folder.zip").read_bytes()


def test_folder_memory_export_uses_front_matter_and_folders():
    parsed = ci.parse_upload([("claude-memory-2026-11-19.zip", FOLDER_ZIP)])
    texts = [m["text"] for m in parsed.memory]
    assert not any(t.startswith(("name:", "description:", "aliases:", "---")) for t in texts)
    files = [f["name"] for f in parsed.files]
    assert "areas/job-search.md" in files and "projects/Atlas/areas/job-search.md" in files  # same name, two places
    assert len(files) == len(set(files))
    sections = {m["text"]: m["section"] for m in parsed.memory}
    assert sections["Cian Byrne is the release manager for Project Atlas."] == "Release management › Team"
    assert sections["How releases are run"] == "Release management"  # the description is memory too
    atlas = [m for m in parsed.memory if m["project"] == "Atlas"]
    assert [m["text"] for m in atlas] == ["Payments modernisation programme", "Atlas hiring", "The Atlas team is hiring two testers."]
    assert [p["name"] for p in parsed.projects] == ["Atlas"]


def test_folder_memory_export_people_and_user():
    parsed = ci.parse_upload([("claude-memory-2026-11-19.zip", FOLDER_ZIP)])
    people = {p["name"]: p for p in parsed.people}
    assert parsed.account_name == "Sam Rivera" and people["Sam Rivera"]["is_user"]
    priya = people["Priya Shah"]  # from people/priya.md: name and aliases from the front matter
    assert priya["role"] == "Mobile QA lead" and "Priya" in priya["aliases"] and priya["team"] == "Mobile QA"
    assert people["Cian Byrne"]["role"] == "release manager"
    assert not {"Team", "Payments", "Atlas"} & set(people)


def test_words_written_in_lower_case_are_not_names():
    entries = ci.memory_entries("- Contacts: Kieran is the QA lead.\n- Engineer at heart.\n- Keen on automation.\n"
                                "- Our contacts list is long.", "m")
    names = [p["name"] for p in ci.extract_people(entries, [])]
    assert names == ["Kieran"]
