# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Quorum**, a meeting transcription and summary agent: a Next.js 16 App Router + TypeScript + Tailwind 4 web app in `web/`, talking to a FastAPI layer `api/main.py` that wraps the Python modules below. Everything runs natively on the Mac (no Docker). The original Streamlit UI (`app.py`) has been removed; its tests were ported to `tests/test_agent.py`.

Chat models:
- **Cloud:** GLM-5.3-Flash through the Globant GAIA API (OpenAI-compatible `/v1/chat/completions`, SSE streaming). **The cloud model and its requests must stay unchanged**; `tests/test_cloud_regression.py` (llm_backends + agent, captured from the original app) and `tests/test_providers.py` pin the exact GAIA requests.
- **Local:** any model installed in Ollama, running natively on the Mac and fully offline. It's used for confidential content. The registry is `models.json`; the default chat model is the cloud one (`cloud:gaia`), and every New chat starts on it (`app-provider` listens for `quorum:new-chat`).

There's also offline **meeting transcription**: Whisper large-v3-turbo through MLX, plus pyannote 3.1 speaker labels. It runs in a native service, `transcriber/`, and has no cloud path. The transcription models are fixed and aren't user-selectable. Gemma 4 E4B accepts audio via Ollama's `images` field, but agreed with Whisper on only 71–80% of words, has no timestamps, and can't share 16 GB with Whisper, so it is not offered for transcription.

`chat.py` is a standalone terminal chat client kept from the first version. It uses different env vars (`CODA_GEAI_BASE_URL`, `CODA_GEAI_API_KEY`, `GLM_MODEL`) than the web app.

## Commands

```bash
# One-time local AI setup (Homebrew ollama/ffmpeg, model pulls, transcriber venv, model downloads)
scripts/setup_local_ai.sh
# Start / stop Ollama + transcriber natively (Docker on macOS has no GPU access); `restart` reloads the transcriber
scripts/start_local_ai.sh
scripts/start_local_ai.sh stop
scripts/start_local_ai.sh restart
# On-screen names benchmark on a synthetic 10-minute call (text reader; add --vision-model gemma4:e4b)
.venv/bin/python scripts/benchmark_screen_names.py --minutes 10
# Compare local vision models on synthetic call frames (needs the models installed in Ollama)
transcriber/.venv/bin/python scripts/benchmark_vision_models.py --models qwen3-vl:4b-instruct gemma4:e4b

# Web app: API (127.0.0.1:8000, .venv) + Next.js (http://localhost:3000); `dev` = hot reload, `stop`
scripts/start_app.sh [dev|stop]
# Local chat models (Ollama >= 0.31 check, gemma4:e4b pull, test prompt); Windows: scripts/setup-local-models.ps1
scripts/setup-local-models.sh [models…]

# Web app tests: real web + API against fake Ollama/GAIA/transcriber (web/e2e/mock_services.py)
cd web && npx playwright test            # starts mock :18999 (+ ollama :18434), API :18000, next dev :13000
cd web && npx tsc --noEmit && npx eslint src

# Python deps (.venv)
pip install -r requirements.txt -r api/requirements.txt -r requirements-dev.txt

# Tests (pytest.ini sets testpaths=tests and -q; add -o addopts="" to see the summary line)
pytest
pytest tests/test_routing.py::test_mode_matrix
# Transcriber service tests need its own venv (torch/mlx/fastapi):
transcriber/.venv/bin/python -m pytest transcriber/tests -q
```

No linter/formatter is configured.

Env vars:
- **Cloud:** `GAIA_*`.
- **Storage:** `APP_DATA_DIR` (default `./data`); `PROJECTS_DIR`, `SESSIONS_DIR`, `CONNECTORS_FILE` default to folders inside it.
- **Local models:** `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_MODEL` (default model), `LOCAL_BACKGROUND_MODEL`, `OLLAMA_CONTEXT_LENGTH`, and `LOCAL_PREFILL_TPS` / `LOCAL_GEN_TPS` (speeds used for time estimates).
- **Transcriber:** `TRANSCRIBER_URL` (server side) and `TRANSCRIBER_PUBLIC_URL` (the browser's URL for the upload iframe), plus `MEETINGS_DIR_HOST` / `TRANSCRIPTS_DIR_HOST`, which the start script reads.

## Architecture

### Web app (web/ + api/)
- **`api/main.py`** (FastAPI) is the only backend the browser talks to (CORS `WEB_ORIGINS`; the browser calls `NEXT_PUBLIC_API_URL` directly, no Next rewrites).
  - Long operations stream SSE through `sse(run)`: chat turns, `/summary`, `/identify`, `/split-speakers`. `run(emit)` runs in a thread, and a client disconnect cancels at the next `emit`. Events: `start`, `delta`, `reset` (drop a streamed tool call), `status`, `notice`, `progress`, `memory`, `confirm` (`sensitive` / `long`; the client re-POSTs with `confirm: "local" | "cloud" | "run"`), `error` (`kind`), `done`.
  - It proxies the transcriber (uploads are chunked through `/api/meetings/uploads/...`).
  - Storage under `APP_DATA_DIR`:
    - `preferences.json` (selected model, active project, `confirm_cloud_sensitive`, `web_search`, `cloud_notices`
      per provider, `voiceprints`, `speaker_cloud_review`, `screen_names`, `vision_fallback`, `vision_model`, `vision_cap`, `screen_debug`,
      `speaker_model`, `summary_model`, `record_live`, and the meeting check's `check_model`, `check_popups`,
      `check_auto_minutes`, `check_days`, `check_all_projects`)
    - `memory/memory.json` (memory_store; `memory/backup-*/` from the one-time migration)
    - `cloud_providers.json` (no keys) and `connectors/` (connectors.json, stdio logs)
    - `speaker_labels/{tid}.review.json` (the cloud review: changes with pending/kept/reverted/edited)
    - `speaker_labels/{tid}.participants.json` (the participants-list check: changes pending/kept/reverted/edited,
      flagged names, suggestions) and `attendees/{tid}.json` (your attendee edits: added, removed, renamed)
    - `speaker_labels/{tid}.json`
    - `meeting_models/{tid}.json` (models picked later in the Speakers/Summary panels, `speaker_used`) and
      `meeting_models/jobs/{job_id}.json` (the "identify and summarise when transcription finishes" follow-up)
    - `summaries/{tid}.json`
    - `model_speeds.json` (persisted `MEASURED_SPEEDS`)
    - `meeting_checks/{rec-<recording id>|t-<transcript id>}.json` (meeting-check alerts with state new/dismissed/tracked)
      and `my_actions.json` (alerts tracked as "My actions", per project)
- **`agent.py`**: the turn logic (tool loop, long-context handling, local-path guards, memory extraction) on an explicit `TurnState`; see "Agent turn" below. A meeting transcript reaches the model only when attached to that chat: `project_documents()` leaves out project documents of kind `transcript` (the old Streamlit app saved some), and they don't count for sensitivity or the minutes skill.
- **`cloud_providers.py`**: providers added in Settings → Models. Anthropic through the official `anthropic` SDK
  (beta Messages stream, `fallbacks: "default"` for models in `FALLBACK_MODELS`, no temperature, `max_tokens`
  required), OpenAI and OpenAI-compatible endpoints over HTTP. Model ids `cloud:<provider>:<model>`; keys in
  secrets_store (`provider_<id>_api_key`). `CloudProviderError.kind` (missing_key, invalid_key, rate_limited, offline,
  unknown_model, overloaded); the SSE error event is `kind: "cloud"` + `error_kind`. `ANTHROPIC_PROVIDER_URL` /
  `OPENAI_PROVIDER_URL` point tests at fakes. Approvals and the memory notice are per provider (`provider_key`;
  GAIA keeps `cloud_approved_reasons`, others `cloud_approved[pid]`).
- **`model_registry.py` + `models.json`**: the `ModelProvider` interface.
  - `GaiaProvider` delegates to `llm_backends`' cloud functions (unchanged requests; `max_tokens`/`temperature` are ignored).
  - `OllamaProvider` wraps the local functions.
  - `list_models` adds status badges (`ready` / `not_downloaded` / `ollama_down`), `ollama_too_old`, and any installed non-embedding Ollama model. `pick_local_model` picks a ready local model.
  - `PullManager` runs `ollama pull` in a thread with progress and cancel.
  - Meeting models: `speaker_default` (reads the transcript, Gemma 4 E4B) and `vision_default` (video frames,
    Qwen3-VL 4B) in models.json; `speaker_models()` / `pick_speaker_model(pref)` (local only), `speeds_for(model)`
    (measured, else models.json `speeds`), `seconds_per_frame`, `unload_local`. Chosen per meeting in New meeting
    (the job's `models` go into the transcript), changed in the panels; `api.meeting_models(tid)` merges Settings →
    transcript → panel choice (`DELETE /api/meetings/transcripts/{tid}/models` forgets the panel choice). Summaries never
    follow the chat model: with no summary model chosen, `/summary` emits `choose_model` and the client asks (the
    follow-up stops at `needs_model`). `FollowUps` runs identify (no cloud review) then the summary after a job, server side,
    resumed in the FastAPI lifespan; a cloud summary model stops at `needs_ok`. Local steps call
    `wait_for_transcriber` (no Ollama work while a transcription runs). Estimates: `speaker_id.estimate_seconds`,
    `long_context.summary_seconds`.
  - Vision models (on-screen names fallback) are separate: `models.json` `vision_models` + `vision_default`; `vision_models()` adds installed models whose Ollama capabilities include `vision`; `pick_vision_model(pref)` never returns a text-only model (the selected chat model isn't used). Tests stub `capabilities` in conftest. Compare models with `scripts/benchmark_vision_models.py`.
- **Recording a call** (`/api/meetings/record/*`, proxied to the transcriber): sources, start (`live` is remembered
  as `record_live`), status, stop (same models/after follow-up as `POST /api/meetings/jobs`), live draft and its
  Markdown. Recorder errors keep `kind` (`meetings.RecordError` → `HTTPException(detail={kind, message})`, the
  browser's `ApiError.code`). A live draft attached to a chat (`POST /api/sessions/{id}/attachments/live`, kind
  `transcript`, `live` = recording id) is refreshed before each turn (`refresh_live_attachments`) and swapped for the
  final transcript once one has that `recording_id`.
- **`meeting_check.py`** + `check_job` (`POST /api/meetings/check`, SSE): alerts of type conflict / raise / pending /
  my_action. Context is numbered (earlier meetings' saved notes E…, BM25 passages of their transcripts P…, memory
  facts M…, tracked actions T…); `validate` drops alerts without the required line/source (`NEEDS_LINE`,
  `NEEDS_SOURCE`), `is_repeat` skips alerts already shown or dismissed. Earlier meetings: `check_days`, same project
  (every meeting in the default project or with `check_all_projects`) or a shared attendee, newest 8. Check model:
  `check_model` or the local default; a cloud model emits `confirm` (`sensitive`), a project with
  `default_model_mode: local` stays local, `unattended` (FollowUps step "check" after the summary) never asks. A
  local check during a call pauses the live draft (`meetings.record_live_pause/resume`). Alerts: `GET/DELETE
  /api/meetings/check/alerts`, `POST …/{key}/{id}/{dismiss|track|restore}`; `/api/my-actions`. Each alert carries the
  `key` of its file (a transcript also shows its recording's alerts). The role for my_action comes from
  `memory_store.user_person()["role"]`.
- **Corrections with other transcripts** (`transcriber/correct.py`, pure code): references per transcript in
  `TRANSCRIPTS_DIR/.refs/<tid>/<rid>.json` (text: `text_import.parse` segments; audio: Whisper words from job kind
  `reference`, `pipeline.run_reference`, the upload renamed to hidden `.ref-<tid>-<rid>.<ext>`). `apply_corrections`
  always recomputes from `before_correction.segments` with every ready reference and `correction.reverted`; `undone`
  keeps the original until `/corrections/apply`. Votes: Quorum + one per `recording` group (Voice Memos text + audio =
  one group; abstains when they disagree); ties keep Quorum except known terms (API `known_terms`: memory names,
  aliases, projects, on-screen names), Quorum `p` < 0.5, a single well-aligned text reference (unless `p` ≥ 0.9), or a
  single recording whose word is ≥ 0.8 sure and 0.15 surer than Quorum's. New transcripts keep Whisper's words
  (`TRANSCRIPTS_DIR/.words/<tid>.json`, `{word,start,end,p}`); older ones get times spread over each segment.
  `correction = {refs, changes (reverted ones listed with reverted: true), marks {seg: [[start, end, id]]}, names,
  stats, reverted, kept, undone, layers}`. `apply_corrections` rebuilds: engine (ready refs) → model layers in
  `LAYER_ORDER` (`review`, `cloud_review`; `POST/DELETE …/corrections/model` with `source`; works without refs) →
  your edits (`transcriber/edits.py`: ops text/speaker/split/merge anchored by start time with an index hint,
  `make_op` validates, applied last, stale when the line is gone; `POST/DELETE /transcripts/{id}/edits[/{op}]`;
  marks and change `seg`s remapped through the edits). A PUT of new segments drops the correction. API (`after_correction`): clears the search
  cache entry, writes `source: "reference"` labels (never over user/microphone; `speaker_id.identify` keeps them),
  marks the summary `stale`. Web: `components/meetings/corrections.tsx` (dialog, Corrections tab, `MarkedText`).
  Beyond words: a single reference inserts up to `MAX_SENTENCE_INSERT` and deletes up to `MAX_SINGLE_DELETE` inside
  well-aligned sentences; sentence ends (`punct`; references keeping Quorum's punctuation vote to keep it); whole
  lines go to another speaker (`speaker`: `SPEAKER_SHARE` of named words, never fixed speakers); `sentences`
  summarises the changes per line ("By sentence" view). Model review: `correction_review.py` (API
  `POST/DELETE /api/meetings/transcripts/{tid}/correction-review`, SSE; a cloud model asks once per meeting and
  provider via `meeting_models/{tid}.json` `cloud_ok`; local projects refuse) over `correct.line_context` (transcriber
  `GET …/corrections/context`, reference words mapped to lines word by word). Validated fixes (words only from the
  line or its reference text, ≥ `KEEP_SHARE` of the line kept, real non-microphone speakers) are saved with
  `POST …/corrections/model` and applied by `correct.apply_model_changes` only while the line still reads as
  `before` (else `stale`).
- **Participants list, attendees, your edits:**
  - `participants_check.py` (pure): the last step of `/identify` (after the local pass, again after a done cloud
    review): `match` (exact, accents, first name, cut short, sounds alike via `sound_key`; one candidate only),
    `check` (list spelling with `previous`, source `participants`; `flag: not_in_list` ×0.7; `suggestions` for
    unnamed speakers; names already listed exactly are claimed first, never two speakers per name; user/microphone
    untouched). Reverted changes stay reverted on re-identify (`run_participants_check`). Keep/Revert:
    `POST …/speakers/participants/{keep|revert|revert-all}`.
  - `attendees.py` (pure): `build` = transcript `participants` rows + named speakers (linked by exact/accents/first
    name/cut short) + your edits; `follow_speaker_rename` (PUT …/speakers renames the linked listed row);
    `POST …/attendees {add|remove|rename}` (renaming someone who spoke renames the speaker; removing them is 409).
    `get_transcript` adds `attendees`, `participants_check` and `edits` (descriptions). Notes (`meeting_meta`) add
    attendees who didn't speak; the meeting check adds attendee names.
  - Cloud review spelling (`speaker_review.spelling`, `check_fix`): before the names, same consent; fixes saved as
    the transcriber's `cloud_review` correction layer (`run_cloud_spelling`), names then reviewed on the corrected
    text; record `spelling`. `DELETE …/correction-review?source=cloud_review` = Revert all; `…/corrections/{id}/keep`.
  - Your edits: `POST/DELETE …/edits[/{op}]` (a `name` without a speaker becomes a user label `SPEAKER_Uxx`;
    `after_edit` clears search, drops unused U labels, marks the summary `stale`/`edited_after`; `voice_question`
    when lines moved to a named person → `…/voiceprints/refresh` (backfill job) then `…/voiceprints/learn`).
    `DELETE …/voiceprints` forgets one meeting's fingerprints (transcript + people memory); deleting a meeting does too.
- **`sessions.py`**: chat JSON files (the original Streamlit app's format, plus `model_id`, `attachments`, `cloud_approved_reasons`, `custom_title`); `clean_title` tidies older titles.
- **`speaker_id.py`**: names and roles with confidence. `screen_evidence` turns the transcript's `screen_names` into
  evidence (≈0.85 when seen 3+ times, weaker once or vision-only; truncated labels map to a known full name);
  `screen_confirmed` decides automatic voice fingerprints (API `save_screen_voiceprints`). `voice_matches` compares pyannote fingerprints (`speaker_embeddings` in new transcripts) with people memory. `text_clues` works per sentence (a speaker who addresses a name isn't that person). The local model's JSON reading is capped at `LLM_CAP` unless backed by evidence. Its reply limit grows with the speaker count (`reply_tokens`: 150 per speaker, at least 1800); a cut-off reply can't be parsed. Signals combine as a noisy-OR, assigned one-to-one; `level` is high ≥ 0.75, medium ≥ 0.5. User labels (`source: "user"`) are never changed. A Quorum call recording's
  `confirmed_speakers` become `source: "microphone"` labels named after `memory_store.user_person()` (else "You"),
  never changed by the model, the cloud review (`FIXED_SOURCES`) or screen names; `save_microphone_voiceprint` saves
  that track's fingerprint as the user's. It always runs on a local model.
- **`memory_store.py`**: the one memory store, shaped like a claude.ai export: entries `profile | person | project | area`
  with name, description, aliases and facts `{text, heading, source, date, key, edited}` (sources `claude.ai`, `chat`,
  `meeting`, `user`, `cloud_review`). First load migrates people.json, imports/memory.json and project memory (backup
  first). `place()` maps imported sections to entries; `relevant()` picks facts for a chat turn (shown as
  `memory_used`); `facts_for_meeting()` for speaker id; `export_zip()` writes the claude.ai layout (never voice
  vectors). Project memory lives here (`project_facts`, `set_project_facts`, `learn_chat_facts`), not in project JSON.
- **`people.py`**: the person entries of memory_store (`load()`/`save()`; an explicit `path` still reads an old
  people.json). `learn(tid, speaker, name, role, vector, source=user|screen|cloud_review)` first forgets what an earlier
  correction of the same speaker taught; `forget_voice`, `note_screen_name`.
- **`speaker_review.py`**: the cloud review after the local pass (`/identify`: local pass → `labels` event → Auto asks
  with `confirm` stage `speaker_review`, client re-POSTs `confirm: "cloud", review_only: true`). Sends lines numbered like
  the transcript view (`#n`), suggestions, on-screen names and relevant people (name/aliases/role/team/projects);
  validates with a schema, one retry, rejects changes without cited lines or a matching on-screen name, never touches
  `source: "user"`. `apply` keeps the local label under `previous`; Keep/Revert via `/speakers/review/{keep|revert|revert-all}`.
- **`connectors.py` + `connector_directory.py`**: connectors with `transport` (http | sse | stdio), `auth`
  (none | header | oauth with PKCE + dynamic registration, tokens in secrets_store), `tools` from tools/list, `enabled`,
  `source`. Older entries without `transport` keep the original single-tool path. stdio packages: `stdio_command`
  pins versions (`npx -y pkg@x.y.z`, `uvx pkg==x.y.z`), `add_stdio` needs `confirmed`, `STDIO` manager (lazy start,
  15-min idle stop, atexit). `connector_directory.McpRegistry` searches `MCP_REGISTRY_URL` (official registry v0),
  sending only the query. agent.py lists active connectors with tools (`connectors_block`) and skips remote ones
  when local or `TurnState.remote_connectors` is False (confidential chats).
- **`summaries.py`**: free-form minutes, used only when the meeting-notes skill is turned off (`prefs.meeting_notes_skill`). Direct when the transcript fits; otherwise `long_context.map_reduce_notes` and a merge. The cloud uses a fixed `CLOUD_CONTEXT_TOKENS` budget instead of truncating.
- **`meeting_notes.py` + `skills/meeting-notes/`** (the default summary path): `SKILL.md` (rules), `schema.json`, and `templates/<type>.md` (front matter: label, detection keywords, guidance, `details` fields) plus `templates/partials/`.
  - `generate`: the model returns JSON, which is validated with jsonschema. One retry with the errors; after that, `coerce` keeps the best partial result. Long meetings are split into parts with `merge_notes`, which de-duplicates per owner.
  - Memory (`generate(memory=…)`, from `api.notes_memory`): full names/roles for attendees already in the meeting,
    owners "Owen" → "Owen Hart" and "me" → the user, the project if said twice; never adds anyone.
  - `postprocess`: fills Unassigned/TBD and needs-follow-up, resolves dates with `relative_dates`, and runs `trace` so every decision and action maps to a transcript line (untraceable items are dropped, with a warning). Header facts come from the app, not the model.
  - `render`: a Mustache subset, driven by detail level and section toggles.
  - `notes_export.py`: email HTML, DOCX, PDF (fpdf2), CSV and the follow-up email, all derived from the saved Markdown so inline edits carry through.
  - Users' template edits live in `APP_DATA_DIR/skills/meeting-notes/templates` (`OVERRIDE_DIR`).
- **`claude_import.py` + `imports.py`**: the claude.ai export parser and the import store.
  - The parser recognises files by content, and memory by keys containing "memor". `extract_people` finds people with role, team, projects and relationships.
  - The store does preview → commit with a `preview_id` (used once). Re-import matches by key and similar wording, by conversation uuid, and by `people.match_imported`.
  - Imported chats are read-only sessions in `APP_DATA_DIR/imports/conversations` (`api.session_home` picks the folder). Imported facts live in `imports/memory.json`.
- **`search_index.py`, `web_providers.py`, `secrets_store.py`**: search.
  - Ranking: BM25 plus Ollama embeddings (`VectorStore`, background `Indexer`), fused with RRF.
  - Web: Tavily (default), Brave, Exa and DuckDuckGo adapters with `WebSearchError.kind` fallbacks.
  - Keys: macOS Keychain, or `SECRETS_BACKEND=file`.
  - The API registers the agent's `search` tool and a privacy-guarded `web_search` tool (`blocked_terms`).
  - `speaker_id.memory_matches` and `api.related_conversation_people` add memory and past-chat evidence.
- **Frontend (`web/src`):**
  - `components/app-provider.tsx` holds global state: models (polled), the selected model (localStorage plus `preferences`), sessions with search, the settings dialog and the sidebar.
  - Pages: `/` and `/c/[id]` (`ChatView`; the first message creates the session and uses `history.replaceState` so the view doesn't remount mid-stream; a question in `sessionStorage["quorum:ask:<id>"]` is sent once on load), `/meetings` (`NewMeeting` with the Record a call tab, `JobPanel` polling `?partial=true`, `MyActions`), `/meetings/[id]` (transcript, Speakers, Summary and Check panels).
  - Player: `components/meetings/player.tsx` (`MeetingPlayer`, `playFrom` → `quorum:play`, `quorum:playing` marks the heard line; fullscreen, S/M/L size, captions from `lib/transcript-export.lines`; view in localStorage `quorum:player-view`). Copy/Download: `lib/transcript-export.ts` (plain, timed, Markdown, VTT, SRT, client-side) over transcriber `GET /transcripts/{id}/media[/{part}]` (call/mic tracks or a cached mix, Range; proxied by the API). `_recording_of` trusts the transcript's saved `source_path` (written by the service), so meetings keep playing after the watch folder changes. A moved recording: `FindRecording` → `GET /transcripts/{id}/recording/candidates` (same name in `HOME_FOLDERS` + meetings folders, `match` = same length ±max(2 s, 1%)) and `POST /transcripts/{id}/recording` (a candidate, cloned into uploads with `cp -c`, or an upload; another length → 409). `/recordings` maps a relinked copy to its transcript by `source_path`.
  - Recording: `components/meetings/recording.tsx` (`RecordTab`, `RecordingBar`, `RecordingIndicator` in the top bar, `LivePanel` docked by `AppShell`; `app-provider` polls `/api/meetings/record/status`, every 2 s while recording). Stop uses the New meeting choices saved in `sessionStorage` (`quorum:record-options`).
  - Meeting check: `lib/meeting-check.ts` (a small store per meeting, `runCheck` over SSE, `notify` = browser Notification with the count only) and `components/meetings/meeting-check.tsx` (`AlertsView`, `MeetingCheckRunner` for automatic checks during a call, `MyActions`).
  - Shared pieces: `components/settings/*` (tabbed dialog), `lib/api.ts` (fetch and SSE client, types).
  - The design tokens in `globals.css` are CSS variables, with a `.dark` override set by next-themes.

### Recording calls (transcriber)
- **`transcriber/record/quorum_record.swift`** → `transcriber/bin/Quorum Recorder.app` (`record/build.sh`, ad-hoc signed: macOS ties its permissions to the
  cdhash, so a changed recorder must be allowed again; a name-only designated requirement isn't accepted),
  embedded Info.plist; built by setup_local_ai.sh or `recorder.ensure_app`). Opened with `open -n -g` so macOS keeps
  its own Screen & System Audio Recording / Microphone permissions, and a transcriber restart never stops it.
  - `list` (displays, apps with windows, microphones); `start --app|--display|--audio-only [--mic] --out --live --events`.
  - Screen access: with Screen & System Audio Recording allowed (`CGPreflightScreenCaptureAccess`), `list` returns apps with
    windows and `start --app|--display` records them directly. Otherwise (`list` says `picker: true`; usual on the
    Jamf-managed, standard-account Mac, where the switch is shown on but never honoured for the ad-hoc app) or with
    `--app-any`, `start` emits `picker` and opens `SCContentSharingPicker` (no TCC screen permission needed); the
    recorder waits `PICK_TIMEOUT`, a closed picker is error `cancelled`, and `started.source` carries what was picked.
  - SCStream filtered to the app (or display), 2 fps video, system audio and `captureMicrophone` as separate streams.
  - AVAssetWriter `.mov`: audio track 0 = call ("System audio"), track 1 = mic ("Microphone"), 10 s fragments.
  - `<file>.quorum.json` holds the track layout.
  - Mic follows the default input (CoreAudio listener) or falls back when the chosen one disconnects; gaps are padded with silence.
  - `--live`: raw 16 kHz PCM per track plus `frames/<second>.jpg` every 2 s.
  - JSON-line events (launched, started, levels, warning, stopped, error) appended to the events file.
- **`transcriber/recorder.py`**: one recording at a time (`recording.json`).
  - Records to a hidden `.recording-<id>.mov` in uploads (never listed), renamed to `<title> <date>.mov` at stop.
  - Status: recording / stopped (ended by itself) / interrupted (helper died).
  - Warns about a silent call track (`call_silence_warning`).
  - `busy()` makes the job worker and `/health.recording` (so Ollama work) wait.
  - Tests set `QUORUM_RECORD_BIN` to `transcriber/tests/fake_recorder.py`.
- **`pipeline.track_roles`** (layout file, else track titles) switches `run()` to `transcribe_call_tracks`:
  - Whisper per track, with silent mic windows skipped (`is_silent`).
  - `drop_echo` removes the call leaking into the mic.
  - pyannote runs on the call track only, with `others_hint` (count − 1).
  - `merge_tracks` merges phrase by phrase.
  - Transcript fields: `confirmed_speakers`, `audio_tracks`, `recorded_by_quorum`, and `recording_id` (set by the worker).
  - Any other recording keeps the mixed (`amix`) path.
- **`transcriber/live.py`** (`LiveDraft`, optional): about every 30 s it cuts the new PCM at a pause (1 s overlap), runs Whisper on both tracks, and labels lines "You" / the name OCR'd from the frame of that moment / "Others".
  - Pauses on memory pressure, or on `hold` from `/record/live/pause` (a local check model; Whisper is freed).
  - Resumes from `live.json`.
  - The final transcription waits for it (`live.active()`).
- **Endpoints:** `/record/sources|start|status|stop|live|live/markdown|live/pause|live/resume`.

### Transcriber additions
On-screen names: `transcriber/screen_names.py` runs as the `screen` stage after diarization (video only; options per
job `screen: {enabled, vision_model, vision_cap, debug, participants}` from the API's prefs). It first scans the
whole recording for the participants list (`transcriber/participants.py`: one frame a second in 5-minute ffmpeg
chunks, OCR again only when the view changed; `find_panel` (heading, Invited section, role lines skipped) /
`find_strip` (evenly spaced names at the right while sharing); `speaking` = the one row whose vivid-pixel share
rises above its own median; `summarise` → transcript `participants` {names, invited, frames…}; `list_evidence` maps
list speakers to diarized turns). `aggregate` counts per name `count` (tiles), `list`, `both`; conflicts carry
`ocr`/`vision`/`list`; the vision prompt also asks for the list's speaker (`list_name`) when a frame shows the list.
`speaker_id.screen_evidence` uses `LIST_*` and `SCREEN_BOTH`. Pref `participants_scan` (default on). The vision model always reviews
(`pick_for_review`: every speaker, unsettled frames incl. `screen_share` first; `review_frame` → confirmed /
corrected (both kept, `conflicts`) / filled / no_answer; `review_budget` = pref `vision_frames` or 4 per speaker,
12–48; `screen_info.review_reason` when 0 reviewed). `vision_fallback`/`vision_cap` prefs are legacy. Frames via ffmpeg, highlighted tile by
border colour (vivid, or white when the frame has little text), names via `transcriber/bin/quorum-ocr` (Swift +
Vision, built by setup_local_ai.sh or on first use), unresolved frames to the local Ollama vision model with the
on-screen labels as the only allowed answers. Results: `screen_names` + `screen_info` on the transcript. Follow-up job
kinds `screen` (`POST /transcripts/{id}/screen-names`) and `voiceprints` (backfill with pyannote's embedding model only).
Jobs carry the meeting's `models` {speaker_model, frames_model, summary_model, after} into the transcript;
`pipeline.wait_for_ollama_idle` frees Ollama's models and waits until `/api/ps` is empty before Whisper loads (stage
`freeing_memory`); `/estimate` takes `vision_seconds` (the frames model's speed).
`POST /jobs/{id}/cancel` drops queued jobs or stops a running one at its next progress call (`JobCancelled`, re-raised past the diarization fallback). `GET /jobs/{id}/partial` returns the text from finished Whisper windows. `run()` stores `speaker_embeddings` under the final labels. The service starts exactly one worker thread (`_WORKER`).

```
Next.js :3000 ──▶ api/main.py :8000 ──cloud──▶ GAIA (GLM-5.3-Flash) / Claude / OpenAI (Settings → Models)
                                    ──local──▶ Ollama   :11434 (native, Metal GPU)
                                    ─────────▶ transcriber :8765 (native FastAPI, 127.0.0.1 only)
```

### Shared modules
- **`llm_backends.py`**: `Backend(kind, model)`, plus `stream_chat` and `complete`.
  - **Cloud** code is moved verbatim from the original Streamlit app.
  - **Local** uses Ollama's native `/api/chat` with `think: false` and strips `<think>` blocks.
  - Every local request sends `num_ctx` = `LOCAL_CONTEXT_LENGTH` (`OLLAMA_CONTEXT_LENGTH`, default 32768). Without it, Ollama defaults to a 4096-token window on a 16 GB Mac and drops whatever doesn't fit, with no error.
  - `num_ctx` only applies when Ollama loads the model, so `ensure_window` checks `/api/ps` first and unloads a model loaded with a smaller window (e.g. by `ollama run`). Tests stub `_loaded_window`.
  - `clean_messages` sends only role and content, so UI metadata like `message["model"]` never reaches a model.
  - `LocalModelError` (a subclass of `RequestException`) never falls back to the cloud.
- **`local_models.py`**: Ollama discovery (`/api/tags`, embedding models filtered out), and context window (`/api/show`, `effective_context`).
- **`sensitivity.py`**: `detect()` returns reasons, using keywords, PII and secret patterns, project `confidential_terms`, and transcript attachments.
- **`long_context.py`**: handles transcripts larger than the local model's window.
  - `plan()` chooses `direct`, `map_reduce` (summary-type requests) or `retrieve` (questions, via local `nomic-embed-text` with a keyword fallback), and builds the user-facing notice with a time estimate.
  - Estimates use `llm_backends.MEASURED_SPEEDS`, the real Ollama timings recorded from each local reply, and fall back to the env defaults until a reply has been measured.
  - Map-reduce parts are 12k tokens and cached one at a time (`long_notes_cache[model]`), so a retry skips finished parts.
  - Local `complete()` streams internally, so `LOCAL_LLM_TIMEOUT` only bounds silent gaps between bytes.
- **`meetings.py`**: HTTP client for the transcriber.
- **`speaker_guess.py`**: guesses speakers for transcripts without labels (used by `speaker_id`). It always uses the local model (`temperature=0`), never the cloud.
  - The model rewrites ~3k-character parts as a `Name: words` script, guided by a worked example. Small models do this far better than answering with sentence numbers, and reasoning mode was too slow.
  - `align` only reads speakers off the script; the original sentences are kept verbatim. `fix_addressed` stops a sentence that greets or addresses a name from being given to that person.
  - Results are saved with `PUT /transcripts/{id}/segments` (the original is kept in `before_speaker_guess` for `undo-speakers`), and the Markdown notes that speakers were guessed.
  - `candidate_names` finds people mentioned in the text (greeted, addressed, talked about; words never written in lower case). They're listed in the prompt, and `name_generic_speakers` names leftover "Speaker N" labels from clues ("Owen, can you…" → next speaker; "thanks Owen" → previous speaker; "I'm Owen"). Example names copied by the model become "Speaker N" (`drop_example_names`).
  - Measured with `qwen3:8b`: a 21k-character raw meeting took 7.8 min (8 parts) and got real names. Turn boundaries remain rough (about 45–70% of sentences right on a 3-person sample).
- **`transcriber/`**: a native service with its own Python 3.12 venv.
  - Recordings: any ffmpeg-readable file, including macOS screen recordings (⇧⌘5 `.mov`). `probe_media` counts audio tracks: a recording with none (screen recording made without a microphone) is refused with `NO_AUDIO_MESSAGE` (`/estimate` `has_audio`, `POST /jobs` 422), and several tracks (mic + system audio) are mixed with `amix` (`audio_map_args`).
  - `pipeline.py` steps: ffmpeg to 16 kHz WAV, then Whisper in 2-minute windows (boundaries snapped to silence, saved per window so it can resume), then pyannote in 60-minute overlapping pieces whose speakers are matched across pieces by voice-fingerprint cosine (`match_speakers`), then `assign_speakers`, then `.json` and `.md` output.
  - `server.py`: job queue (one job at a time), estimates (`speed.json` calibrated after each job), resumable chunked uploads (`upload.html`, which can also start the job itself), and path-traversal checks. Recordings may come from the watch folder or the uploads folder (`media_roots`), and API paths are absolute. `DELETE /transcripts/{id}?recording=true` also deletes the recording, but only when it's in the uploads folder; watch-folder files are never deleted.
  - The watch folder can be changed from the UI (`POST /settings`). It's saved in `TRANSCRIPTS_DIR/settings.json`; `config.DEFAULT_MEETINGS_DIR` is the env default. Uploads always stay in `config.UPLOADS_DIR`.
  - Jobs take an optional speaker count (`{"num": n}` or `{"min", "max"}`). `pipeline.speaker_kwargs` passes it to pyannote; for long recordings split into pieces, an exact count becomes a per-piece maximum.
  - `text_import.py` turns existing conversation text (Teams/Zoom VTT, SRT, `Name   0:03` exports, `Name: text` lines, chat logs) into the same transcript shape, with `source_type: "text"` (`POST /transcripts/import`). The app extracts `.docx`/`.pdf` text first via `conversation_file_text`, without the chat `MAX_CHARS` cut.
  - It refuses to start without `HF_HUB_OFFLINE=1` or without Whisper cached. Speaker labels degrade to "unavailable" if pyannote isn't downloaded, because the models are gated on Hugging Face. Every repo in `config.DIARIZATION_REPOS` must be cached, including the `plda/` folder of `pyannote/speaker-diarization-community-1`, which pyannote 4 loads even for the 3.1 pipeline. If labelling fails during a job, the transcript is still saved, with `diarization_error` set.
  - It unloads Ollama models before each job, because 16 GB RAM can't hold both.
- **`projects.py`**: each project is JSON in `PROJECTS_DIR`. `PROJECT_FIELDS` also covers `default_model_mode`, `default_local_model` and `confidential_terms`. String fields are normalized in `_normalize`.
- **`tools.py`, `connectors.py`, `doc_extract.py`, `web_search.py`**: the tool registry, skills and system prompt; global MCP connectors; document text extraction (capped at `MAX_CHARS`); and DuckDuckGo search.

### Routing (the chat turn in `api/main.py`)

The selected model decides the backend. With a cloud model, a turn is checked first:
- If `confirm_cloud_sensitive` is on and `agent.sensitive_reasons` finds something not yet approved for that provider, the API emits `confirm` (`sensitive`) and stops.
  - `confirm: "local"` runs the turn on a local model and marks the chat `confidential`, so later cloud turns ask again.
  - `confirm: "cloud"` remembers the reasons (`cloud_approved_reasons` for GAIA, `cloud_approved[pid]` otherwise).
- Local models never ask.

**Local-path guards:** `agent.build_turn_context` drops `web_search` and non-localhost connectors on a local model, and remote connectors in a confidential chat (`TurnState.remote_connectors=False`). Transcripts aren't truncated on the local path; the cloud path keeps the 50k-character cap.

### Agent turn (`agent.generate_reply`)

The model has no native tool-calling. Every tool use goes through **JSON that the model writes in plain text**, which the app parses with regex.

1. **Web search check:** if `web_search` is allowed (cloud only) and enabled, a non-streaming search-decision call may add search results first.
2. **Up to `MAX_TOOL_ROUNDS` rounds:** each round `stream_round` streams a reply and `detect_action` checks it for, in order:
   - a skill request (progressive disclosure)
   - a `{"tool": …}` call (project tools only)
   - a `{"connector": …}` call
   - an *unavailable* request: a reply that is nothing but tool JSON for something that can't run. The model is told why, which covers the offline case.
3. **Feeding results back:** each call is appended as an assistant message, followed by a `[Tool output — …]` user message. Raw tool JSON is erased from the UI and never returned as the answer.
4. **Limits:** a call identical to an earlier one isn't re-run. When the round limit is hit, one final plain answer is forced, falling back to `TOOL_LOOP_FALLBACK`.

Other pieces: slash-command skills (`turn_skill`); the built-in "Meeting minutes" skill, offered when a transcript is attached; and memory extraction (`extract_memories`).

## Testing notes

- **Isolation (`tests/conftest.py`, autouse):** every test gets its own memory store, cloud-provider config and file
  secrets; the memory migration can only read files inside the test's tmp_path; `api.main`'s `ATTENDEES_DIR`,
  `LABELS_DIR` and `SUMMARIES_DIR` point at tmp_path (`no_real_meeting_files`). Never point tests at `data/`.
  `meetings.TRANSCRIBER_URL` and `llm_backends.GAIA_ENDPOINT` (except in test_cloud_regression) point at a closed
  port and `model_registry.capabilities` is stubbed, so tests never reach the real transcriber, GAIA or Ollama.
  E2E summaries answer the model dialog with `helpers.chooseSummaryModel`.
- **Speaker identification quality:** `tests/fixtures/speaker_meetings.py` (names inferable from the text, with
  `score`), used by `scripts/benchmark_screen_names.py --speaker-models …`.
- **Fakes:** `tests/fake_cloud.py` (Anthropic + OpenAI; keys `sk-ant-test` / `sk-openai-test`; `*-missing/-limit/-busy`,
  `claude-refuse`; `REVIEW_PLAN` drives speaker-review replies), `tests/fake_mcp.py` (registry, `/mcp`, `/mcp-key`,
  `/mcp-oauth`, `/sse`, OAuth server), `tests/fake_mcp_stdio.py`. In e2e they run on :18997 / :18996 inside
  mock_services, and `CONNECTORS_TEST_STDIO` swaps the package command for the fake stdio server.
- **Recording and meeting check:** `web/e2e/mock_services.py` fakes `/record/*` (admin `record_error`,
  `live_line_seconds`; live lines from `LIVE_SCRIPT`; a recorded call's transcript has `confirmed_speakers`), and
  the fake Ollama answers the check prompt (`check_reply`, including one unsourced alert that must be dropped).
  Transcriber: `test_recording.py` (fake helper), `test_live.py` (synthetic PCM, fake Whisper/OCR). Pop-ups are
  tested by replacing `window.Notification` in the page.
- **Synthetic video:** `transcriber/tests/synthetic_meeting.py` draws gallery calls (Pillow + ffmpeg; this ffmpeg has
  no drawtext), modes `panel` / `panel_other` (participants panel with coloured avatars, ring or mic speaking marker,
  Invited section) and `share_strip`. The e2e transcript `t-video` carries `screen_names`; `t-panel` (added with
  `POST /__admin/add-transcript {"fixture": "panel"}`) carries `participants` and list counts.
  `POST /__admin/spelling-plan` drives the fake cloud's spelling step (`fake_cloud.SPELLING_PLAN`).

- **Web e2e (`web/e2e`):**
  - `helpers.resetAll` resets the mocks, chats, people, preferences and connectors before each test.
  - The fake Ollama is a child process that `POST /__admin/ollama {"running": false}` really stops.
  - Workers = 1, because the backend state is shared.
  - The dev server uses `NEXT_DIST_DIR=.next-e2e`, so it can run next to the normal one.
  - Fixtures are synthetic.
    - `tests/fixtures/meetings/*.json`: one meeting per type, plus `*.notes.json`, the JSON the fake model returns.
    - `web/e2e/fixtures/`: claude.ai exports.
    - Regenerate both with their `make_*.py` scripts.
  - The fake Ollama serves `/api/embed` and answers the notes prompt with the fixture's notes, or with notes read off the lines. `Find …` messages trigger the search tool.
  - The fake Tavily and Exa live under `/tavily` and `/exa` (admin `web_mode`: ok, invalid or rate). Brave points at a closed port, which tests "offline".
  - `tests/test_api.py::isolate_new_storage` points imports, embeddings (`fake_embed`), template copies and secrets at `tmp_path`.
- **API tests (`tests/test_api.py`):** they monkeypatch the `api.main` path constants and `model_registry.ollama_state`, fake Ollama by patching `llm_backends.requests.post`, and parse SSE from `response.text`.

- **Agent tests (`tests/test_agent.py`):** a `FakeProvider` replies from a script `(messages, call_number) -> text`; `tools.run_tool` and `connectors.load_connectors` are patched.
- **Storage:** storage functions take explicit directories, or tests monkeypatch the path constants (`connectors.CONNECTORS_FILE`, `projects.PROJECTS_DIR`).
- **Measured on the M4 MacBook Air (16 GB):**
  - `qwen3:8b` reads 100–120 tokens/s and writes 13 tokens/s (7.5 at 16k context).
  - Whisper runs at about 0.115 s per second of audio.
  - Ollama's `qwen3:4b` is a reasoning-only model that ignores `think:false`, so don't use it for background calls.
