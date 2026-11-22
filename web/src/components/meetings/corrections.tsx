"use client";

/**
 * Correcting a meeting with other transcripts of it (transcriber/correct.py does the work, on this Mac, no model):
 *
 * - AddTranscriptDialog: the Voice Memos recording (uploaded, then transcribed by Whisper as a job), pasted text
 *   (Voice Memos, claude.ai, other) or a text file. Any combination, added at any time; nothing is needed.
 * - CorrectionsPanel (the Corrections tab): the sources with how well each matched, the changes (revert or bring
 *   back one), Undo all and Apply again.
 * - useCorrectionJobs: while a recording is being transcribed, polls and reloads the meeting when it's done.
 * - MarkedText: corrected words underlined in the transcript, with what they were.
 */
import * as Tabs from "@radix-ui/react-tabs";
import { AudioLines, ClipboardPaste, FileText, Loader2, RotateCcw, Trash2, Undo2, Upload } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { Badge, Button, Callout, Dialog, Label } from "@/components/ui";
import { api, type Correction, type CorrectionChange, type RefSource, type Transcript } from "@/lib/api";
import { clsx, fmtTime } from "@/lib/format";

const SOURCE_LABELS: Record<RefSource, string> = { voice_memos: "Voice Memos", claude: "claude.ai", other: "Other" };
const TEXT_TYPES = ".txt,.md,.vtt,.srt,.docx,.pdf";
const AUDIO_TYPES = ".m4a,.qta,.mp3,.wav,.aac,.mp4,.mov";

/** "142 words corrected · 6 turns moved · 2 names suggested". */
export function correctionLine(c: Correction): string {
  const parts = [];
  if (c.stats.words) parts.push(`${c.stats.words} word${c.stats.words === 1 ? "" : "s"} corrected`);
  if (c.stats.turns) parts.push(`${c.stats.turns} turn${c.stats.turns === 1 ? "" : "s"} moved`);
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
    action: c.stats.words || c.stats.turns ? {
      label: "Undo",
      onClick: async () => {
        await api.post(`/api/meetings/transcripts/${transcriptId}/corrections/undo`);
        reload();
      },
    } : undefined,
  });
}

/** Polls while a source recording is being transcribed; reloads the meeting (and says so) when it's done. */
export function useCorrectionJobs(transcript: Transcript, reload: () => void) {
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
          else if (data.correction) announce(transcript.id, data.correction, reload);
          reload();
        }
      } catch {
        /* the transcriber may be busy: try again next tick */
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [active, transcript.id, reload]);
  return { watch: () => setWatching(true) };
}

/** Upload a file in chunks (the same path as a new meeting's recording); returns its path on this Mac. */
async function upload(file: File, onProgress: (pct: number) => void): Promise<string> {
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

/** "Add another transcript": the Voice Memos recording, pasted text, or a text file. */
export function AddTranscriptDialog({ open, onOpenChange, transcriptId, onDone, onWatch }: {
  open: boolean; onOpenChange: (open: boolean) => void; transcriptId: string;
  onDone: () => void; onWatch: () => void;
}) {
  const [tab, setTab] = useState("text");
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
      const path = await upload(file, setProgress);
      await api.post(`${base}/audio`, { path, source: "voice_memos", name: file.name.replace(/\.[^.]+$/, "") });
      toast("Transcribing the recording", { description: "The meeting is corrected when it's done. You can keep working." });
      onWatch();
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
            description="Quorum lines it up with this meeting and corrects words and speakers. Everything stays on this Mac; no model is used.">
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
      {error && <Callout tone="danger" className="mt-3">{error}</Callout>}
    </Dialog>
  );
}

/** The Corrections tab: sources, changes, Undo all. */
export function CorrectionsPanel({ transcript, onChange, onAdd }: {
  transcript: Transcript; onChange: () => void; onAdd: () => void;
}) {
  const c = transcript.correction;
  const [only, setOnly] = useState(false);
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
  const changes = useMemo(() => (c?.changes ?? []).filter((ch) => !only || ch.name_or_number || ch.kind === "turn"), [c, only]);

  if (!c || !c.refs.length) {
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
          {(c.stats.words > 0 || c.stats.turns > 0) && (
            <Button size="sm" variant="outline" onClick={() => act("undo", `${base}/corrections/undo`)} loading={busy === "undo"} data-testid="corrections-undo">
              <Undo2 className="h-3.5 w-3.5" /> Undo all
            </Button>
          )}
        </div>
      )}

      {changes.length > 0 || only ? (
        <div className="space-y-2">
          <label className="flex items-center gap-2 text-xs text-muted">
            <input type="checkbox" checked={only} onChange={(e) => setOnly(e.target.checked)} data-testid="corrections-only" />
            Only names, numbers and turns
          </label>
          <ul className="max-h-[28rem] space-y-1 overflow-y-auto pr-1">
            {changes.map((ch) => <ChangeRow key={ch.id} change={ch} busy={busy === ch.id}
              onToggle={() => act(ch.id, `${base}/corrections/${ch.id}/${ch.reverted ? "restore" : "revert"}`)} />)}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function ChangeRow({ change: ch, busy, onToggle }: { change: CorrectionChange; busy: boolean; onToggle: () => void }) {
  const seek = () => ch.seg !== null && window.dispatchEvent(new CustomEvent("quorum:seek", { detail: { line: ch.seg + 1 } }));
  const what = ch.kind === "turn" ? <>Turn moved: <i>{ch.from}</i> now said by {ch.to}</>
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
        {ch.sources.length > 0 && <div className="text-xs text-faint">from {ch.sources.join(" + ")}</div>}
      </div>
      <Button size="sm" variant="ghost" className="h-7 shrink-0 px-2 text-xs" onClick={onToggle} loading={busy} data-testid="correction-toggle">
        {ch.reverted ? "Bring back" : <><RotateCcw className="h-3 w-3" /> Revert</>}
      </Button>
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
