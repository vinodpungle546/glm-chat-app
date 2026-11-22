"use client";

/**
 * The 'New transcription' box on the Meetings page: record a call, upload a recording, pick one from the watch
 * folder, or paste conversation text. Before Start it shows the time estimate and the models this meeting will use.
 */
import * as Tabs from "@radix-ui/react-tabs";
import { Circle, FileText, FolderOpen, Mic, RefreshCw, Upload, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { FramesNote, ModelSelect } from "@/components/meetings/model-select";
import { RecordTab } from "@/components/meetings/recording";
import { Button, Callout, Input, Label, Progress, Spinner, Textarea } from "@/components/ui";
import { api, type Estimate, type Job, type Recording, type TranscriptMeta } from "@/lib/api";
import { clsx, CONVERSATION_EXTENSIONS, extension, fmtBytes, fmtDuration, MEDIA_EXTENSIONS, shortDate } from "@/lib/format";

const SPEAKER_CHOICES = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 25, 30];

/**
 * What Start sends with the recording: speaker count, this meeting's models, and whether to identify
 * speakers and summarise afterwards.
 */
export interface StartOptions {
  speakers: number | null;
  speaker_model: string;
  frames_model: string;
  summary_model: string;
  after: boolean;
}

/** Called when the user presses Start for a recording. */
type OnStart = (path: string, options: StartOptions) => void;

/** A time range for estimates, e.g. '2 min–4 min' ('' when unknown). */
function range(low: number | null | undefined, high: number | null | undefined) {
  return low == null || high == null ? "" : `${fmtDuration(low)}–${fmtDuration(high)}`;
}

/** Estimate + speaker count + models + Start for one recording. */
function StartPanel({ path, onStart, busy }: { path: string; onStart: OnStart; busy: boolean }) {
  const { prefs, selectedModel } = useApp();
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [speakers, setSpeakers] = useState(0);
  const [speakerModel, setSpeakerModel] = useState(prefs?.speaker_model ?? "");
  const [framesModel, setFramesModel] = useState(prefs?.vision_model ?? "");
  const [summaryModel, setSummaryModel] = useState(prefs?.summary_model ?? "");
  const [after, setAfter] = useState(false);
  useEffect(() => { setEstimate(null); setError(null); }, [path]);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      api.post<Estimate>("/api/meetings/estimate", {
        path, speakers: speakers || null, speaker_model: speakerModel, frames_model: framesModel,
        summary_model: summaryModel, model_id: selectedModel?.id ?? "",
      }).then(setEstimate).catch((err) => setError((err as Error).message));
    }, 250);
    return () => window.clearTimeout(timer);
  }, [path, speakers, speakerModel, framesModel, summaryModel, selectedModel?.id]);
  if (error) return <Callout tone="danger" title="Can't read this recording">{error}</Callout>;
  if (!estimate) return <div className="flex items-center gap-2 text-[13px] text-muted"><Spinner /> Checking the recording…</div>;
  if (!estimate.has_audio) {
    return (
      <Callout tone="danger" title="This recording has no sound" data-testid="no-audio">
        {estimate.no_audio_message} Record again with ⇧⌘5 → Options → Microphone set to your microphone.
      </Callout>
    );
  }
  const use = estimate.models?.use;
  const follow = estimate.followups ?? {};
  const screen = estimate.has_video && estimate.models?.screen_enabled && estimate.screen_estimate;
  return (
    <div className="space-y-3">
      <p className="text-[13px] text-muted">
        <span className="font-medium text-fg">{fmtDuration(estimate.duration)}</span> recording · transcription takes about{" "}
        <span className="font-medium text-fg" data-testid="estimate-transcription">{fmtDuration(estimate.estimate_low)}–{fmtDuration(estimate.estimate_high)}</span>
        {estimate.queued_ahead ? ` · ${estimate.queued_ahead} job(s) ahead` : ""} · offline on this Mac
      </p>
      {!estimate.disk_ok && <Callout tone="danger">Not enough free disk space ({fmtBytes(estimate.free_disk_bytes)} free).</Callout>}
      <div className="space-y-3 rounded-xl border border-border p-3" data-testid="start-models">
        <p className="text-xs text-muted" data-testid="fixed-models">
          Speech-to-text: <span className="font-medium text-fg">Whisper large-v3-turbo</span> · speaker separation:{" "}
          <span className="font-medium text-fg">pyannote 3.1</span> (fixed, on this Mac)
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          <ModelSelect kind="speaker" id="start-speaker-model" label="Speaker identification" defaultLabel="Recommended"
            value={speakerModel} onChange={setSpeakerModel} />
          {estimate.has_video && (
            <ModelSelect kind="frames" id="start-frames-model" label="Names from the video" defaultLabel="Recommended"
              value={framesModel} onChange={setFramesModel} disabled={!estimate.models?.screen_enabled} />
          )}
          <ModelSelect kind="summary" id="start-summary-model" label="Summary" defaultLabel="Ask me when it's written"
            value={summaryModel} onChange={setSummaryModel} />
        </div>
        <FramesNote speakerModel={speakerModel} />
        {estimate.models?.fallbacks.map((f) => (
          <p key={f.kind} className="text-xs text-warning" data-testid="model-fallback">
            {f.wanted} isn&apos;t installed: {f.using} will be used unless you download it first.
          </p>
        ))}
        <ul className="space-y-0.5 text-xs text-muted" data-testid="estimate-steps">
          {screen && <li>Names from the video: about {range(estimate.screen_estimate?.low, estimate.screen_estimate?.high)}
            {use?.frames_model ? ` (unclear frames with ${use.frames_model})` : ""}</li>}
          {follow.speakers && <li data-testid="estimate-speakers">Identifying speakers with {follow.speakers.model}: about {range(follow.speakers.low, follow.speakers.high)}</li>}
          {follow.summary && (follow.summary.ask
            ? <li data-testid="estimate-summary">Summary: you&apos;ll be asked which model writes it</li>
            : <li data-testid="estimate-summary">Summary with {follow.summary.model}:{" "}
              {follow.summary.local ? `about ${range(follow.summary.low, follow.summary.high)}` : "cloud model, asks before the transcript is sent"}</li>)}
        </ul>
        <label className="flex items-start gap-2 text-[13px]">
          <input type="checkbox" className="mt-0.5" checked={after} onChange={(e) => setAfter(e.target.checked)} data-testid="start-after" />
          <span>Identify speakers and write the summary when transcription finishes
            <span className="block text-xs text-muted">Runs on this Mac after the transcriber has freed its memory, even if you close this page.
              {follow.summary?.ask ? " No summary model chosen: the speakers are identified, then the summary waits for you to pick a model."
                : follow.summary && !follow.summary.local ? " The cloud summary still waits for your OK." : ""}</span></span>
        </label>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <Label htmlFor="speakers">Speakers</Label>
          <select id="speakers" value={speakers} disabled={!estimate.diarization} onChange={(e) => setSpeakers(Number(e.target.value))}
                  className="h-9 rounded-lg border border-border-strong bg-surface px-3 text-sm">
            {SPEAKER_CHOICES.map((n) => <option key={n} value={n}>{n === 0 ? "Detect automatically" : `${n} ${n === 1 ? "person" : "people"}`}</option>)}
          </select>
        </div>
        <Button variant="primary" data-testid="start-transcription" disabled={!estimate.disk_ok || busy}
                onClick={() => onStart(path, { speakers: speakers || null, speaker_model: speakerModel, frames_model: framesModel,
                                               summary_model: summaryModel, after })}>
          <Mic className="h-4 w-4" /> Start transcription
        </Button>
      </div>
    </div>
  );
}

/**
 * Drag-and-drop upload. Large files are sent in chunks (an interrupted upload resumes), then the
 * StartPanel appears.
 */
function UploadTab({ onStart, busy }: { onStart: OnStart; busy: boolean }) {
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState(0);
  const [uploaded, setUploaded] = useState<{ path: string; name: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const upload = async (f: File) => {
    const ext = extension(f.name);
    if (!MEDIA_EXTENSIONS.includes(ext)) {
      setError(`Unsupported file type “.${ext || "?"}”. Use a video or audio recording: ${MEDIA_EXTENSIONS.join(", ")}.`);
      toast.error("Unsupported file type", { description: f.name });
      return;
    }
    setError(null);
    setUploaded(null);
    setFile(f);
    setProgress(0);
    const controller = new AbortController();
    abortRef.current = controller;
    setUploading(true);
    try {
      const init = await api.post<{ upload_id: string; chunk_size: number; received: number[] }>(
        "/api/meetings/uploads", { name: f.name, size: f.size });
      const total = Math.max(1, Math.ceil(f.size / init.chunk_size));
      const have = new Set(init.received);
      for (let n = 0; n < total; n++) {
        if (controller.signal.aborted) throw new DOMException("aborted", "AbortError");
        if (!have.has(n)) {
          await api.putBytes(`/api/meetings/uploads/${init.upload_id}/chunks/${n}`,
            f.slice(n * init.chunk_size, (n + 1) * init.chunk_size), controller.signal);
        }
        setProgress(((n + 1) / total) * 100);
      }
      const done = await api.post<{ path: string; name: string }>(`/api/meetings/uploads/${init.upload_id}/complete`);
      setUploaded(done);
      toast.success("Upload finished", { description: "Choose the speakers and start the transcription." });
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        setFile(null);
        toast("Upload cancelled", { description: "Uploading the same file again resumes where it stopped." });
      } else {
        setError((err as Error).message);
      }
    } finally {
      abortRef.current = null;
      setUploading(false);
    }
  };

  return (
    <div className="space-y-4">
      <div
        data-testid="dropzone"
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); const f = e.dataTransfer.files[0]; if (f) upload(f); }}
        onClick={() => !uploading && inputRef.current?.click()}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && inputRef.current?.click()}
        role="button" tabIndex={0} aria-label="Choose a recording to upload"
        className={clsx("flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-8 text-center transition-colors",
          dragging ? "border-accent bg-accent-soft" : "border-border-strong hover:bg-hover/60")}
      >
        <Upload className="mb-2 h-6 w-6 text-muted" />
        <div className="text-sm font-medium">Drop a meeting recording here, or click to choose</div>
        <div className="mt-1 text-xs text-muted">Video or audio of any size — {MEDIA_EXTENSIONS.slice(0, 7).join(", ")}… Mac screen recordings (.mov) work.</div>
        <input ref={inputRef} type="file" hidden data-testid="upload-input" accept={MEDIA_EXTENSIONS.map((e) => `.${e}`).join(",")}
               onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = ""; }} />
      </div>
      {error && <Callout tone="danger" data-testid="upload-error">{error}</Callout>}
      {file && !uploaded && !error && (
        <div className="space-y-1.5" data-testid="upload-progress">
          <div className="flex items-center justify-between text-[13px]">
            <span className="truncate font-medium">{file.name}</span>
            <span className="flex items-center gap-2 tabular-nums text-muted">
              {fmtBytes(file.size * progress / 100)} / {fmtBytes(file.size)}
              <Button size="icon-sm" variant="ghost" aria-label="Cancel upload" onClick={() => abortRef.current?.abort()}><X className="h-3.5 w-3.5" /></Button>
            </span>
          </div>
          <Progress value={progress} label="Upload progress" />
        </div>
      )}
      {uploaded && (
        <div className="space-y-3 rounded-xl border border-border p-4">
          <div className="text-[13px]"><span className="text-muted">Uploaded:</span> <span className="font-medium">{uploaded.name}</span></div>
          <StartPanel path={uploaded.path} onStart={onStart} busy={busy} />
        </div>
      )}
    </div>
  );
}

/** Recordings in the watch folder; pick one to see its StartPanel. */
function FolderTab({ onStart, busy }: { onStart: OnStart; busy: boolean }) {
  const { openSettings } = useApp();
  const [recordings, setRecordings] = useState<Recording[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [folder, setFolder] = useState("");
  const load = useCallback(async () => {
    try {
      const [list, settings] = await Promise.all([
        api.get<Recording[]>("/api/meetings/recordings"),
        api.get<{ meetings_dir: string }>("/api/meetings/settings"),
      ]);
      setRecordings(list);
      setFolder(settings.meetings_dir);
      setError(null);
      setSelected((s) => (s && list.some((r) => r.path === s) ? s : list.find((r) => !r.transcript_id)?.path ?? list[0]?.path ?? ""));
    } catch (err) {
      setError((err as Error).message);
      setRecordings([]);
    }
  }, []);
  useEffect(() => { load(); }, [load]);
  if (error) return <Callout tone="danger" title="The transcriber isn't reachable">{error}</Callout>;
  if (!recordings) return <div className="skeleton h-24" />;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2 text-[13px] text-muted">
        <span className="flex items-center gap-1.5"><FolderOpen className="h-3.5 w-3.5" /> <span className="font-mono text-xs">{folder}</span></span>
        <span className="flex gap-1">
          <Button size="sm" variant="ghost" onClick={() => openSettings("general")}>Change folder</Button>
          <Button size="sm" variant="ghost" onClick={load} aria-label="Refresh recordings"><RefreshCw className="h-3.5 w-3.5" /></Button>
        </span>
      </div>
      {recordings.length === 0 ? (
        <p className="text-[13px] text-muted">No recordings in this folder yet. Save or copy meeting recordings there and press refresh.</p>
      ) : (
        <ul className="max-h-60 space-y-1 overflow-y-auto" role="listbox" aria-label="Recordings" data-testid="recordings">
          {recordings.map((r) => (
            <li key={r.path}>
              <button role="option" aria-selected={selected === r.path} onClick={() => setSelected(r.path)}
                className={clsx("flex w-full items-center justify-between gap-3 rounded-lg border px-3 py-2 text-left text-[13px]",
                  selected === r.path ? "border-accent/50 bg-accent-soft" : "border-border hover:bg-hover")}>
                <span className="min-w-0"><span className="block truncate font-medium">{r.name}</span>
                  <span className="text-xs text-muted">{fmtBytes(r.size)} · {shortDate(r.modified)}{r.source === "upload" ? " · uploaded" : ""}</span></span>
                {r.transcript_id && <span className="shrink-0 text-xs text-success">✓ transcribed</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
      {selected && <StartPanel path={selected} onStart={onStart} busy={busy} />}
    </div>
  );
}

/** Paste a conversation or choose an export file (VTT, SRT, DOCX, PDF…) to turn it into a transcript. */
function TextTab({ onImported }: { onImported: (t: TranscriptMeta) => void }) {
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setError(null);
    if (file && !CONVERSATION_EXTENSIONS.includes(extension(file.name))) {
      setError(`Unsupported file type “.${extension(file.name)}”. Use ${CONVERSATION_EXTENSIONS.map((e) => "." + e).join(", ")}.`);
      return;
    }
    if (!file && !text.trim()) {
      setError("Paste the conversation or choose a file first.");
      return;
    }
    setBusy(true);
    try {
      let result: TranscriptMeta;
      if (file) {
        const form = new FormData();
        form.append("file", file);
        form.append("title", title);
        result = await api.upload<TranscriptMeta>("/api/meetings/import", form);
      } else {
        result = await api.post<TranscriptMeta>("/api/meetings/import", { title, text });
      }
      setText("");
      setTitle("");
      setFile(null);
      onImported(result);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-3">
      <p className="text-[13px] text-muted">Teams/Zoom/Meet exports (.vtt, .srt, .docx), chat logs or notes like “Priya: …” become a transcript with a label per speaker.</p>
      <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
        <Input placeholder="Title (optional)" aria-label="Title" value={title} onChange={(e) => setTitle(e.target.value)} />
        <label className="inline-flex h-9 cursor-pointer items-center gap-2 rounded-lg border border-border px-3 text-sm hover:bg-hover">
          <FileText className="h-4 w-4" /> {file ? file.name : "Choose file"}
          <input type="file" hidden data-testid="conversation-file" accept={CONVERSATION_EXTENSIONS.map((e) => `.${e}`).join(",")}
                 onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
      </div>
      <Textarea rows={7} value={text} aria-label="Conversation text" data-testid="conversation-text" onChange={(e) => setText(e.target.value)}
                placeholder={"[00:00:05] Priya: Revenue grew 12%.\nRaj: Marketing is over plan.\n…"} />
      {error && <Callout tone="danger">{error}</Callout>}
      <Button variant="primary" onClick={submit} loading={busy} data-testid="import-text"><Mic className="h-4 w-4" /> Start transcription</Button>
    </div>
  );
}

/** The three source tabs; starts the job through the API with the chosen options. */
export function NewMeeting({ onStarted, onImported }: { onStarted: (job: Job) => void; onImported: (t: TranscriptMeta) => void }) {
  const { selectedModel, recording } = useApp();
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState("upload");
  const recordingNow = !!recording && recording.state !== "idle";
  useEffect(() => { if (recordingNow) setTab("record"); }, [recordingNow]);
  const start = async (path: string, options: StartOptions) => {
    setBusy(true);
    try {
      onStarted(await api.post<Job>("/api/meetings/jobs", { path, ...options, model_id: selectedModel?.id ?? "" }));
    } catch (err) {
      toast.error("Couldn't start the transcription", { description: (err as Error).message });
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="rounded-2xl border border-border bg-surface shadow-card" aria-label="New transcription">
      <Tabs.Root value={tab} onValueChange={setTab}>
        <Tabs.List className="flex gap-1 border-b border-border px-3 pt-2" aria-label="Source">
          {[
            ["record", "Record a call", <Circle key="r" className={recordingNow ? "h-3.5 w-3.5 fill-danger text-danger" : "h-3.5 w-3.5"} />],
            ["upload", "Upload recording", <Upload key="u" className="h-3.5 w-3.5" />],
            ["folder", "Watch folder", <FolderOpen key="f" className="h-3.5 w-3.5" />],
            ["text", "Conversation text", <FileText key="t" className="h-3.5 w-3.5" />],
          ].map(([value, label, icon]) => (
            <Tabs.Trigger key={value as string} value={value as string} data-testid={`source-${value}`}
              className="-mb-px flex items-center gap-1.5 border-b-2 border-transparent px-3 py-2 text-[13px] font-medium text-muted hover:text-fg data-[state=active]:border-accent data-[state=active]:text-fg">
              {icon} {label}
            </Tabs.Trigger>
          ))}
        </Tabs.List>
        <div className="p-4 sm:p-5">
          <Tabs.Content value="record"><RecordTab onStarted={onStarted} /></Tabs.Content>
          <Tabs.Content value="upload"><UploadTab onStart={start} busy={busy} /></Tabs.Content>
          <Tabs.Content value="folder"><FolderTab onStart={start} busy={busy} /></Tabs.Content>
          <Tabs.Content value="text"><TextTab onImported={onImported} /></Tabs.Content>
        </div>
      </Tabs.Root>
    </section>
  );
}
