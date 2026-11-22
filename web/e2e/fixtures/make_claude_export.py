#!/usr/bin/env python3
"""Synthetic claude.ai exports for the Playwright tests (all names and content invented).
Run: python web/e2e/fixtures/make_claude_export.py"""
import io
import json
import zipfile
from pathlib import Path

HERE = Path(__file__).parent

MEMORY = """**Work context**
- Mai Tran is a backend developer on the Payments team.
- Cian Byrne is the release manager for Project Atlas.
- Priya Shah leads the Mobile QA team.

**Personal context**
- Prefers short answers with tables.
"""
MEMORY_NEWER = MEMORY.replace("Cian Byrne is the release manager for Project Atlas.",
                              "Cian Byrne is the release manager for Project Atlas and Project Orion.")


def message(sender, text, at):
    return {"uuid": f"m-{abs(hash(text)) % 10**8}", "sender": sender, "text": text,
            "content": [{"type": "text", "text": text}], "created_at": at, "attachments": [], "files": []}


def conv(uuid, name, created, updated, pairs):
    msgs = []
    for i, (q, a) in enumerate(pairs):
        msgs += [message("human", q, f"{created[:10]}T09:{i:02d}:00Z"), message("assistant", a, f"{created[:10]}T09:{i:02d}:30Z")]
    return {"uuid": uuid, "name": name, "created_at": created, "updated_at": updated, "account": {"uuid": "acc-1"},
            "chat_messages": msgs, "new_field_from_the_future": True}


CONVERSATIONS = [
    conv("conv-refund", "Refund endpoint rollout", "2026-10-02T09:00:00Z", "2026-10-02T09:30:00Z", [
        ("Mai is deploying the refund endpoint fix to staging. What should QA retest?",
         "Retest full and partial refunds on staging after Mai's deploy, then run the payments regression.")]),
    conv("conv-atlas", "Atlas go-live checklist", "2026-11-05T10:00:00Z", "2026-11-05T10:20:00Z", [
        ("What goes on the Atlas go-live checklist?", "Regression sign-off, a rollback plan and release notes."),
        ("Who signs off?", "Cian as release manager, after QA signs off regression.")]),
    conv("conv-lunch", "Team lunch ideas", "2026-08-15T12:00:00Z", "2026-08-15T12:05:00Z", [
        ("Ideas for a team lunch near the office?", "A sushi place, a pizza place or a salad bar.")]),
]
NEWER = json.loads(json.dumps(CONVERSATIONS))
NEWER[1]["updated_at"] = "2026-11-19T10:00:00Z"
NEWER[1]["chat_messages"] += [message("human", "And the rollback owner?", "2026-11-19T09:58:00Z"),
                              message("assistant", "Mai owns the rollback of the refund service.", "2026-11-19T10:00:00Z")]


def zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, value in files.items():
            z.writestr(name, value if isinstance(value, (bytes, str)) else json.dumps(value, indent=1))
    return buf.getvalue()


common = {"projects.json": [{"uuid": "proj-atlas", "name": "Atlas", "description": "Payments programme", "prompt_template": "",
                             "docs": [], "is_starter_project": False, "created_at": "2026-09-01T00:00:00Z"}],
          "users.json": [{"uuid": "u-1", "full_name": "Sam Rivera", "email_address": "sam@example.com"}]}
(HERE / "claude-export.zip").write_bytes(zip_bytes({**common, "conversations.json": CONVERSATIONS,
                                                    "memories.json": [{"conversations_memory": MEMORY, "project_memories": {}}],
                                                    "files/attachment-1": b"\x89PNG..."}))
(HERE / "claude-export-newer.zip").write_bytes(zip_bytes({**common, "conversations.json": NEWER,
                                                          "memories.json": [{"conversations_memory": MEMORY_NEWER}]}))
(HERE / "conversations.json").write_text(json.dumps(CONVERSATIONS[:2], indent=1))
(HERE / "claude-memory.md").write_text(MEMORY)
(HERE / "broken.zip").write_bytes(b"PK\x03\x04 this is not really a zip")
(HERE / "bad.json").write_text('[{"uuid": "x", "chat_messages": [')
# The memory-only export (claude.ai memory settings → export): a ZIP of Markdown files with front matter.
def md(name, description, body, aliases=None):
    fm = f"---\nname: {name}\ndescription: {description}\n" + (f"aliases: [{', '.join(aliases)}]\n" if aliases else "") + "---\n"
    return fm + body


MEMORY_FOLDER = {
    "claude-memory-2026-11-19/profile.md": md("profile", "Who the user is",
        "- Sam Rivera works as a QA lead on the Payments programme.\n- Prefers short answers with tables.\n"),
    "claude-memory-2026-11-19/areas/release-management.md": md("release-management", "How releases are run",
        "- Weekly release train on Tuesdays.\n## Team\n- Cian Byrne is the release manager for Project Atlas.\n"
        "- Mai Tran is a backend developer on the Payments team.\n", ["releases"]),
    "claude-memory-2026-11-19/areas/job-search.md": md("job-search", "Looking at senior QA roles",
        "- Interested in test automation leadership roles.\n"),
    "claude-memory-2026-11-19/people/priya.md": md("Priya Shah", "Mobile QA lead",
        "- Priya Shah leads the Mobile QA team.\n- Prefers async updates.\n", ["Priya"]),
    "claude-memory-2026-11-19/projects/Atlas/index.md": md("Atlas", "Payments modernisation programme", ""),
    "claude-memory-2026-11-19/projects/Atlas/areas/job-search.md": md("job-search", "Atlas hiring",
        "- The Atlas team is hiring two testers.\n"),
}
(HERE / "claude-memory-folder.zip").write_bytes(zip_bytes(MEMORY_FOLDER))
print("ok")
