// Client for the Quorum API (api/main.py). All requests go from the browser to the local API.

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

/**
 * What kind of failure an ApiError is, so the UI can show the right help (API down, local model,
 * transcriber…).
 */
export type ErrorKind = "network" | "http" | "local_model" | "transcriber" | "server" | "cloud";

export class ApiError extends Error {
  status: number;
  kind: ErrorKind;
  /** A machine-readable reason from the API, e.g. the call recorder's "permission_screen". */
  code?: string;
  constructor(message: string, status = 0, kind: ErrorKind = "http", code?: string) {
    super(message);
    this.status = status;
    this.kind = kind;
    this.code = code;
  }
}

const NETWORK_MESSAGE = `Can't reach the Quorum API at ${API_URL}. Start it with scripts/start_app.sh.`;

/**
 * Turns an HTTP error response into an ApiError, using the API's JSON 'detail' message when there is one.
 */
async function parseError(res: Response): Promise<ApiError> {
  let detail = `${res.status} ${res.statusText}`;
  let code: string | undefined;
  try {
    const body = await res.json();
    if (typeof body.detail === "string") detail = body.detail;
    else if (body.detail && typeof body.detail.message === "string") {
      detail = body.detail.message;
      code = typeof body.detail.kind === "string" ? body.detail.kind : undefined;
    }
    else if (Array.isArray(body.detail)) detail = body.detail.map((d: { msg: string }) => d.msg).join("; ");
  } catch {
    /* not JSON */
  }
  return new ApiError(detail, res.status, res.status === 503 && !code ? "transcriber" : "http", code);
}

/**
 * Sends one request to the API and returns its JSON; network failures and error replies become ApiError.
 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, init);
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    throw new ApiError(NETWORK_MESSAGE, 0, "network");
  }
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as T;
}

/** Request options for a JSON body with the given HTTP method. */
const json = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => request<T>(path, json("POST", body ?? {})),
  put: <T>(path: string, body?: unknown) => request<T>(path, json("PUT", body ?? {})),
  patch: <T>(path: string, body?: unknown) => request<T>(path, json("PATCH", body ?? {})),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
  /** GET that answers with plain text (Markdown). */
  text: async (path: string): Promise<string> => {
    let res: Response;
    try {
      res = await fetch(`${API_URL}${path}`);
    } catch {
      throw new ApiError(NETWORK_MESSAGE, 0, "network");
    }
    if (!res.ok) throw await parseError(res);
    return res.text();
  },
  upload: <T>(path: string, form: FormData) => request<T>(path, { method: "POST", body: form }),
  putBytes: <T>(path: string, data: Blob, signal?: AbortSignal) =>
    request<T>(path, { method: "PUT", body: data, signal, headers: { "Content-Type": "application/octet-stream" } }),
};

/** POST that answers with Server-Sent Events; calls onEvent for each `data:` JSON event. */
/** Upload a file in chunks (the same path as a new meeting's recording); returns its path on this Mac. */
export async function uploadInChunks(file: File, onProgress: (pct: number) => void): Promise<string> {
  const init = await api.post<{ upload_id: string; chunk_size: number; received: number[] }>(
    "/api/meetings/uploads", { name: file.name, size: file.size });
  const total = Math.max(1, Math.ceil(file.size / init.chunk_size));
  const have = new Set(init.received);
  for (let n = 0; n < total; n++) {
    if (!have.has(n)) {
      await api.putBytes(`/api/meetings/uploads/${init.upload_id}/chunks/${n}`,
        file.slice(n * init.chunk_size, (n + 1) * init.chunk_size));
    }
    onProgress(((n + 1) / total) * 100);
  }
  const done = await api.post<{ path: string }>(`/api/meetings/uploads/${init.upload_id}/complete`);
  return done.path;
}

export async function stream<E = Record<string, unknown>>(
  path: string,
  body: unknown,
  onEvent: (event: E) => void,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, { ...json("POST", body), signal });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    throw new ApiError(NETWORK_MESSAGE, 0, "network");
  }
  if (!res.ok || !res.body) throw await parseError(res);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      for (const line of block.split("\n")) {
        if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)) as E);
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Types (mirror api/main.py)
// ---------------------------------------------------------------------------

/** Whether a model can be used now ('ready') or why not. */
export type ModelStatus = "ready" | "not_downloaded" | "ollama_down" | "not_configured";

/** Progress of a model download (Ollama pull). */
export interface PullState {
  model: string;
  state: "running" | "done" | "error" | "cancelled";
  status: string;
  completed: number;
  total: number;
  percent: number;
  error: string;
}

/** A chat model in the picker: id, label, where it runs (local/cloud) and its status. */
export interface ModelEntry {
  id: string;
  provider: "ollama" | "gaia" | "cloud";
  provider_id?: string;
  provider_kind?: string;
  model: string;
  label: string;
  group: "local" | "cloud";
  description?: string;
  size_gb?: number;
  installed_bytes?: number;
  status: ModelStatus;
  ollama_too_old?: boolean;
  min_ollama?: string;
  uses?: string[];
  pull?: PullState;
}

/** GET /api/models: the models, which one is the default and which is selected. */
export interface ModelsResponse {
  ollama_running: boolean;
  ollama_version: string;
  default: string;
  selected: string;
  models: ModelEntry[];
}

/** Search settings stored in the preferences. */
export interface SearchPrefs {
  web_enabled: boolean;
  provider: string;
  max_results: number;
  sources: { chats: boolean; transcripts: boolean; memory: boolean; connectors: boolean };
  embed_model: string;
}

/** The user's preferences (GET/PUT /api/preferences). */
export interface Prefs {
  selected_model: string;
  active_project: string;
  confirm_cloud_sensitive: boolean;
  web_search: boolean;
  search: SearchPrefs;
  meeting_notes_skill: boolean;
  imported_cloud_notice: boolean;
  cloud_notices: string[];
  voiceprints: boolean;
  speaker_cloud_review: boolean;
  screen_names: boolean;
  vision_fallback: boolean;
  vision_cap: number;
  vision_frames: number;  // frames the vision model reviews per meeting; 0 = auto (4 per speaker, 12 to 48)
  /** Scan the whole video for the call's participants list (attendees, and a second check of who spoke). */
  participants_scan?: boolean;
  screen_debug: boolean;
  vision_model: string;
  speaker_model: string;
  summary_model: string;
  record_live: boolean;
  /** Meeting check: "" = the local default model. */
  check_model: string;
  check_popups: boolean;
  check_auto_minutes: number;
  check_days: number;
  check_all_projects: boolean;
}



/** One chat message; answers also say which model wrote them and which memory facts were used. */
export interface Message {
  role: "user" | "assistant";
  content: string;
  model?: string;
  model_id?: string;
  memory_used?: string[];
}

/** A chat in the sidebar list. */
export interface SessionSummary {
  id: string;
  title: string;
  updated_at: string;
  created_at: string;
  message_count: number;
  confidential: boolean;
  model_id: string;
  preview: string;
  imported?: boolean;
  read_only?: boolean;
  imported_at?: string;
  imported_source?: string;
  continued_from?: { id: string; title: string; source: string };
}

/** A file or meeting attached to a chat (name and size; the text stays on the server). */
export interface Attachment {
  name: string;
  kind: "document" | "transcript";
  chars: number;
  transcript_id?: string | null;
  /** The recording id of a call's live draft (refreshed before each message), until its final transcript exists. */
  live?: string | null;
}

/** A full chat: its messages and attachments. */
export interface Session extends Omit<SessionSummary, "imported"> {
  messages: Message[];
  attachments: Attachment[];
  local_model: string;
  imported: { source: string; uuid: string; imported_at: string; project: string } | null;
}

/** A skill: a named set of instructions the model can use. */
export interface Skill {
  name: string;
  description: string;
  content: string;
}

/** A project: its chats share instructions, documents, memory and skills. */
export interface Project {
  id: string;
  name: string;
  instructions: string;
  memory: string[];
  skills: Skill[];
  tools: string[];
  confidential_terms: string[];
  documents: { name: string; kind: string; chars: number }[];
  active: boolean;
  updated_at?: string;
}

/** A tool offered by a connector (MCP server). */
export interface ConnectorTool {
  name: string;
  description: string;
}

/** A connector as shown in Settings (secrets never included, only whether they're set). */
export interface Connector {
  id: string;
  kind: string;
  name: string;
  description: string;
  url: string;
  transport?: "http" | "sse" | "stdio";
  enabled?: boolean;
  tools?: ConnectorTool[];
  remote?: boolean;
  running?: boolean | null;
  auth?: { type: "none" | "header" | "oauth"; header?: string };
  signed_in?: boolean;
  secrets_set?: Record<string, boolean>;
  last_error?: string;
  tools_checked_at?: string;
  source?: { type: string; directory?: string; server?: string; version?: string; repository?: string; publisher?: string };
  stdio?: { registry_type: string; identifier: string; version: string; command: string[]; env: Record<string, string>; secret_env: string[] };
}

/** A setting or key a package connector needs (from the MCP Registry). */
export interface DirectoryEnv {
  name: string;
  description: string;
  required: boolean;
  secret: boolean;
  default: string;
}

/** A server found in the MCP Registry: how it can be added (remote URL or package). */
export interface DirectoryResult {
  id: string;
  name: string;
  title: string;
  description: string;
  version: string;
  publisher: string;
  repository: string;
  website: string;
  official: boolean;
  namespace_verified: boolean;
  updated_at: string;
  installed: boolean;
  needs: string[];
  transports: string[];
  remotes: { type: "http" | "sse"; url: string; headers: { name: string; description: string; required: boolean; secret: boolean; template: string }[] }[];
  packages: { registry_type: string; identifier: string; version: string; runtime: string; transport: string; supported: boolean; args: string[]; env: DirectoryEnv[] }[];
}

/** An added cloud provider (Claude, OpenAI, compatible) and its models. */
export interface CloudProvider {
  id: string;
  kind: "anthropic" | "openai" | "openai_compatible";
  label: string;
  base_url: string;
  default_base_url: string;
  auth_header: "bearer" | "api-key";
  has_key: boolean;
  key_hint: string;
  models: { id: string; label: string; manual?: boolean }[];
  enabled: string[];
  added_at: string;
  tested_at: string;
  last_error: string;
}

/** GET /api/providers: added providers, the kinds that can be added, and GAIA. */
export interface ProvidersResponse {
  providers: CloudProvider[];
  kinds: { kind: string; label: string; base_url: string }[];
  gaia: { label: string; model: string; configured: boolean };
}

/** One remembered fact, with where it came from (claude.ai, chat, meeting, you…) and when. */
export interface MemoryFact {
  id: string;
  text: string;
  heading: string;
  source: "claude.ai" | "chat" | "meeting" | "user" | "cloud_review";
  date: string;
  edited: boolean;
}

/** A memory entry: you (profile), a person, a project or a topic, with its facts. */
export interface MemoryEntry {
  id: string;
  kind: "profile" | "person" | "project" | "area";
  name: string;
  description: string;
  aliases: string[];
  project: string;
  facts: MemoryFact[];
  updated_at?: string;
  source?: string;
  role?: string;
  team?: string;
  projects?: string[];
  relationships?: string[];
  meetings?: string[];
  screen_names?: { tid: string; speaker: string; name: string; count: number; at: string }[];
  voiceprint_count?: number;
  voiceprint_sources?: Record<string, number>;
  is_user?: boolean;
  quorum_project?: string;
}

/** GET /api/memory: all entries, counts and likely duplicates. */
export interface MemoryResponse {
  entries: MemoryEntry[];
  counts: Record<string, number>;
  duplicates: { entries: string[][]; facts: Record<string, string[][]> };
  sources: Record<string, string>;
  migrated_at: string;
  backup: string;
}

/** A person known from meetings, memory or imports (voice fingerprints only counted, never sent). */
export interface Person {
  id: string;
  name: string;
  role: string;
  notes: string;
  aliases: string[];
  meetings: string[];
  voiceprint_count: number;
  screen_names?: { tid: string; speaker: string; name: string; count: number; at: string }[];
  team?: string;
  projects?: string[];
  relationships?: string[];
  source?: string;
  imported_at?: string;
  evidence?: string[];
  is_user?: boolean;
}

/** A recording in the watch or uploads folder. */
export interface Recording {
  path: string;
  name: string;
  size: number;
  modified: string;
  source: "upload" | "folder";
  transcript_id: string | null;
}

/**
 * POST /api/meetings/estimate: duration, time estimates for each step, disk space and the models to be
 * used.
 */
export interface Estimate {
  duration: number;
  audio_tracks: number;
  has_audio: boolean;
  no_audio_message: string;
  estimate_low: number;
  estimate_high: number;
  temp_disk_bytes: number;
  free_disk_bytes: number;
  disk_ok: boolean;
  diarization: boolean;
  queued_ahead: number;
  has_video?: boolean;
  screen_estimate?: { frames: number; low: number; high: number } | null;
  models?: {
    chosen: { speaker_model: string; frames_model: string; summary_model: string };
    use: { speaker_model: string; frames_model: string; summary_model: string };
    fallbacks: { kind: "speaker_model" | "frames_model"; wanted: string; using: string }[];
    screen_enabled: boolean;
  };
  followups?: {
    speakers?: { model: string; low: number; high: number };
    summary?: { model: string; model_id: string; local: boolean; low: number | null; high: number | null; ask?: boolean };
  };
}

/** The 'identify speakers and summarise after transcription' work for a job, and its state. */
export interface FollowUp {
  job_id: string;
  state: "waiting" | "running" | "done" | "needs_ok" | "needs_model" | "error" | "cancelled";
  step: "transcription" | "speakers" | "summary";
  message: string;
  transcript_id?: string;
  speaker_model_used?: string;
  summary_model_used?: string;
}

/** A meeting's model choices and which models were actually used. */
export interface MeetingModels {
  speaker_model: string;
  frames_model: string;
  summary_model: string;
  speaker_used: string;
  frames_used: string;
  summary_used: string;
  after: boolean;
}

/** A local model in a meeting model chooser (installed? can it read video? recommended?). */
export interface LocalModelChoice {
  model: string;
  label: string;
  description?: string;
  size_gb?: number;
  installed: boolean;
  recommended: boolean;
  vision?: boolean;
}

/** GET /api/models/speaker or /api/models/vision: the choices, downloads and the default. */
export interface LocalModelList {
  default: string;
  models: LocalModelChoice[];
  ollama_running: boolean;
  pulls: Record<string, PullState>;
  selected: string;
  effective: string;
  frames?: string;
}

/** A transcription job's progress (and, while running, the text so far). */
export interface Job {
  id: string;
  name: string;
  path: string;
  status: "queued" | "running" | "done" | "error" | "cancelled";
  stage: string;
  progress: number;
  duration: number;
  elapsed?: number;
  eta?: number;
  error: string | null;
  transcript_id: string | null;
  /** transcribe (default), screen (names from the video), voiceprints, reference */
  kind?: string;
  partial?: { segments: { start: number; end: number; text: string }[]; windows: number };
  followup?: FollowUp | null;
}

/** A transcript in the list (without its lines). */
export interface TranscriptMeta {
  id: string;
  title: string;
  source_name: string;
  source_type?: string;
  created_at: string;
  duration: number;
  language: string;
  diarization: boolean;
  speakers: string[];
  speaker_names: Record<string, string>;
  speaker_source?: string;
  timestamps?: boolean;
  uploaded_recording?: boolean;
}

/** The name and role found for one speaker, how sure it is, and why. */
export interface SpeakerLabel {
  name: string;
  role: string;
  confidence: number;
  level: "high" | "medium" | "low" | "confirmed";
  evidence: string[];
  /** "microphone": you, on your own track in a call Quorum recorded (never changed automatically);
   *  "participants": spelled as in the call's participants list (the check at the end of identification). */
  source: "auto" | "user" | "cloud" | "microphone" | "participants" | "reference";
  cloud?: { model: string; reason: string; lines: number[]; kept: boolean };
  previous?: SpeakerLabel;
  /** not_in_list: the name matches nobody in the meeting's participants list. */
  flag?: "not_in_list";
  /** In the participants list (exactly, or after the check took its spelling). */
  listed?: boolean;
  /** For a speaker without a name: listed people no speaker has yet. */
  suggestions?: string[];
}

/** One person in the meeting's attendees: from the video's participants list, a named speaker, or added by you. */
export interface Attendee {
  name: string;
  source: "video" | "speaker" | "you";
  speaker: string | null;
  speaker_name: string;
  spoke: boolean;
  first_seen: number | null;
  last_seen: number | null;
  first_spoke: number | null;
  frames: number;
}

/** A change the participants-list check made to a speaker's name. */
export interface ParticipantsChange {
  speaker: string;
  from: string;
  to: string;
  how: string;
  reason: string;
  state: "pending" | "kept" | "reverted" | "edited";
}

/** The last check of the speaker names against the participants list. */
export interface ParticipantsCheck {
  changes: ParticipantsChange[];
  flagged: { speaker: string; name: string }[];
  confirmed: string[];
  suggestions: string[];
  roster: string[];
  created_at: string;
}

/** One of your edits of the transcript (applied last; `stale`: its line can't be found any more). */
export interface TranscriptEdit {
  id: string;
  kind: "text" | "speaker" | "split" | "merge";
  text: string;
  state: "applied" | "stale";
  created: string;
}

/** A name read from a speaker's video tile, and how often it was seen. */
export interface ScreenName {
  name: string;
  count: number;
  times: number[];
  truncated: boolean;
  ocr: number;
  speaker_view: number;
  vision: number;
  /** Frames where the participants list marked this name as speaking while this speaker talked. */
  list?: number;
  /** Sampled frames where the tile and the list showed this name at the same moment. */
  both?: number;
}

/** A moment two sources named different people: text reader (ocr), vision model, participants list. */
export interface ScreenConflict { t: number; ocr?: string; vision?: string; list?: string }

/** One change the cloud review made to a speaker's name, and its state (pending, kept, reverted…). */
export interface ReviewChange {
  speaker: string;
  name: string;
  role: string;
  confidence: number;
  reason: string;
  lines: number[];
  on_screen: boolean;
  from: { name: string; role: string; confidence: number; level: string };
  state: "pending" | "kept" | "reverted" | "edited";
}

/** The cloud review of a meeting's speaker names: changes, rejected suggestions and agreements. */
export interface SpeakerReview {
  status: "done" | "failed";
  model: string;
  provider: string;
  created_at: string;
  error?: string;
  changes: ReviewChange[];
  rejected: { speaker: string; name: string; why: string }[];
  agreed?: number;
  parts?: number;
  /** The spelling step before the names: fixes made (in the Corrections tab) and refused. */
  spelling?: { fixes: number; rejected: { line: number; text: string; why: string }[]; error?: string };
}

/** One line of a transcript: speaker, start and end time, text. */
export interface Segment {
  speaker: string;
  start: number;
  end: number;
  text: string;
  /** You edited this line (wording, speaker, split or merge). */
  edited?: boolean;
}

/** A full meeting for the meeting page: lines, speaker labels, summary, review and model choices. */
export interface Transcript extends TranscriptMeta {
  segments: Segment[];
  labels: Record<string, SpeakerLabel>;
  display_names: Record<string, string>;
  has_voiceprints: boolean;
  diarization_error?: string | null;
  summary: Summary | null;
  review?: SpeakerReview | null;
  models?: MeetingModels;
  followup?: FollowUp | null;
  screen_names?: Record<string, { names: ScreenName[]; frames: number; resolved: number; list_frames?: number;
                                  conflicts?: ScreenConflict[] }>;
  /** What the on-screen names stage did. The vision review (newer transcripts): frames reviewed, and how many it
   *  confirmed, corrected or filled; `review_reason` says why nothing was reviewed. */
  screen_info?: { status: string; frames: number; resolved: number; screen_share: number; unresolved: number;
                  vision_frames: number; vision_model: string; seconds: number; error: string;
                  reviewed?: number; confirmed?: number; corrected?: number; filled?: number; no_answer?: number;
                  review_reason?: string; list_frames?: number; list_agree?: number; list_conflicts?: number };
  /** The call's participants list read from the video: who joined (and when), who was only invited. */
  participants?: { status: string; names: { name: string; frames: number; first_seen: number; last_seen: number; spoke: number }[];
                   invited: string[]; count: number | null; frames_scanned: number; frames_with_list: number;
                   frames_speaking: number; kinds: string[] } | null;
  attendees?: { attendees: Attendee[]; invited: string[]; from_video: boolean };
  participants_check?: ParticipantsCheck | null;
  edits?: TranscriptEdit[];
  /** After an edit moved lines to a named person: offer to update their voice fingerprint. */
  voice_question?: { speaker: string; name: string };
  processing_seconds?: number;
  /** Corrections made with other transcripts of the same meeting (Voice Memos, claude.ai, …). */
  correction?: Correction | null;
}

/** Where another transcript of a meeting comes from (Voice Memos text and audio are one recording). */
export type RefSource = "voice_memos" | "claude" | "other";

/** Another transcript of the meeting, and how well it matched. */
export interface CorrectionRef {
  id: string;
  name: string;
  source: RefSource;
  kind: "text" | "audio";
  status: "ready" | "transcribing" | "failed";
  error?: string | null;
  job_id?: string | null;
  applied?: boolean;
  reason?: string | null;
  coverage?: number;
  share?: number;
  offset?: number | null;
}

/** One correction: words replaced, added or removed, or a turn boundary moved. `reverted`: undone by you. */
export interface CorrectionChange {
  id: string;
  /** word / insert / delete: words; turn: a boundary moved; speaker: a whole line given to another speaker;
   *  punct: a sentence split or joined; model: a sentence fix proposed by the chosen model. */
  kind: "word" | "insert" | "delete" | "turn" | "speaker" | "punct" | "model";
  from: string;
  to: string;
  sources: string[];
  seg: number | null;
  t: number;
  name_or_number: boolean;
  reverted?: boolean;
  /** model: the line no longer reads as the model saw it (run the review again). */
  stale?: boolean;
  label?: string;          // speaker / model: the new speaker label
  speaker_to?: string;     // model: the new speaker's name
  text?: string;           // speaker: the line
  reason?: string;         // model: why
  layer?: "review" | "cloud_review";  // model: the corrections review, or the cloud review's spelling fixes
  model?: string;
}

/** One line of the transcript with its corrections: what it said and says now. */
export interface CorrectionSentence {
  seg: number;
  before: string;
  after: string;
  ids: string[];
  t: number;
  speaker_from: string | null;
  speaker_to: string | null;
  sources: string[];
}

export interface Correction {
  refs: CorrectionRef[];
  changes: CorrectionChange[];
  /** Per segment index: [start, end, change id] character spans of corrected words. */
  marks: Record<string, [number, number, string][]>;
  names: Record<string, { name: string; share: number; words: number }>;
  stats: { words: number; turns: number; names: number; applied_refs: number; speakers?: number; sentences?: number;
           model?: number; model_stale?: number };
  sentences?: CorrectionSentence[];
  /** The model's review, if one was run: its fixes are in `changes` (kind "model"). */
  model?: { model: string; label: string; at: string } | null;
  /** Every model's fixes by source: review (the corrections review), cloud_review (the cloud review's spelling). */
  layers?: Record<string, { model: string; label: string; at: string; source?: string }>;
  reverted: string[];
  /** Changes you checked and kept. */
  kept?: string[];
  undone: boolean;
  updated_at: string;
}

/** How detailed meeting notes are. */
export type DetailLevel = "brief" | "standard" | "detailed";

/** Saved meeting notes: the Markdown, the model that wrote them and their options. */
export interface Summary {
  model: string;
  model_id: string;
  markdown: string;
  created_at: string;
  seconds: number;
  skill?: "meeting-notes";
  notes?: Record<string, unknown>;
  meeting_type?: string;
  detected_type?: { type: string; label: string; confidence: string; scores: Record<string, number> };
  detail?: DetailLevel;
  sections?: Record<string, boolean>;
  warnings?: string[];
  follow_up?: string[];
  valid?: boolean;
  attempts?: number;
  parts?: number;
  edited?: boolean;
  /** The transcript was corrected after these notes were written. */
  stale?: boolean;
}

/** A meeting type notes can be written for (standup, interview…). */
export interface MeetingType {
  id: string;
  label: string;
}

// claude.ai import
/** What importing an item would do: add it, update it, move it to its folder, or nothing. */
export type ItemStatus = "new" | "update" | "move" | "unchanged";

/** What a claude.ai export contains, item by item, before importing. */
export interface ImportPreview {
  preview_id: string;
  files: { name: string; kind: string; items: number }[];
  warnings: string[];
  account_name: string;
  projects: { uuid: string; name: string }[];
  memory: { key: string; text: string; section: string; origin: string; project: string; status: ItemStatus; replaces: string }[];
  people: { key: string; name: string; role: string; team: string; projects: string[]; relationships: string[];
            evidence: string[]; status: ItemStatus; changes: string[]; is_user: boolean }[];
  conversations: { uuid: string; title: string; created_at: string; updated_at: string; message_count: number;
                   project: string; status: ItemStatus }[];
  date_range: [string, string] | null;
  counts: { memory: number; people: number; conversations: number };
}

/** What has been imported from claude.ai so far. */
export interface ImportStatus {
  memory: number;
  conversations: number;
  people: number;
  last_import: Record<string, number | string> | null;
}

/** An imported fact in the older imported-memory list. */
export interface ImportedMemory {
  id: string;
  key: string;
  text: string;
  section: string;
  project: string;
  source: string;
  origin: string;
  imported_at: string;
  updated_at: string;
  edited: boolean;
}

// Search
/** Where search looks. */
export type SearchSource = "chats" | "transcripts" | "memory" | "connectors" | "web";

/** One search result with its source, link and snippet. */
export interface SearchResult {
  source: SearchSource;
  kind: string;
  id: string;
  title: string;
  snippet: string;
  date: string;
  link: string;
  score: number;
  method: string;
  meta: Record<string, unknown>;
}

/** POST /api/search: results, plus web-search status and notes (e.g. a source that was skipped). */
export interface SearchResponse {
  query: string;
  groups: Record<SearchSource, SearchResult[]>;
  web: { status: string; provider: string; label?: string; message: string };
  method: string;
  notes: string[];
  index: { state: string; done: number; total: number; model: string; error: string };
  total: number;
}

/** A web search provider and whether its API key is set (masked). */
export interface SearchProvider {
  id: string;
  label: string;
  needs_key: boolean;
  signup: string;
  note: string;
  has_key: boolean;
  key_hint: string;
}

/** GET /api/search/settings. */
export interface SearchSettings {
  settings: SearchPrefs;
  providers: SearchProvider[];
  key_storage: string;
  embedding: { model: string; installed: boolean; ollama_running: boolean; pull?: PullState };
  index: { state: string; done: number; total: number; model: string; error: string; finished_at?: string };
}

// Built-in skills
/** A built-in skill (meeting notes) with its templates. */
export interface BuiltinSkill {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  default_for: string;
  instructions: string;
  schema: Record<string, unknown>;
  templates: { name: string; type: string; label: string; customized: boolean }[];
  folder: string;
}

/** GET /api/health: Ollama, the cloud model, the transcriber and the search index. */
export interface Health {
  product: string;
  ollama: { running: boolean; version: string; url: string };
  cloud: { configured: boolean; model: string; providers?: number };
  transcriber: { ok?: boolean; error?: string; diarization?: boolean; whisper_model?: string; meetings_dir?: string };
}

// Events streamed by chat turns, summaries and speaker identification
export type StreamEvent =
  | { type: "start"; model: string; model_id: string; user?: Message }
  | { type: "delta"; text: string }
  | { type: "reset" }
  | { type: "status"; text: string }
  | { type: "notice"; text: string; kind?: string }
  | { type: "progress"; done: number; total: number; text: string }
  | { type: "memory"; added: string[] }
  | { type: "memory_used"; facts: string[] }
  | { type: "labels"; labels: Record<string, SpeakerLabel>; display_names: Record<string, string>; model: string }
  | { type: "confirm"; kind: "sensitive"; reasons: string[]; local_model?: string | null; model?: string;
      provider?: string; connectors?: string[]; stage?: "speaker_review"; question?: string }
  | { type: "confirm"; kind: "long"; notice: string }
  | { type: "choose_model"; kind: "summary"; text: string }
  | { type: "error"; kind: ErrorKind; message: string; error_kind?: string; provider?: string }
  | { type: "done"; message?: Message; session?: SessionSummary; [key: string]: unknown };

// --- Recording a call -------------------------------------------------------------------------------------

/** An app with on-screen windows that can be recorded; calls and remote desktop clients are flagged. */
export interface RecordApp {
  bundle_id: string;
  name: string;
  meeting: boolean;
  remote: boolean;
  display: number;
  windows: { title: string; width: number; height: number; display: number }[];
}

export interface RecordDisplay { id: number; name: string; width: number; height: number; main: boolean }
export interface RecordMic { id: string; name: string; default: boolean; bluetooth: boolean }
/** What to record. Without `id` (picker mode), macOS's picker chooses the app/window or display at Start; a picked
 * source comes back with `picked` (and the app's `name` when known). */
export type RecordSource = { kind: "app" | "display" | "window"; id?: string | number | null; name?: string; picked?: boolean }
  | { kind: "audio"; id?: null; picked?: boolean };

/** GET /api/meetings/record/sources */
export interface RecordSources {
  apps: RecordApp[];
  displays: RecordDisplay[];
  microphones: RecordMic[];
  /** No screen access for the recorder: macOS's own picker chooses the window or screen at Start. */
  picker?: boolean;
  last: { source?: RecordSource; mic?: string | null; live?: boolean };
  live_default: boolean;
}

export interface RecordWarning { kind: string; message: string; mic?: string | null }

/** GET /api/meetings/record/status */
export interface RecordStatus {
  state: "idle" | "recording" | "stopped" | "interrupted";
  id?: string;
  title?: string;
  source?: RecordSource;
  mic?: string | null;
  video?: boolean;
  live?: boolean;
  started_at?: string;
  elapsed?: number | null;
  levels?: { system: number; mic: number } | null;
  bytes?: number;
  warnings?: RecordWarning[];
  reason?: string | null;
  live_state?: { state: LiveState; processed_seconds: number; behind_seconds: number } | null;
}

export type LiveState = "starting" | "running" | "catching_up" | "paused_memory" | "done" | "error" | "final";

export interface LiveSegment { speaker: string; start: number; end: number; text: string }

/** GET /api/meetings/record/live */
export interface LiveDraft {
  id: string;
  title?: string;
  state: LiveState;
  error?: string | null;
  segments: LiveSegment[];
  total: number;
  processed_seconds?: number;
  behind_seconds?: number;
  transcript_id?: string | null;
}

/** POST /api/meetings/record/stop */
export interface RecordStopResult {
  recording: { id: string; path: string | null; seconds: number | null; complete: boolean; reason: string };
  job: Job | null;
}

// --- Meeting check ------------------------------------------------------------------------------------------

export type AlertType = "conflict" | "raise" | "pending" | "my_action";

/** Where an alert comes from: an earlier meeting's decision/action/risk/question or passage, memory, or My actions. */
export interface AlertSource {
  kind: "decision" | "action" | "risk" | "question" | "passage" | "memory" | "tracked";
  text: string;
  meeting?: string;
  date?: string;
  tid?: string;
  time?: string;
  owner?: string;
  due?: string;
}

export interface MeetingAlert {
  id: string;
  type: AlertType;
  text: string;
  line: string | null;
  seconds: number | null;
  quote: string;
  sources: AlertSource[];
  state: "new" | "dismissed" | "tracked";
  created: string;
  model?: string;
  /** The alerts file it lives in (rec-… during the call, t-… afterwards). */
  key: string;
}

/** A tracked action ("My actions"). */
export interface MyAction {
  id: string;
  text: string;
  type: AlertType;
  project: string | null;
  sources: AlertSource[];
  line: string | null;
  created: string;
  done: boolean;
}
