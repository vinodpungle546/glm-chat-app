# Quorum — private meeting intelligence

Quorum transcribes meeting recordings offline, works out who said what, and writes minutes. You can also chat about your meetings and documents. Each conversation can use a **local** model running in Ollama on your Mac, where nothing leaves the laptop, or the **cloud** model (GLM-5.3 Flash via Globant GAIA).

```
Browser ── Next.js web app (web/, :3000)
              │  REST + Server-Sent Events
              ▼
           Python API (api/main.py, :8000) ── cloud ──▶ Globant GAIA (GLM-5.3 Flash)
              │                             ── local ──▶ Ollama :11434 (Gemma 4 E4B, Qwen3, …)
              └──────────────────────────────────────▶ Transcriber :8765 (Whisper + pyannote, offline)
```

## Features
- **Models:** pick a model in the sidebar, grouped into **Local** and **Cloud**. Each local model shows a status badge: *Ready*, *Not downloaded* or *Ollama not running*. Your choice is remembered. Download, cancel or remove local models under **Settings → Local models**. Add Claude, ChatGPT models or any OpenAI-compatible endpoint under **Settings → Models** (see [Cloud models](#cloud-models-claude-chatgpt-and-others)).
- **Meetings:** add a recording in one of four ways:
  - record the call in Quorum (your microphone and the call on separate tracks, so your words are confirmed as yours; see [Record a call](#record-a-call))
  - upload a video or audio file of any size, including Mac screen recordings (`.mov`)
  - pick it from a watch folder
  - paste conversation text, or import a Teams/Zoom `.vtt`/`.srt` file
- **Live transcription:** progress and the text appear as they're transcribed, and you can cancel. Starting a new run clears the previous output.
- **Speaker identification:** speakers are separated by voice. Each one gets a likely **name and role** with a **confidence level** and the evidence behind it. The guess draws on:
  - names on the video: in Teams/Zoom screen recordings, the name on the highlighted (active speaker) tile, read on this Mac
  - voices you labelled in earlier meetings, or whose names were on screen
  - names said in the meeting ("Owen, can you…", "thanks Owen", "I'm Owen")
  - people and facts in Memory
  - what each person says (someone assigning tasks is likely a lead, someone reporting test results is likely QA)

  Correct a label and it's saved to **Memory → People**, voice fingerprint included, so the same people are recognised next time. With a cloud model selected, it then reviews the names; you see every change and can keep or revert it (see [Speakers](#speakers-names-on-screen-voices-and-cloud-review)).
- **Meeting notes:** professional notes from the built-in *Professional Meeting Notes* skill, with any model, local or cloud (see [Meeting notes](#meeting-notes)).
  - The meeting type is detected automatically (stand-up, sprint planning, retro, steering review, client call, go/no-go, postmortem, 1:1, interview, brainstorm), and you can override it.
  - Choose a detail level (Brief / Standard / Detailed) and which sections to show.
  - Every decision and action item links to its moment in the transcript.
  - Edit the notes inline, then export them as Markdown, Word, PDF, email-ready HTML or an action-items CSV, copy them, or draft a follow-up email.
- **Correct with another transcript:** add the Voice Memos recording or its text, a claude.ai transcript, or any other transcript of the same meeting, in any combination, and Quorum corrects words and who said what. Corrections are applied at once, underlined, and can be reverted one by one or all at once (see [Correct with another transcript](#correct-with-another-transcript)).
- **Meeting check:** during a call (with the live transcript) or afterwards, Quorum compares the meeting with earlier meetings and memory and alerts you to conflicts, things to raise with everyone, items still pending, and actions for you in your role (see [Meeting check](#meeting-check)).
- **Import from Claude:** bring your claude.ai memory and chats in from the files claude.ai lets you export (see [Import from claude.ai](#import-from-claudeai)). Imported chats are read-only, tagged "Imported", and can be continued in a new chat with any model.
- **Search** (<kbd>⌘</kbd><kbd>K</kbd>): one search across your chats, imported chats, transcripts, meeting notes, memory, connectors and, if you turn it on, the web.
  - Results are grouped by source, with filters and links.
  - The assistant can also search during a chat and answers with inline citations.
- **Chat:** streaming answers, attachments and meeting transcripts, project skills (type `/`), connectors (MCP) and long-term memory. A cloud model asks before sending anything that looks confidential.
- **Memory:** one store on this Mac, organised like claude.ai memory (you, people, projects, topics), used in chats, speaker identification and meeting notes (see [Memory](#memory)).
- **Connectors:** find MCP servers in the official MCP Registry and add them, remote or as local packages (see [Connectors](#connectors)).
- **Settings:** General, Projects, Memory, Speakers, Skills, Connectors, Search, Import from Claude, Models (cloud) and Local models.
- **Interface:** light and dark themes (follows the system by default), keyboard shortcuts (press <kbd>⌘</kbd><kbd>/</kbd> to list them), and layouts for desktop and tablet.

## Setup (once per machine)

Requirements: macOS on Apple Silicon (16 GB RAM or more), Python 3.12+, Node 20+, [Homebrew](https://brew.sh). On Windows or Linux, chat with local models works, but the transcriber needs Apple Silicon (it uses MLX).

1. **Cloud credentials (optional):**
   ```bash
   cp .env.example .env
   ```
   Then fill in the `GAIA_*` values.
2. **Local chat models.** This installs and checks Ollama 0.31+, downloads Gemma 4 E4B (about 9.6 GB, with progress shown and no time limit), and runs a test prompt:
   ```bash
   scripts/setup-local-models.sh                     # macOS / Linux
   scripts/setup-local-models.sh gemma4:e4b qwen3:8b # several models
   # Windows: powershell -ExecutionPolicy Bypass -File scripts\setup-local-models.ps1
   ```
   You can also download models later from **Settings → Local models**.
3. **Offline transcriber** (macOS). This installs ffmpeg and a Python 3.12 virtualenv, then downloads Whisper and pyannote:
   ```bash
   scripts/setup_local_ai.sh
   ```
   Speaker labels need a Hugging Face token. You also need to accept the terms of the pyannote models; the script lists them. After setup, everything runs offline.

   The script also builds `transcriber/bin/quorum-ocr`, a small Swift helper that reads names from video frames with Apple's on-device text recognition (needs the Xcode command line tools: `xcode-select --install`). Without it, names on screen stay off. After updating Quorum, restart the transcriber with `scripts/start_local_ai.sh restart`.

## Run
```bash
scripts/start_local_ai.sh      # Ollama + transcriber (native, uses the Apple GPU)
scripts/start_app.sh           # API on :8000 + web app on http://localhost:3000
scripts/start_app.sh dev       # same, with the Next.js dev server (hot reload)
scripts/start_app.sh api       # restart only the API (e.g. after updating the Python code)
scripts/start_app.sh stop      # stop the API and web app
```
`start_app.sh` creates `.venv` with the Python dependencies on first run, and installs and builds the web app. Data stays in `./data`: projects, chats, people, speaker labels and summaries.

### Configuration
- **Models:** they live in `models.json`, not in code. The default is `local:gemma4:e4b`. Any other model you `ollama pull` appears in the picker automatically.
- **Environment variables:** see `.env.example` (cloud `GAIA_*`, `OLLAMA_CONTEXT_LENGTH`, transcriber URLs).

## Import from claude.ai

claude.ai has no API for this, so Quorum imports the files claude.ai lets you export. It never logs in to claude.ai and never asks for your password. Everything stays in `./data`.

1. **Export your data:**
   - **Everything (memory + all chats):** claude.ai → **Settings → Privacy → Export data**. claude.ai emails you a link to a ZIP.
   - **Memory only:** claude.ai → Settings → memory → **export memory**. You get text or Markdown.
2. **Preview it:** in Quorum, open **Settings → Import from Claude**. Drop in the ZIP as it is, single JSON files from it, or the memory file, or paste the memory text. Then choose **Preview import**.
3. **Check the preview:**
   - What it shows: the number of conversations and their date range, the memory entries, and the people found with their role, team, projects and relationships.
   - Each item is marked *New*, *Update* or *Already imported*.
   - Problems (a damaged ZIP, malformed JSON, unknown files) appear as warnings. They are never imported.
   - Untick anything you don't want, then choose **Import**.
4. **Where the data goes:**
   - **Memory:** appears under **Settings → Memory → Imported from claude.ai**, with its source and import date. You can edit entries, merge duplicates or delete them there.
   - **People:** go into **Memory → People**, where duplicate people can be merged.
   - **Chats:** appear in the sidebar, tagged *Imported*. The eye button hides or shows them.
5. **Re-importing:** importing a newer export updates what you imported before instead of duplicating it. Matching works like this:
   - Chats match by their claude.ai id.
   - Memory matches by wording.
   - People match by name.
   - Entries you edited keep your wording.
6. **Removing it:** **Delete all imported data** (in the same tab, with confirmation) removes everything that was imported.

**How imported data is used:**
- **Chats:** imported memory goes into the chat's long-term memory.
- **Speaker identification:** people's roles, teams and projects become extra evidence for naming speakers, for example "Matched to Cian Byrne: release manager in claude.ai memory". People who appear in related past chats strengthen a guess, for example "appears in 2 past conversations about these topics".
- **Search:** imported chats are indexed for search.
- **Cloud models:** nothing imported is sent to a cloud model unless you pick a cloud model for that chat. The first time that happens, Quorum shows a one-time notice.

### claude.ai export format (as observed)
The export isn't documented and changes over time. Quorum therefore recognises files by their content, not their names, and ignores fields it doesn't know. This is the structure it reads:

| File | Content |
|---|---|
| `conversations.json` | `[{uuid, name, created_at, updated_at, account{uuid}, chat_messages: [{uuid, sender: "human"\|"assistant", text, created_at, content: [{type: "text"\|"tool_use"\|"tool_result", text, input{title, content}}], attachments: [{file_name, extracted_content}], files}]}]` |
| `projects.json` | `[{uuid, name, description, prompt_template, created_at, docs: [{filename, content}]}]` |
| `users.json` | `[{uuid, full_name, email_address, verified_phone_number}]` (only the name is used, to recognise you in memory) |
| `memories.json` (newer exports) | memory text, e.g. `[{conversations_memory: "…", project_memories: {project-uuid: "…"}}]` |
| other files | attachments and images are skipped, with a note in the preview |

The **memory export** (from claude.ai's memory settings) is different: a ZIP named like `claude-memory-YYYY-MM-DD.zip`, holding a folder of Markdown files. Each file starts with YAML front matter (`name`, `description`, optional `aliases`), followed by bullets, sometimes grouped under `##` headings:

| Path | Meaning | Imported as |
|---|---|---|
| `profile.md` | about you | memory section "Profile"; your name is taken from it and you're marked "(you)" |
| `areas/<topic>.md` | a topic, e.g. `job-search.md` | memory section named after the topic; `##` headings become sub-sections |
| `people/<name>.md` | one person | a person (name, aliases and role from the file), plus their facts |
| `projects/<Project>/index.md`, `projects/<Project>/areas/*.md`, `projects/<Project>/profile.md` | a project's memory | memory linked to that project |

The `description` in the front matter is imported as a memory entry too. The same file name can appear in more than one folder (for example `areas/job-search.md` and `projects/CV Improvement/areas/job-search.md`), so files are identified by their path.

How the parser handles variations:
- **Top-level shape:** both a bare list and a wrapper object (`{"conversations": [...]}`) are accepted.
- **Message keys:** `messages` is accepted as well as `chat_messages`, and `role` as well as `sender`.
- **Message text:** it can come from a `content` string, from `content` blocks, or from `text`.
- **Artifacts:** `tool_use` artifacts are kept as text.

## Search

Press <kbd>⌘</kbd><kbd>K</kbd>, or use the search bar at the top.

**What's searched:**
- Your chats, including imported ones
- Every transcript line
- Meeting notes
- Memory and people
- Connectors you enable
- The web, if enabled

**How it ranks:** results combine keyword ranking (BM25) with meaning-based search.
- Meaning-based search uses a local embedding model, `nomic-embed-text` in Ollama. Install it from **Settings → Search**.
- Without the embedding model, search uses keywords only.
- The index is built in the background on this Mac.

**Filters:** by source and by date range.

**Web search setup** (**Settings → Search**):
1. Turn on **Include web results**.
2. Pick a provider:
   - **Tavily:** the default. Built for AI agents, with a free tier.
   - **Brave Search**
   - **Exa**
   - **DuckDuckGo:** no key needed, but often rate-limited.
3. Paste the API key, then **Test connection**.

If you used Quorum's web search before this setting existed, it stays on, using DuckDuckGo, until you pick another provider.

**Where the key is kept:** in the macOS Keychain, or in a private `data/secrets.json` (permissions 600) on other systems. It's never stored in code or settings files, and never sent to the browser.

**When the web isn't available:** if the key is missing or invalid, you hit a rate limit, or you're offline, search falls back to your own data and says why.

**Privacy rules:**
- Only the words you search for are sent to the provider.
- When the assistant searches on its own, queries that mention people's names from memory, speaker names or confidential terms are not sent to the web unless you typed those words yourself.
- Chats on a local model never use the web.

## Meeting notes

Meeting notes come from a skill folder, `skills/meeting-notes/`:
- `SKILL.md`: the writing rules every model follows. For example, only what was said; "Unassigned" or "TBD" plus a needs-follow-up note; verb-first actions with one owner; timestamps on every decision and action; actual dates instead of "next Friday"; sensitive topics kept high level unless you choose to include them.
- `schema.json`: the JSON the model must return.
- `templates/<type>.md`: one template per meeting type. `templates/partials/` holds the shared sections (header, summary, decisions, action items, …).

**How notes are generated:**
1. The model returns JSON.
2. Quorum validates it against `schema.json`. If it's invalid, Quorum retries once with the errors. If it's still invalid, Quorum shows the valid parts with a warning.
3. Quorum fixes up the result:
   - Fills in owners and due dates ("Unassigned" / "TBD").
   - Converts relative dates.
   - Puts a timestamp on every item.
   - Checks every decision and action against the transcript. Anything that can't be traced to something actually said is removed, with a warning.
4. Quorum renders the result with the template for the meeting type.

Long meetings use the same splitting as summaries: notes are extracted from each part, merged, and duplicate decisions and action items are removed.

**Editing a template:** open **Settings → Skills → Professional Meeting Notes**.
- **Edit:** your copy is saved in `data/skills/meeting-notes/templates/`, so the repo's file stays the default.
- **Reset to default:** deletes your copy.
- **Template syntax:** a small subset of Mustache:
  - values: `{{meeting.title}}`
  - lists: `{{#decisions}}…{{/decisions}}`
  - empty states: `{{^decisions}}…{{/decisions}}`
  - shared sections: `{{> actions}}`

**Adding a meeting type:** add `skills/meeting-notes/templates/<type>.md` with front matter, then add the type to `$defs.meeting_type` in `schema.json`. Any new `details` fields go under `details` in the schema. The front matter looks like this:
```markdown
---
type: design_review
label: Design review
keywords: design review, mockup, figma, feedback on the design
fields: design_feedback
guidance: Fill details.design_feedback with each piece of feedback and who gave it.
---
{{> header}}
{{> tldr}}
…
```
Detection uses the keywords: they're counted in the transcript, and words in the title count three times.

## Models for meetings

Speech-to-text (**Whisper large-v3-turbo**) and speaker separation (**pyannote 3.1**) are fixed and always run on this Mac. Three steps use a model you choose:

| Step | Default | Choices |
|---|---|---|
| Speaker identification (reads the transcript) | Gemma 4 E4B | Local models only; each is marked if it can also read video |
| Names from the video (unclear frames) | Qwen3-VL 4B | Local models that can read images (Ollama's `vision` capability); a text-only model is never sent frames |
| Summary | Same as the chat model | Any local or cloud model; a cloud model asks before a transcript is sent |

- **Settings → Meetings → Models** sets the defaults (`speaker_default` and `vision_default` in `models.json`).
- **New meeting** shows the fixed steps and the three choices for this meeting before Start, with an estimate for each step. The choices are kept with the job and the transcript.
- **Identify speakers and write the summary when transcription finishes** (optional) runs in the API, so it works with the page closed and resumes after a restart. It waits until the transcriber has freed its memory. A cloud summary model still waits for your OK.
- **The Speakers and Summary panels** show which model was used, and you can pick another and re-run; the meeting keeps it.
- **A model that isn't installed** can be downloaded from the chooser, with progress. Until then the recommended installed model is used, and the app says so.
- **Memory (16 GB):** Whisper, pyannote and Ollama models never share memory. The transcriber frees Ollama's models and waits until they're unloaded (shown as "Waiting for the local model to free its memory"). Local steps in the app wait while a transcription runs.

## Record a call

**Meetings → Record a call** records the call on this Mac with **Quorum Recorder** (a small helper app using ScreenCaptureKit, built by `scripts/setup_local_ai.sh` or on first use). macOS asks once to allow Quorum Recorder's **Microphone**. For the screen, Quorum uses macOS's own picker: when you press Start, click the meeting window (or choose the screen), and that choice is the permission. This works on managed Macs and standard accounts, where switching on **Privacy & Security → Screen & System Audio Recording** for a self-built app may have no effect. If that switch does work for Quorum Recorder, the app list is shown instead and no picker appears. Nothing is recorded or sent anywhere until you choose.

**What to record:**
- **Meeting app:** Teams, Zoom, Meet in a browser… or a remote desktop app (Windows App, Citrix, Horizon, Screen Sharing) when the call runs inside a remote desktop. It records the app's windows, including the new window Teams/Zoom open when someone shares their screen, and only that app's sound.
- **Entire screen:** a display of your choice, with every app's sound.
- **Audio only.**

**Why it's more accurate:**
- **Separate tracks:** your microphone and the call's sound are recorded as separate tracks.
- **You're confirmed:** your track becomes a speaker named after you (from Memory), shown as *Confirmed: your microphone*. Nothing automatic changes it, though you can.
- **Fewer voices to separate:** pyannote only separates the other people, from the call's clean digital sound.
- **Overlapping speech** keeps both sides.

**Headphones:** wired, Bluetooth or the laptop speakers all work. The call is taken from the app, not from the speakers, and plugging in or connecting a headset mid-call is fine: the microphone follows the Mac's input and the recording never stops. Headphones are best, because then your microphone hears only you. With speakers, the call leaking into your microphone is filtered out.

**While recording:**
- **On every page:** a red **● 12:03** in the top bar; the Meetings page shows sound meters for you and the call.
- **Warnings:** you're warned when the call has been silent for a minute while you talk, and when the microphone switched.
- **Protection against losing audio:**
  - The file is written in 10-second fragments, so a crash keeps all but the last few seconds.
  - The Mac doesn't sleep while recording.
  - It stops cleanly if the disk gets low or the meeting app quits.
  - Other transcriptions and local-model work wait until the call is over.
- **Stop and transcribe:** makes the final transcript with your New meeting choices, and optionally identifies speakers and writes the summary afterwards.

**Transcribe during the meeting** (a switch, off by default) adds a **live transcript**: about every 30 s, Whisper transcribes the new part of each track.
- **Labels:** "You" for your microphone; for the call, the name read from the highlighted tile at that moment, otherwise "Others". There's no pyannote and no vision model during the call.
- **From the panel:** **Copy** the transcript so far, **Ask in chat** (the chat's attachment refreshes itself before each message and becomes the final transcript once it exists), and **Actions so far**.
- **Memory:** it uses about 2 GB more during the call and pauses while macOS reports memory pressure, then catches up; the recording itself never pauses.
- **After Stop:** the final transcript replaces the draft. It's made from the full recording and is as accurate as without the live draft.

⇧⌘5 screen recordings still work as before: they reach Quorum only when they're finished, and their audio tracks are mixed.

## Correct with another transcript

On a meeting's page, **Add another transcript** takes any of these, alone or together, now or later (earlier meetings too):
- **The Voice Memos recording:** drag the memo from Voice Memos onto the dialog, or use Share → Save to Files there and choose it. Quorum transcribes it with the same Whisper model (a job, like a new meeting) and then corrects the meeting.
- **Text:** paste the Voice Memos transcript, a claude.ai transcript, or any other (Teams/Zoom `.vtt`/`.srt`, `Name: text` lines, `.docx`, `.pdf`).

None is needed: without them the meeting stays as it is.

**How it decides** (pure code on this Mac, no model):
- Each source is lined up with Quorum's transcript word by word. A recording also gets its time offset ("started 12 s before the recording"). A source that matches too little is marked "doesn't match this meeting" and changes nothing; one covering part of the meeting corrects only that part.
- Where they disagree, the majority wins. The Voice Memos recording and its text come from the same microphone, so they count as one vote; claude.ai is another. With one source, its word is used where the surrounding words agree, unless Quorum's Whisper was sure of its own. A recording is transcribed by Whisper too, so its word wins only where Whisper was clearly surer of it (a noisy memo doesn't undo the call's better hearing). Names you have in Memory beat unknown spellings.
- Words are removed only when two sources agree, or when they're a known Whisper invention in silence ("Thank you.").
- A source with speaker names (a Teams transcript, `Name: text`) names Quorum's speakers and moves turn boundaries that are off by a few words. Your microphone's speaker and labels you set are never changed.

**After a correction:** a message gives the counts ("142 words corrected · 6 turns moved · 2 names suggested") with **Undo**. Corrected words are underlined in the transcript; hover to see what they were and which source fixed them. The **Corrections** tab lists the sources (how much matched, the offset; remove any) and every change (revert or bring back each), with **Undo all** and **Apply again**. Search uses the corrected words, and notes written before say so and offer **Regenerate**.

Measured on a synthetic meeting: a poor call recording (13% wrong words) went to 8.4% with a clear memo; a clear call with a noisy memo got no changes.

## Meeting check

The meeting check compares a meeting with **earlier meetings** and **memory**, and alerts you to:
- **Conflict:** something said now contradicts an earlier decision, date or commitment.
- **To raise with everyone:** a risk, blocker, dependency or change all stakeholders should hear about.
- **Still pending:** an earlier action or open question that isn't done and wasn't said to be done.
- **My action:** something to raise or track in your role. The role comes from your person entry in Memory, for example Release Manager.

**Where it looks:** earlier meetings' saved notes (decisions, actions with owners and due dates, open questions), plus matching passages of their transcripts. It uses meetings of the active project or with the same people, from the last 90 days; both are adjustable in Settings → Meetings. It also uses memory and your tracked actions.

**Every alert shows its source:** the line of this meeting (click to jump to it) and the earlier meeting's item or memory fact. An alert without a source is dropped, and alerts already shown or dismissed don't come back.

**Running it:**
- **During a call:** **Check now** in the live transcript's Alerts tab, or automatically every 5–30 minutes. The recording indicator shows how many alerts are open.
- **Afterwards:** the meeting's **Check** tab. It also runs after the summary when "identify and summarise" is on, with a local model.
- **On each alert:** **Dismiss**, **Track** (adds it to *My actions* on the Meetings page, with done/not done), or **Ask in chat**.

**Models and privacy:** the check model defaults to a local model. A cloud model asks first, the same way summaries do, and a project set to Local always stays local. With a local model during a call, the live transcript pauses while the check runs, because Whisper and the model can't share 16 GB.

**Pop-up alerts** (Settings → Meetings, on by default) show a macOS notification with only the number and kinds of alerts ("2 alerts: 1 conflict, 1 still pending"), never their content. Switch them off if you share your screen in meetings: alerts then show only in the app. macOS also hides notifications while you share your screen, unless you allowed them in System Settings → Notifications.

## Speakers: names on screen, voices and cloud review

**Names on screen (video recordings only).** After Whisper and pyannote finish (never at the same time, because of 16 GB RAM), the transcriber:
1. samples one or two frames at up to 10 turns per speaker;
2. finds the highlighted tile (a coloured border, or a white one in newer Teams versions);
3. reads the name label under it with Apple's Vision framework, on this Mac.

It handles screen sharing (frames are skipped), gallery and speaker view, several highlighted tiles (left unresolved), audio-only files (the step is skipped) and names cut short with "…" (matched to the full name). Frames it can't settle can go to a **local** vision model, which is shown the labels on screen and asked only which one is on the highlighted tile, never anything about faces. The vision model is chosen separately from the chat model in Settings → Meetings, and only models that can read images are offered; the default is **Qwen3-VL 4B** (`ollama pull qwen3-vl:4b-instruct`, 3.3 GB). At most *N* frames per meeting (about 9 s each). Frames never go to a cloud model, and frame crops are kept only when the debug setting is on. Older transcripts: **Read names from video** in a meeting's Speakers panel (`POST /api/meetings/transcripts/{id}/screen-names`) when the video still exists.

**Evidence.** An on-screen name seen on 3+ of a speaker's turns counts as strong (≈0.85); seen twice, medium; once, or only read by the vision model, weaker. The Speakers panel's "why" line says e.g. *name on the active tile at 12:04 (seen 9×)*.

**Voice fingerprints** (Settings → Meetings, on by default) are biometric data: stored only in the memory file on this Mac, never exported or sent anywhere. One is saved when you confirm a name, or automatically when a name was read as text on 3+ of the speaker's turns (≥70% of them) and no other speaker shares it. **Make fingerprints for older meetings** computes them from the recordings with pyannote's embedding model only (no new transcript). Forget one person's voice in Memory, or all voices in Settings → Meetings.

**Cloud review.** Names are always identified on this Mac first. If the selected chat model is a cloud model (GAIA, Claude, ChatGPT, …) and the review is on:
- **What it gets:** the transcript lines, the local suggestions with their evidence, names seen on screen, and people from memory who may be in this meeting (name, aliases, role, team, projects only).
- **What it never gets:** voice fingerprints, video frames, or memory unrelated to the meeting.
- **What it may change:** names and roles you didn't set yourself. Every change must cite transcript lines (or match the name on that speaker's tile); anything else is ignored.
- **Routing:** in Auto mode you're asked first ("Send speaker names and the transcript to Claude Opus 5.5 for review?"); in Cloud mode it runs with a one-time notice per provider; Local mode, confidential projects (default mode Local) and meetings attached to confidential chats skip it.
- **Changes:** the Speakers panel lists each one (local name → cloud name, reason, links to the cited lines) with **Keep**, **Revert** and **Revert all**. A changed name's voice is remembered only after you keep it. If the review fails (offline, bad key, rate limit, invalid JSON after one retry), the local names stay and a notice says why.

## Memory
Settings → Memory shows one store (`data/memory/memory.json`) in the shape of a claude.ai memory export:
- **Profile** (you), **People**, **Projects** (with their own areas) and **Areas** (topics).
- Each entry has a name, description, aliases and facts grouped under headings.
- Every fact shows its source and date: *Imported from Claude*, *Learned from chat*, *Learned from a meeting*, *Entered by you*, *Suggested by cloud review, kept by you*.
- People also show their role, team, meetings, voice fingerprints (count, with **Forget voice**) and names seen on screen.
- You can search, edit, merge duplicates (entries and facts), delete, and **Export ZIP** in the claude.ai folder layout (`profile.md`, `people/`, `projects/<name>/index.md`, `projects/<name>/areas/`, `areas/`). Re-importing that ZIP adds nothing twice.

The first start after updating moves the older stores (`people.json`, imported claude.ai memory, each project's chat memory) into this file. It copies them to `data/memory/backup-<time>/` first and leaves the originals untouched; your edits are kept.

**Where memory is used:**
- **Chat:** the facts relevant to your message (about you, the people and projects you mention, and facts that share words with it) go into the prompt, and the answer lists them under *Used N facts from memory*.
- **Speaker identification:** roles, teams, aliases and projects are used as evidence.
- **Meeting notes:** full names and roles for attendees and action owners ("Owen" → "Owen Hart"), the project a meeting is about, and "I"/"me" as you. Memory never adds attendees, decisions or actions that aren't in the transcript.

## Cloud models: Claude, ChatGPT and others
Settings → Models adds cloud providers next to the built-in GAIA (GLM-5.3 Flash, unchanged):
- **Anthropic (Claude):** official `anthropic` SDK, Messages API with streaming. Claude Opus 5.5, Sonnet 5.5 and Haiku 5.5 are shown by default. For models that support it, server-side refusal fallback (`fallbacks: "default"`) is on: if Claude declines a request on policy grounds, the API retries it on another Claude model.
- **OpenAI (ChatGPT models):** Chat Completions API; you pick models after listing them.
- **OpenAI-compatible endpoint:** a base URL + key + model (Azure OpenAI with the `api-key` header, OpenRouter, Groq, …). `https://` is required except for localhost.

API keys are stored in the macOS Keychain (`secrets_store`; `SECRETS_BACKEND=file` keeps them in a 600-permission file instead), never in `preferences.json`, `cloud_providers.json`, logs or exports. **Test connection** checks the key and lists models; tick the ones the picker shows, or add one by name. Errors (missing/invalid key, rate limit, offline, unknown model, overloaded) are shown as such and never fall back to another provider. Only the conversation text (role and content) is sent; the app's tool calling stays plain-text JSON, so skills, search, connectors and memory work the same with every model.

**Privacy routing:** Auto/Local/Cloud works with every provider. Approving cloud use for sensitive content is per provider: approving GAIA in a chat doesn't approve Claude. The one-time "memory may be sent" notice is per provider too, and a chat moved to a local model as confidential asks again for any cloud model. A project can set its default cloud model the same way as its default local model.

## Connectors
Settings → Connectors → **Find connectors** searches the official MCP Registry (`registry.modelcontextprotocol.io`, API v0). Only the words you type are sent; nothing else from Quorum. Results show the publisher, whether the registry lists it as active, the version, a source link, the transport and what it needs, and they're marked when already added.
- **Remote servers** (Streamable HTTP or SSE): the details list the tools when the server allows it without signing in. Sign in with an API key/header or OAuth (dynamic client registration and PKCE; the browser returns to `http://127.0.0.1:8000/api/connectors/oauth/callback`, set `API_PUBLIC_URL` if the API runs elsewhere). After adding, Quorum tests the connection and shows the tools.
- **Local packages** (npm via `npx`, PyPI via `uvx`, run over stdio): Quorum shows the exact command with a pinned version (never "latest") and the publisher, and installs only after you tick *I trust … and want to install version …*. Secret environment variables go to the Keychain. A package starts on first use and stops after 15 idle minutes, when disabled or removed, and when the API stops. Its log is in `data/connectors/logs/`.
- **Managing connectors:** each one shows its source, with **Enabled**, **Re-test**, **Check for update** (confirm the version change) and **Remove** (which also deletes its stored keys and sign-in).
- **Privacy:** remote connectors follow the cloud rules. They're skipped with a local model and in confidential chats, and in Auto mode a sensitive message names them in the confirm prompt. Local packages count as local but may access the network. The agent lists every enabled connector with its tools and calls one as `{"connector": …, "tool": …, "input": {…}}`.

## Tests
```bash
.venv/bin/python -m pytest                                  # Python: API, providers, chunking, speakers, …
transcriber/.venv/bin/python -m pytest transcriber/tests    # transcriber service
cd web && npx playwright test                               # end-to-end UI (starts its own servers)
```
The Playwright suite runs the real web app and API against fake services, with a throwaway data folder:
- **Fake services** (in `web/e2e/mock_services.py`): Ollama (including embeddings), GAIA, the transcriber, the Tavily and Exa web search APIs, the Claude and OpenAI APIs (`tests/fake_cloud.py`), and an MCP Registry with remote MCP servers (API key, OAuth, SSE) plus a stdio server standing in for packages (`tests/fake_mcp.py`, `tests/fake_mcp_stdio.py`). Nothing calls the real Anthropic, OpenAI or MCP Registry APIs, and no package is installed.
- **Data:** all synthetic: fixture meetings in `tests/fixtures/meetings/` and claude.ai exports in `web/e2e/fixtures/`. No real recordings or exports are used.

It covers:
- models, status badges, downloads and switching models
- chats
- uploads and transcription, including cancel
- notes for every meeting type, including chunked meetings, exports, edits and the cloud model
- speaker labels from memory, past conversations and names on screen; the cloud review with Keep / Revert / Revert all
- the Memory tab, cloud providers (adding, testing keys, choosing models, chatting with Claude and OpenAI models), and finding, adding, using, disabling and removing connectors
- claude.ai import and imported chats
- search, including web fallbacks and the assistant's search tool
- every Settings tab
- theme, shortcuts and responsive layout
- error cases

To regenerate the fixtures: `python tests/fixtures/make_meetings.py` and `python web/e2e/fixtures/make_claude_export.py`. Synthetic meeting videos (drawn name tiles, a highlight that follows the speaker) come from `transcriber/tests/synthetic_meeting.py`.

**Benchmark of the whole local pass** (`.venv/bin/python scripts/benchmark_screen_names.py --minutes 10 --vision-model qwen3-vl:4b-instruct --vision-cap 6 --speaker-models gemma4:e4b qwen3-vl:4b-instruct`), M4 MacBook Air.

On a synthetic 10-minute call (49 turns, 4 speakers):

| | Text reader only | + Qwen3-VL 4B for unclear frames (6) |
|---|---|---|
| On-screen name stage | 6.3 s, 70 frames | 37 s (includes loading the model) |
| Frames named | 58 (6 screen share, 6 without a highlight) | 58; the 6 unclear frames correctly have no highlight |
| Local pass, speakers named correctly | 4 of 4 (no model; with Gemma 4 E4B 6 s, with Qwen3-VL 4B 14 s) | same |
| Cloud review (fake model told to change one) | 1 change, 3 agreed, 0 rejected | same |

Reading the transcript, on 6 synthetic meetings where names can be inferred from what is said (introductions, hand-overs, thanks; 17 speakers, 2 never named; `tests/fixtures/speaker_meetings.py`):

| Model reading the transcript | Correct | Wrong name | Missed | Seconds per meeting |
|---|---|---|---|---|
| None (name clues only) | 13 | 0 | 4 | 0 |
| **Gemma 4 E4B** (default) | **14** | **0** | 3 | 4.8 |
| Qwen3-VL 4B | 10 | 3 | 4 | 10.3 |
| Gemma 4 12B QAT (`gemma4:12b-it-qat`, 7.2 GB) | 12 | 1 | 4 | 20.3 |

Qwen3-VL 4B mixed up the person addressed with the person speaking ("Hey Owen…" given to Owen), so it reads video frames and Gemma reads the transcript. Gemma 4 12B QAT (tried 2026-11-21) was slower and less accurate than E4B, so E4B stays the default; the 12B is fine as a chat model. The time estimates use these speeds (`speeds` in `models.json`) until this Mac has measured its own.

**Choosing the vision model** (`transcriber/.venv/bin/python scripts/benchmark_vision_models.py [--hard] --models …`, synthetic frames, M4 MacBook Air). The first set has 30 plain frames (2–6 tiles; Teams, thin, white, Zoom and Meet borders; truncated names; screen shares; nothing highlighted). The `--hard` set has 16 frames with busy "camera" content, 1–2 px borders and decoy patches in the highlight colour. The row that matters most is the frames the text reader can't settle, because only those reach the vision model.

| | Gemma 4 E4B | **Qwen3-VL 4B** (default) | Qwen3-VL 8B |
|---|---|---|---|
| Plain set: correct | 26 of 30 | 29 of 30 | 29 of 30 |
| Hard set: correct | 8 of 16 | 12 of 16 | 11 of 16 |
| Frames the text reader can't settle (hard set), named | 0 of 8 | 4 of 8 | 3 of 8 |
| Wrong names (both sets) | 0 | 0 | 0 |
| Seconds per frame | 3.8–4.5 | 8.4–9.6 | 14–18 |
| Download | 9.5 GB | 3.3 GB | 6.2 GB |

Qwen3-VL 8B was retested on 2026-11-21 with the same result (plain 29 of 30, hard 11 of 16; 64 s for the name stage on the 10-minute call against 43 s for the 4B), so it was dropped from the suggested models; any installed model that can read images can still be picked. Qwen3 8B (text only) can't read images, so it can't be used here. The instruct versions are used because the "thinking" versions reason before every answer, which is too slow per frame.

## Notes
- **Gemma 4 E4B and audio:** Ollama accepts audio for Gemma 4 E4B, but only through the `images` field of `/api/chat`. On real meeting audio it agreed with Whisper on 71–80% of words. It gives no word timestamps, so speaker labels can't be lined up, and it can't stay in memory alongside Whisper on 16 GB. Whisper large-v3-turbo therefore remains the speech-to-text engine. Gemma is used for chat, summaries and speaker identification.
- **Classic UI removed:** the original Streamlit UI (`app.py`, Docker on :8503) has been retired; the web app replaces it and keeps the same data. Git history has the version from before the local-model work (commit `778a6f3`).
