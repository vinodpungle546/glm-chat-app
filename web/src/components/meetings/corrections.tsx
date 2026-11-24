"use client";

/**
 * Correcting a meeting with other transcripts of it (transcriber/correct.py does the work, on this Mac, no model):
 *
 * - AddTranscriptDialog: the Voice Memos recording (uploaded, then transcribed by Whisper as a job), pasted text
 *   (Voice Memos, claude.ai, other) or a text file. Any combination, added at any time; nothing is needed.
 * - CorrectionsPanel (the Corrections tab): the sources with how well each matched, the changes by sentence (before →
 *   after, a speaker change) or one by one (revert or bring back), Undo all and Apply again, and a model's review.
 * - useCorrectionReview: a chosen model (local, or cloud after a confidentiality question) reviews each corrected
 *   line against the other transcripts; its fixes are validated by the API and can be reverted like the others.
 * - useCorrectionJobs: while a recording is being transcribed, polls and reloads the meeting when it's done.
 * - MarkedText: corrected words underlined in the transcript, with what they were.
 */
import * as Tabs from "@radix-ui/react-tabs";
import { AudioLines, Check, ClipboardPaste, Cloud, FileText, Loader2, RotateCcw, Sparkles, Trash2, Undo2, Upload } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { Badge, Button, Callout, Dialog, Label, Progress } from "@/components/ui";
import {
  api, type Correction, type CorrectionChange, type CorrectionSentence, type RefSource, stream, type Transcript,
  uploadInChunks,
} from "@/lib/api";
import { clsx, fmtTime } from "@/lib/format";

const SOURCE_LABELS: Record<RefSource, string> = { voice_memos: "Voice Memos", claude: "claude.ai", other: "Other" };
const TEXT_TYPES = ".txt,.md,.vtt,.srt,.docx,.pdf";
const AUDIO_TYPES = ".m4a,.qta,.mp3,.wav,.aac,.mp4,.mov";

/** "142 words corrected · 6 turns moved · 2 names suggested". */
export function correctionLine(c: Correction): string {
  const parts = [];
  if (c.stats.words) parts.push(`${c.stats.words} word${c.stats.words === 1 ? "" : "s"} corrected`);
  if (c.stats.turns) parts.push(`${c.stats.turns} turn${c.stats.turns === 1 ? "" : "s"} moved`);
  if (c.stats.speakers) parts.push(`${c.stats.speakers} line${c.stats.speakers === 1 ? "" : "s"} given to another speaker`);
  if (c.stats.sentences) parts.push(`${c.stats.sentences} sentence${c.stats.sentences === 1 ? "" : "s"} split or joined`);
  if (c.stats.model) parts.push(`${c.stats.model} fix${c.stats.model === 1 ? "" : "es"} by ${c.model?.label ?? "the model"}`);
  if (c.stats.names) parts.push(`${c.stats.names} name${c.stats.names === 1 ? "" : "s"} suggested`);
  return parts.join(" · ") || "No corrections needed";
}

/** A toast for a new correction, with Undo. */
function announce(transcriptId: string, c: Correction, reload: () => void) {
  const newest = c.refs[c.refs.length - 1];
  if (newest && newest.status === "ready" && newest.applied === false) {
    toast.warning("Not used", { description: `${newest.name} doesn't match this meeting, so nothing was changed.` });
    return;
  }
  toast.success("Transcript corrected", {
    description: correctionLine(c),
    action: c.stats.words || c.stats.turns || c.stats.speakers || c.stats.sentences ? {
      label: "Undo",
      onClick: async () => {
        await api.post(`/api/meetings/transcripts/${transcriptId}/corrections/undo`);
        reload();
      },
    } : undefined,
  });
}

/** Polls while a source recording is being transcribed; reloads the meeting (and says so) when it's done. */
export function useCorrectionJobs(transcript: Transcript, reload: () => void, onReady?: () => void) {
  const pending = (transcript.correction?.refs ?? []).some((r) => r.status === "transcribing");
  const [watching, setWatching] = useState(false);
  const active = pending || watching;
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(async () => {
      try {
        const data = await api.get<{ correction: Correction | null }>(`/api/meetings/transcripts/${transcript.id}/references`);
        const refs = data.correction?.refs ?? [];
        if (!refs.some((r) => r.status === "transcribing")) {
          setWatching(false);
          const failed = refs.find((r) => r.status === "failed");
          if (failed) toast.error("Couldn't transcribe the recording", { description: failed.error ?? undefined });
          else if (data.correction) {
            announce(transcript.id, data.correction, reload);
            onReady?.();
          }
          reload();
        }
      } catch {
        /* the transcriber may be busy: try again next tick */
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [active, transcript.id, reload, onReady]);
  return { watch: () => setWatching(true) };
}

/** "none" (the engine only) or a model id. */
export type CorrectionModel = string;

interface ReviewAsk { modelId: string; model: string; provider: string; sends: string[] }

/** A model's review of the corrections (SSE). A cloud model first asks (once per meeting and provider). */
export function useCorrectionReview(transcriptId: string, reload: () => void) {
  const [running, setRunning] = useState<{ model: string; status: string; done: number; total: number } | null>(null);
  const [ask, setAsk] = useState<ReviewAsk | null>(null);
  const start = useCallback(async (modelId: string, confirm?: "cloud") => {
    if (!modelId || modelId === "none") return;
    setRunning({ model: modelId, status: "Starting the review…", done: 0, total: 0 });
    try {
      await stream<Record<string, unknown>>(`/api/meetings/transcripts/${transcriptId}/correction-review`,
        { model_id: modelId, confirm }, (e) => {
          if (e.type === "status") setRunning((r) => r && { ...r, status: String(e.text) });
          else if (e.type === "progress") setRunning((r) => r && { ...r, done: Number(e.done), total: Number(e.total) });
          else if (e.type === "notice") toast(String(e.text));
          else if (e.type === "confirm") setAsk({ modelId, model: String(e.model), provider: String(e.provider), sends: (e.sends as string[]) ?? [] });
          else if (e.type === "error") toast.error("The model review didn't run", { description: String(e.message) });
          else if (e.type === "done") {
            const dropped = (e.dropped as string[] | undefined) ?? [];
            if (e.model) {
              toast.success(`${e.model} reviewed the transcript`, {
                description: `${Number(e.changes ?? 0)} sentence fix${Number(e.changes) === 1 ? "" : "es"}`
                  + (dropped.length ? ` · ${dropped.length} proposal${dropped.length === 1 ? "" : "s"} dropped (not supported by the other transcripts)` : ""),
              });
            }
            reload();
          }
        });
    } catch (err) {
      toast.error("The model review didn't run", { description: (err as Error).message });
    } finally {
      setRunning(null);
    }
  }, [transcriptId, reload]);
  const dialog = (
    <Dialog open={!!ask} onOpenChange={(o) => !o && setAsk(null)} title={ask ? `Send this meeting to ${ask.model}?` : ""}
            description="The model runs in the cloud, not on this Mac." footer={<>
      <Button variant="ghost" onClick={() => setAsk(null)} data-testid="review-cloud-cancel">Cancel</Button>
      <Button variant="primary" data-testid="review-cloud-send" onClick={() => { const a = ask!; setAsk(null); void start(a.modelId, "cloud"); }}>
        <Cloud className="h-4 w-4" /> Send to {ask?.provider}
      </Button></>}>
      {ask && (
        <div className="space-y-2 text-[13px]" data-testid="review-cloud-warning">
          <p>To review the corrections, {ask.provider} receives:</p>
          <ul className="list-disc pl-5">{ask.sends.map((x) => <li key={x}>{x}</li>)}</ul>
          <p className="text-muted">Meetings are often confidential. Use a local model (“on this Mac”) to keep everything here. You&apos;re asked once per meeting.</p>
        </div>
      )}
    </Dialog>
  );
  return { start, running, dialog };
}

/** The model that reviews the corrections: none (the engine only), a local model, or a cloud model (with a warning). */
export function CorrectionModelSelect({ value, onChange, id = "correction-model" }: {
  value: CorrectionModel; onChange: (v: CorrectionModel) => void; id?: string;
}) {
  const { models } = useApp();
  const ready = (models?.models ?? []).filter((m) => m.status === "ready");
  const local = ready.filter((m) => m.group !== "cloud");
  const cloud = ready.filter((m) => m.group === "cloud");
  const chosen = ready.find((m) => m.id === value);
  return (
    <div className="space-y-1" data-testid="correction-model">
      <Label htmlFor={id}>Then review the sentences with</Label>
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)} data-testid="correction-model-select"
              className="h-9 w-full rounded-lg border border-border-strong bg-surface px-3 text-sm">
        <option value="none">No model (fast: words, speakers and sentence ends only)</option>
        {local.length > 0 && <optgroup label="On this Mac">{local.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}</optgroup>}
        {cloud.length > 0 && <optgroup label="Cloud">{cloud.map((m) => <option key={m.id} value={m.id}>{m.label} · cloud</option>)}</optgroup>}
      </select>
      {chosen?.group === "cloud" ? (
        <Callout tone="warning" data-testid="correction-model-cloud">
          {chosen.label} runs in the cloud: this meeting&apos;s transcript, the other transcripts and the speakers&apos;
          names would be sent there. You&apos;ll be asked to confirm. Projects set to local never use it.
        </Callout>
      ) : chosen ? (
        <p className="text-xs text-muted">Runs on this Mac. It proposes fixes only where the other transcripts support them; you can revert each.</p>
      ) : null}
    </div>
  );
}

/** "Add another transcript": the Voice Memos recording, pasted text, or a text file. */
export function AddTranscriptDialog({ open, onOpenChange, transcriptId, onDone, onWatch, onReview }: {
  open: boolean; onOpenChange: (open: boolean) => void; transcriptId: string;
  onDone: () => void; onWatch: (model: CorrectionModel) => void; onReview: (model: CorrectionModel) => void;
}) {
  const [tab, setTab] = useState("text");
  const [model, setModel] = useState<CorrectionModel>("none");
  const [source, setSource] = useState<RefSource>("voice_memos");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const audioRef = useRef<HTMLInputElement | null>(null);
  const base = `/api/meetings/transcripts/${transcriptId}/references`;

  const finish = (c: Correction) => {
    announce(transcriptId, c, onDone);
    setText("");
    onOpenChange(false);
    onDone();
    const newest = c.refs[c.refs.length - 1];
    if (model !== "none" && !(newest && newest.applied === false)) onReview(model);
  };
  const sendText = async () => {
    setBusy(true);
    setError(null);
    try {
      finish(await api.post<Correction>(base, { source, text }));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const sendFile = async (file: File) => {
    setBusy(true);
    setError(null);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("source", source);
      finish(await api.upload<Correction>(base, form));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const sendAudio = async (file: File) => {
    setBusy(true);
    setError(null);
    setProgress(0);
    try {
      const path = await uploadInChunks(file, setProgress);
      await api.post(`${base}/audio`, { path, source: "voice_memos", name: file.name.replace(/\.[^.]+$/, "") });
      toast("Transcribing the recording", { description: "The meeting is corrected when it's done. You can keep working." });
      onWatch(model);
      onOpenChange(false);
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
      setProgress(null);
    }
  };
  const tabClass = "-mb-px border-b-2 border-transparent px-3 py-2 text-[13px] font-medium text-muted hover:text-fg data-[state=active]:border-accent data-[state=active]:text-fg";
  const sourcePicker = (
    <div>
      <Label htmlFor="ref-source">From</Label>
      <select id="ref-source" value={source} onChange={(e) => setSource(e.target.value as RefSource)} data-testid="ref-source"
              className="h-9 w-full rounded-lg border border-border-strong bg-surface px-3 text-sm">
        <option value="voice_memos">Voice Memos</option>
        <option value="claude">claude.ai</option>
        <option value="other">Other (Teams, Zoom, notes…)</option>
      </select>
    </div>
  );
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="Add another transcript" className="max-w-xl"
            description="Quorum lines it up with this meeting and corrects words, who said what and sentence ends, on this Mac. A model can then review whole sentences.">
      <Tabs.Root value={tab} onValueChange={setTab}>
        <Tabs.List className="mb-3 flex gap-1 border-b border-border" aria-label="Source">
          <Tabs.Trigger value="text" className={tabClass} data-testid="ref-tab-text"><ClipboardPaste className="mr-1 inline h-3.5 w-3.5" />Paste text</Tabs.Trigger>
          <Tabs.Trigger value="audio" className={tabClass} data-testid="ref-tab-audio"><AudioLines className="mr-1 inline h-3.5 w-3.5" />Voice Memos recording</Tabs.Trigger>
          <Tabs.Trigger value="file" className={tabClass} data-testid="ref-tab-file"><FileText className="mr-1 inline h-3.5 w-3.5" />Text file</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="text" className="space-y-3">
          {sourcePicker}
          <textarea value={text} onChange={(e) => setText(e.target.value)} rows={8} data-testid="ref-text"
                    placeholder="Paste the transcript: Voice Memos (Transcript → select all → copy), claude.ai, or any other."
                    className="w-full rounded-lg border border-border-strong bg-surface px-3 py-2 text-[13px] leading-relaxed" />
          <div className="flex justify-end">
            <Button variant="primary" onClick={sendText} loading={busy} disabled={!text.trim()} data-testid="ref-add-text">Correct the transcript</Button>
          </div>
        </Tabs.Content>
        <Tabs.Content value="audio" className="space-y-3">
          <p className="text-[13px] text-muted">Drag the memo from Voice Memos onto the box below, or use Share → Save to Files there and pick it here.
            Quorum transcribes it with the same Whisper model (a job, like a new meeting), then corrects this meeting.</p>
          <div onDragOver={(e) => e.preventDefault()} data-testid="ref-drop"
               onDrop={(e) => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) void sendAudio(f); }}
               className="flex flex-col items-center gap-2 rounded-xl border-2 border-dashed border-border px-4 py-6 text-center text-[13px] text-muted">
            <Upload className="h-5 w-5" />
            {progress !== null ? <span>Uploading… {Math.round(progress)}%</span> : <span>Drop the Voice Memos recording here</span>}
            <input ref={audioRef} type="file" accept={AUDIO_TYPES} className="hidden" data-testid="ref-audio-input"
                   onChange={(e) => { const f = e.target.files?.[0]; if (f) void sendAudio(f); e.target.value = ""; }} />
            <Button size="sm" variant="outline" onClick={() => audioRef.current?.click()} loading={busy}>Choose a file</Button>
          </div>
        </Tabs.Content>
        <Tabs.Content value="file" className="space-y-3">
          {sourcePicker}
          <input ref={fileRef} type="file" accept={TEXT_TYPES} className="hidden" data-testid="ref-file-input"
                 onChange={(e) => { const f = e.target.files?.[0]; if (f) void sendFile(f); e.target.value = ""; }} />
          <Button variant="outline" onClick={() => fileRef.current?.click()} loading={busy}>
            <FileText className="h-4 w-4" /> Choose a text file (.txt, .vtt, .srt, .docx, .pdf)
          </Button>
        </Tabs.Content>
      </Tabs.Root>
      <div className="mt-4 border-t border-border pt-3"><CorrectionModelSelect value={model} onChange={setModel} /></div>
      {error && <Callout tone="danger" className="mt-3">{error}</Callout>}
    </Dialog>
  );
}

/** The Corrections tab: sources, changes, Undo all. */
export function CorrectionsPanel({ transcript, onChange, onAdd, review }: {
  transcript: Transcript; onChange: () => void; onAdd: () => void;
  review?: { start: (model: CorrectionModel) => void; running: { model: string; status: string; done: number; total: number } | null };
}) {
  const c = transcript.correction;
  const [only, setOnly] = useState(false);
  const [view, setView] = useState<"sentences" | "changes">("sentences");
  const [model, setModel] = useState<CorrectionModel>("none");
  const [busy, setBusy] = useState<string | null>(null);
  const base = `/api/meetings/transcripts/${transcript.id}`;
  const act = useCallback(async (key: string, path: string, method: "post" | "del" = "post") => {
    setBusy(key);
    try {
      if (method === "del") await api.del(path);
      else await api.post(path);
      onChange();
    } catch (err) {
      toast.error("Couldn't change the corrections", { description: (err as Error).message });
    } finally {
      setBusy(null);
    }
  }, [onChange]);
  const changes = useMemo(() => (c?.changes ?? []).filter((ch) => !only || ch.name_or_number || ch.kind === "turn" || ch.kind === "speaker"), [c, only]);
  const byId = useMemo(() => new Map((c?.changes ?? []).map((ch) => [ch.id, ch])), [c]);
  const revertAll = async (sentence: CorrectionSentence) => {
    setBusy(`s${sentence.seg}`);
    try {
      for (const id of sentence.ids) await api.post(`${base}/corrections/${id}/revert`);
      onChange();
    } catch (err) {
      toast.error("Couldn't change the corrections", { description: (err as Error).message });
    } finally {
      setBusy(null);
    }
  };
  const stale = (c?.changes ?? []).filter((ch) => ch.stale).length;
  const anything = !!c && (c.stats.words > 0 || c.stats.turns > 0 || !!c.stats.speakers || !!c.stats.sentences || !!c.stats.model);

  const cloudFixes = (c?.changes ?? []).filter((ch) => ch.kind === "model" && ch.layer === "cloud_review");
  const cloudLayer = c?.layers?.cloud_review;
  const kept = new Set(c?.kept ?? []);
  if (!c || (!c.refs.length && !cloudLayer)) {
    return (
      <div className="space-y-3 text-[13px] text-muted" data-testid="corrections-empty">
        <p>Have another transcript of this meeting? The Voice Memos recording or its text, a claude.ai transcript, or a
          Teams or Zoom one: add any of them and Quorum corrects the words and who said what. None is needed.</p>
        <Button variant="outline" size="sm" onClick={onAdd} data-testid="corrections-add">Add another transcript</Button>
      </div>
    );
  }
  return (
    <div className="space-y-4" data-testid="corrections">
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <h3 className="text-[13px] font-semibold">Sources</h3>
          <Button size="sm" variant="ghost" onClick={onAdd}>Add</Button>
        </div>
        <ul className="space-y-1.5">
          {c.refs.map((r) => (
            <li key={r.id} className="flex items-start gap-2 rounded-lg border border-border px-2.5 py-2 text-[13px]" data-testid="correction-ref">
              {r.kind === "audio" ? <AudioLines className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted" /> : <FileText className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted" />}
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="truncate font-medium">{r.name}</span>
                  <Badge>{SOURCE_LABELS[r.source] ?? r.source}</Badge>
                </div>
                <div className="text-xs text-muted" data-testid="correction-ref-status">
                  {r.status === "transcribing" ? <span className="inline-flex items-center gap-1"><Loader2 className="h-3 w-3 animate-spin" /> Transcribing…</span>
                    : r.status === "failed" ? <span className="text-danger">Couldn&apos;t transcribe: {r.error}</span>
                    : r.applied === false ? <span className="text-warning">Doesn&apos;t match this meeting, not used</span>
                    : <>
                        {Math.round((r.coverage ?? 0) * 100)}% of words matched
                        {r.share !== undefined && r.share < 0.9 ? ` · covers ${Math.round(r.share * 100)}% of the meeting` : ""}
                        {r.offset !== null && r.offset !== undefined && Math.abs(r.offset) >= 1
                          ? ` · started ${Math.abs(Math.round(r.offset))} s ${r.offset < 0 ? "before" : "after"} the recording` : ""}
                      </>}
                </div>
              </div>
              <Button size="icon-sm" variant="ghost" aria-label={`Remove ${r.name}`} loading={busy === r.id}
                      onClick={() => act(r.id, `${base}/references/${r.id}`, "del")}><Trash2 className="h-3.5 w-3.5" /></Button>
            </li>
          ))}
        </ul>
      </div>

      {c.undone ? (
        <Callout tone="info" title="Corrections undone" action={
          <Button size="sm" onClick={() => act("apply", `${base}/corrections/apply`)} loading={busy === "apply"} data-testid="corrections-apply">Apply again</Button>}>
          The transcript is back to the original. The sources are kept.
        </Callout>
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-[13px] font-medium" data-testid="corrections-summary">{correctionLine(c)}</span>
          {anything && (
            <Button size="sm" variant="outline" onClick={() => act("undo", `${base}/corrections/undo`)} loading={busy === "undo"} data-testid="corrections-undo">
              <Undo2 className="h-3.5 w-3.5" /> Undo all
            </Button>
          )}
        </div>
      )}

      {cloudLayer && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-warning/30 bg-warning-soft px-2.5 py-2 text-xs" data-testid="cloud-spelling">
          <span>
            Spelling fixed by {cloudLayer.label} in the cloud review: {cloudFixes.filter((ch) => ch.seg !== null && !ch.reverted).length} line
            {cloudFixes.filter((ch) => ch.seg !== null && !ch.reverted).length === 1 ? "" : "s"}
            {cloudFixes.some((ch) => ch.stale) ? ` · ${cloudFixes.filter((ch) => ch.stale).length} no longer match their line` : ""}.
            Keep or revert each under Each change.
          </span>
          <Button size="sm" variant="outline" loading={busy === "cloud"} data-testid="cloud-spelling-revert-all"
                  onClick={() => act("cloud", `${base}/correction-review?source=cloud_review`, "del")}>
            <Undo2 className="h-3.5 w-3.5" /> Revert all
          </Button>
        </div>
      )}

      {!c.undone && review && c.refs.length > 0 && (
        <div className="space-y-2 rounded-lg border border-border p-2.5" data-testid="correction-review">
          <div className="flex items-center gap-1.5 text-[13px] font-semibold"><Sparkles className="h-3.5 w-3.5" /> Review with a model</div>
          {c.model && (
            <p className="text-xs text-muted" data-testid="correction-review-done">
              Reviewed by {c.model.label}: {c.stats.model ?? 0} fix{c.stats.model === 1 ? "" : "es"} applied
              {stale ? ` · ${stale} no longer match the lines (run the review again)` : ""}.{" "}
              <button type="button" className="text-accent hover:underline" data-testid="correction-review-remove"
                      onClick={() => act("model", `${base}/correction-review`, "del")}>Remove the model&apos;s fixes</button>
            </p>
          )}
          {review.running ? (
            <div data-testid="correction-review-running">
              <Progress value={review.running.total ? (review.running.done / review.running.total) * 100 : 5}
                        label={review.running.status} />
            </div>
          ) : (
            <div className="flex flex-wrap items-end gap-2">
              <div className="min-w-0 flex-1"><CorrectionModelSelect value={model} onChange={setModel} id="correction-model-panel" /></div>
              <Button size="sm" variant="outline" disabled={model === "none"} onClick={() => review.start(model)} data-testid="correction-review-run">
                Review now</Button>
            </div>
          )}
        </div>
      )}

      {(changes.length > 0 || only) && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex gap-1 text-xs" role="tablist">
              {(["sentences", "changes"] as const).map((v) => (
                <button key={v} type="button" role="tab" aria-selected={view === v} onClick={() => setView(v)} data-testid={`corrections-view-${v}`}
                        className={clsx("rounded-md px-2 py-1", view === v ? "bg-accent-soft text-fg" : "text-muted hover:text-fg")}>
                  {v === "sentences" ? "By sentence" : "Each change"}
                </button>
              ))}
            </div>
            {view === "changes" && (
              <label className="flex items-center gap-2 text-xs text-muted">
                <input type="checkbox" checked={only} onChange={(e) => setOnly(e.target.checked)} data-testid="corrections-only" />
                Only names, numbers and speakers
              </label>
            )}
          </div>
          {view === "sentences" ? (
            <ul className="max-h-[28rem] space-y-1.5 overflow-y-auto pr-1">
              {(c.sentences ?? []).map((x) => <SentenceRow key={x.seg} sentence={x} byId={byId} busy={busy === `s${x.seg}`}
                                                          onRevert={() => revertAll(x)} />)}
              {!(c.sentences ?? []).length && <li className="text-xs text-muted">Nothing is applied right now. Reverted changes are under Each change.</li>}
            </ul>
          ) : (
            <ul className="max-h-[28rem] space-y-1 overflow-y-auto pr-1">
              {changes.map((ch) => <ChangeRow key={ch.id} change={ch} busy={busy === ch.id} kept={kept.has(ch.id)}
                onKeep={() => act(ch.id, `${base}/corrections/${ch.id}/keep`)}
                onToggle={() => act(ch.id, `${base}/corrections/${ch.id}/${ch.reverted ? "restore" : "revert"}`)} />)}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

/** One line with its corrections: the speaker change, what it said (struck through) and what it says now. */
function SentenceRow({ sentence: x, byId, busy, onRevert }: {
  sentence: CorrectionSentence; byId: Map<string, CorrectionChange>; busy: boolean; onRevert: () => void;
}) {
  const seek = () => window.dispatchEvent(new CustomEvent("quorum:seek", { detail: { line: x.seg + 1 } }));
  const kinds = new Set(x.ids.map((id) => byId.get(id)?.kind));
  const reasons = x.ids.map((id) => byId.get(id)?.reason).filter(Boolean) as string[];
  return (
    <li className="flex items-start gap-2 rounded-md px-2 py-1.5 text-[13px] hover:bg-hover" data-testid="correction-sentence">
      <button type="button" onClick={seek} title="Show in the transcript"
              className="w-11 shrink-0 pt-0.5 text-left font-mono text-xs text-faint hover:text-accent">{fmtTime(x.t)}</button>
      <div className="min-w-0 flex-1 space-y-0.5">
        {x.speaker_to && (
          <div className="text-xs font-medium" data-testid="correction-sentence-speaker">
            Said by {x.speaker_to}{x.speaker_from ? <span className="text-muted">, not {x.speaker_from}</span> : null}
          </div>
        )}
        {x.before !== x.after && <div className="break-words text-muted line-through decoration-muted/60">{x.before}</div>}
        <div className="break-words">{x.after}</div>
        <div className="text-xs text-faint">
          {[...kinds].map((k) => KIND_LABEL[k ?? "word"]).filter(Boolean).join(" · ")}
          {x.sources.length > 0 && <> · from {x.sources.join(" + ")}</>}
          {reasons.length > 0 && <> · {reasons[0]}</>}
        </div>
      </div>
      <Button size="sm" variant="ghost" className="h-7 shrink-0 px-2 text-xs" onClick={onRevert} loading={busy} data-testid="correction-sentence-revert">
        <RotateCcw className="h-3 w-3" /> Revert</Button>
    </li>
  );
}

const KIND_LABEL: Record<string, string> = {
  word: "words", insert: "words added", delete: "words removed", turn: "turn moved", speaker: "speaker",
  punct: "sentence end", model: "model fix",
};

function ChangeRow({ change: ch, busy, onToggle, kept = false, onKeep }: {
  change: CorrectionChange; busy: boolean; onToggle: () => void; kept?: boolean; onKeep?: () => void;
}) {
  const seek = () => ch.seg !== null && window.dispatchEvent(new CustomEvent("quorum:seek", { detail: { line: ch.seg + 1 } }));
  const what = ch.kind === "turn" ? <>Turn moved: <i>{ch.from}</i> now said by {ch.to}</>
    : ch.kind === "speaker" ? <>Line said by <b>{ch.to}</b>, not {ch.from}{ch.text ? <>: <i>{ch.text}</i></> : null}</>
    : ch.kind === "punct" ? <>Sentence: <s className="text-muted">{ch.from}</s> → <b>{ch.to}</b></>
    : ch.kind === "model" ? <>{ch.speaker_to ? <>Said by <b>{ch.speaker_to}</b>{ch.to !== ch.from ? ": " : ""}</> : null}
        {ch.to !== ch.from && <><s className="text-muted">{ch.from}</s> → <b>{ch.to}</b></>}
        {ch.stale && <span className="text-warning"> (the line changed since: not applied)</span>}</>
    : ch.kind === "insert" ? <>Added <b>{ch.to}</b></>
    : ch.kind === "delete" ? <>Removed <s className="text-muted">{ch.from}</s></>
    : <><s className="text-muted">{ch.from}</s> → <b>{ch.to}</b></>;
  return (
    <li className={clsx("flex items-start gap-2 rounded-md px-2 py-1.5 text-[13px] hover:bg-hover", ch.reverted && "opacity-60")}
        data-testid="correction-change" data-reverted={ch.reverted || undefined}>
      <button type="button" onClick={seek} disabled={ch.seg === null} title="Show in the transcript"
              className="w-11 shrink-0 pt-0.5 text-left font-mono text-xs text-faint hover:text-accent disabled:hover:text-faint">{fmtTime(ch.t)}</button>
      <div className="min-w-0 flex-1">
        <div className="break-words">{what}</div>
        {ch.sources.length > 0 && <div className="text-xs text-faint">from {ch.sources.join(" + ")}{ch.reason ? ` · ${ch.reason}` : ""}</div>}
      </div>
      {ch.kind === "model" && !ch.stale && !ch.reverted && (kept
        ? <Badge tone="success"><span data-testid="correction-kept">kept</span></Badge>
        : onKeep && <Button size="sm" variant="ghost" className="h-7 shrink-0 px-2 text-xs" onClick={onKeep} disabled={busy}
                            data-testid="correction-keep"><Check className="h-3 w-3" /> Keep</Button>)}
      {!ch.stale && (
        <Button size="sm" variant="ghost" className="h-7 shrink-0 px-2 text-xs" onClick={onToggle} loading={busy} data-testid="correction-toggle">
          {ch.reverted ? "Bring back" : <><RotateCcw className="h-3 w-3" /> Revert</>}
        </Button>
      )}
    </li>
  );
}

/** A transcript line with its corrected words underlined (hover: what it was, and from which source). */
export function MarkedText({ text, spans, changes }: { text: string; spans?: [number, number, string][]; changes: Map<string, CorrectionChange> }) {
  if (!spans?.length) return <>{text}</>;
  const out: React.ReactNode[] = [];
  let pos = 0;
  spans.forEach(([start, end, cid], i) => {
    if (start < pos || end > text.length) return;
    out.push(text.slice(pos, start));
    const ch = changes.get(cid);
    out.push(
      <span key={i} data-testid="corrected-word" className="underline decoration-accent/60 decoration-dotted underline-offset-4"
            title={ch ? `was “${ch.from}”${ch.sources.length ? ` · from ${ch.sources.join(" + ")}` : ""}` : "corrected"}>
        {text.slice(start, end)}
      </span>);
    pos = end;
  });
  out.push(text.slice(pos));
  return <>{out}</>;
}
