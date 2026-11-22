"use client";

/**
 * The Speakers panel of a meeting: identify speakers (on this Mac, optionally reviewed by a cloud model),
 * see why each name was chosen and how sure it is, correct names, read names from the video, and keep or
 * revert cloud changes.
 */
import { AudioLines, Check, ChevronDown, Cloud, Mic, MonitorPlay, Pencil, ShieldCheck, Sparkles, Split, Undo2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { ModelSelect } from "@/components/meetings/model-select";
import { Badge, Button, Callout, Dialog, Input, Progress, Spinner, Tooltip } from "@/components/ui";
import { api, stream, type SpeakerLabel, type SpeakerReview, type StreamEvent, type Transcript } from "@/lib/api";
import { clsx, fmtDuration, initials, speakerColor } from "@/lib/format";

/** 'High / Medium / Low · 82%' (or 'Confirmed' for names the user set). */
export function ConfidenceBadge({ label }: { label?: SpeakerLabel }) {
  if (!label || (!label.name && label.level === "low")) return <Badge>Unknown</Badge>;
  if (label.level === "confirmed") return <Badge tone="success"><ShieldCheck className="h-3 w-3" /> Confirmed</Badge>;
  if (label.source === "microphone") {
    return <Badge tone="success" title="Recorded on its own track by Quorum"><Mic className="h-3 w-3" />
      <span data-testid="confirmed-mic">Confirmed: your microphone</span></Badge>;
  }
  const tone = label.level === "high" ? "success" : label.level === "medium" ? "warning" : "danger";
  const word = label.level === "high" ? "High" : label.level === "medium" ? "Medium" : "Low";
  return (
    <Badge tone={tone} title={`Confidence ${Math.round(label.confidence * 100)}%`}>
      <span data-testid="confidence">{word} · {Math.round(label.confidence * 100)}%</span>
    </Badge>
  );
}

/** Which evidence the one-line "why" shows: the cloud reason for cloud changes, then names on
 * screen, then voice and memory matches. */
function whyRank(text: string, label: SpeakerLabel): number {
  if (label.source === "cloud" && label.cloud && text.startsWith(`${label.cloud.model}:`)) return 5;
  if (text.startsWith("name on the active tile") || text.startsWith("name under the main video")) return 4;
  if (text.startsWith("name read by the local vision model")) return 3;
  if (text.startsWith("voice") || text.startsWith("Matched to")) return 2;
  return 0;
}

/**
 * What the cloud review changed: each change with its reason and cited lines, and Keep / Revert / Revert
 * all.
 */
function ReviewSummary({ transcript, review, onChange }: {
  transcript: Transcript; review: SpeakerReview; onChange: (t: Partial<Transcript>) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const pending = review.changes.filter((c) => c.state === "pending");
  const act = async (action: "keep" | "revert" | "revert-all", speaker = "") => {
    setBusy(`${action}:${speaker}`);
    try {
      const res = await api.post<{ labels: Transcript["labels"]; display_names: Transcript["display_names"]; review: SpeakerReview }>(
        `/api/meetings/transcripts/${transcript.id}/speakers/review/${action}`, { speaker });
      onChange(res);
      toast.success(action === "keep" ? "Kept" : action === "revert" ? "Reverted to the local name" : "All cloud changes reverted");
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setBusy(null);
    }
  };
  if (review.status === "failed") {
    return <Callout tone="warning" data-testid="review-summary">The cloud review by {review.model} didn&apos;t run ({review.error}). The local names are kept.</Callout>;
  }
  if (!review.changes.length) {
    return <Callout tone="success" data-testid="review-summary">{review.model} agreed with all local names.</Callout>;
  }
  const seek = (line: number) => window.dispatchEvent(new CustomEvent("quorum:seek", { detail: { line } }));
  const pct = (v: number) => `${Math.round(v * 100)}%`;
  return (
    <div className="space-y-2 rounded-xl border border-warning/30 bg-warning-soft p-3" data-testid="review-summary">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-sm font-medium"><Cloud className="h-4 w-4" /> Changed by {review.model}</div>
        {pending.length > 1 && <Button size="sm" variant="secondary" onClick={() => act("revert-all")} loading={busy === "revert-all:"}><Undo2 className="h-3.5 w-3.5" /> Revert all</Button>}
      </div>
      <ul className="space-y-2">
        {review.changes.map((c) => (
          <li key={c.speaker} className="rounded-lg bg-surface p-2.5 text-[13px]" data-testid="review-change" data-speaker={c.speaker}>
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-muted">{c.speaker}:</span>
              <span className="line-through decoration-muted">{c.from.name || "unnamed"}</span>
              <span className="text-xs text-muted">({pct(c.from.confidence)})</span>
              <span>→</span>
              <span className="font-semibold">{c.name || "unnamed"}</span>
              <span className="text-xs text-muted">({pct(c.confidence)})</span>
              {c.state !== "pending" && <Badge tone={c.state === "kept" ? "success" : "neutral"}>{c.state}</Badge>}
            </div>
            {c.reason && <p className="mt-0.5 text-xs text-muted">{c.reason}</p>}
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              {c.lines.slice(0, 6).map((n) => (
                <button key={n} className="rounded border border-border px-1.5 text-[11px] text-accent hover:bg-hover" onClick={() => seek(n)}
                        aria-label={`Show line ${n}`}>#{n}</button>
              ))}
              {c.on_screen && <Badge><MonitorPlay className="h-3 w-3" /> on screen</Badge>}
              {c.state === "pending" && <>
                <Button size="sm" variant="primary" onClick={() => act("keep", c.speaker)} loading={busy === `keep:${c.speaker}`}><Check className="h-3.5 w-3.5" /> Keep</Button>
                <Button size="sm" variant="ghost" onClick={() => act("revert", c.speaker)} loading={busy === `revert:${c.speaker}`}><Undo2 className="h-3.5 w-3.5" /> Revert</Button>
              </>}
            </div>
          </li>
        ))}
      </ul>
      {review.rejected.length > 0 && (
        <p className="text-[11px] text-muted">Ignored {review.rejected.length} suggestion{review.rejected.length === 1 ? "" : "s"} without support in the transcript.</p>
      )}
      <p className="text-[11px] text-muted">A changed name&apos;s voice is remembered only after you keep it.</p>
    </div>
  );
}

/**
 * One speaker: name, role, confidence and evidence, editable. Saving teaches memory (and their voice).
 */
function SpeakerRow({ id, transcript, onSaved }: { id: string; transcript: Transcript; onSaved: (t: Partial<Transcript>) => void }) {
  const label = transcript.labels[id];
  const name = transcript.display_names[id] ?? id;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({ name: label?.name || (name.startsWith("Speaker ") ? "" : name), role: label?.role ?? "" });
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const segs = transcript.segments.filter((s) => s.speaker === id);
  const talk = segs.reduce((sum, s) => sum + Math.max(0, s.end - s.start), 0);

  const save = async (value = draft) => {
    setSaving(true);
    try {
      const res = await api.put<{ labels: Transcript["labels"]; display_names: Transcript["display_names"] }>(
        `/api/meetings/transcripts/${transcript.id}/speakers`, { labels: { [id]: value } });
      onSaved(res);
      setEditing(false);
      toast.success(value.name ? `Saved ${value.name}` : "Label cleared",
        { description: value.name ? "Remembered in Memory → People for future meetings." : undefined });
    } catch (err) {
      toast.error("Couldn't save the label", { description: (err as Error).message });
    } finally {
      setSaving(false);
    }
  };

  return (
    <li className="rounded-xl border border-border p-3" data-testid="speaker-row" data-speaker={id}>
      <div className="flex items-start gap-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold text-white"
             style={{ background: speakerColor(id, transcript.speakers) }}>{initials(name)}</div>
        <div className="min-w-0 flex-1">
          {editing ? (
            <div className="space-y-2">
              <Input value={draft.name} placeholder="Name" aria-label={`Name for ${id}`} data-testid="speaker-name-input" autoFocus
                     onChange={(e) => setDraft({ ...draft, name: e.target.value })} onKeyDown={(e) => e.key === "Enter" && save()} />
              <Input value={draft.role} placeholder="Role (optional), e.g. QA lead" aria-label={`Role for ${id}`}
                     onChange={(e) => setDraft({ ...draft, role: e.target.value })} onKeyDown={(e) => e.key === "Enter" && save()} />
              <div className="flex gap-2">
                <Button size="sm" variant="primary" onClick={() => save()} loading={saving} data-testid="save-speaker"><Check className="h-3.5 w-3.5" /> Save</Button>
                <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
              </div>
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-sm font-semibold" data-testid="speaker-name">{name}</span>
                <ConfidenceBadge label={label} />
                {label?.source === "cloud" && label.cloud && (
                  <Badge tone="warning" title={label.cloud.reason}><Cloud className="h-3 w-3" /> {label.cloud.kept ? "Kept from" : "Changed by"} {label.cloud.model}</Badge>
                )}
              </div>
              <div className="mt-0.5 text-xs text-muted">
                {label?.role ? <span className="text-fg/80">{label.role} · </span> : null}
                {segs.length} turns · {fmtDuration(talk)}
              </div>
              {label?.name && label.source !== "user" && label.evidence?.length ? (
                <div className="mt-0.5 truncate text-xs text-muted" data-testid="speaker-why" title={label.evidence.join("\n")}>
                  {[...label.evidence].sort((a, b) => whyRank(b, label) - whyRank(a, label))[0]}
                </div>
              ) : null}
            </>
          )}
        </div>
        {!editing && (
          <div className="flex shrink-0 gap-0.5">
            {label && label.source === "auto" && label.name && (
              <Tooltip content="Confirm this guess">
                <Button size="icon-sm" variant="ghost" aria-label={`Confirm ${name}`} onClick={() => save({ name: label.name, role: label.role })}><Check className="h-3.5 w-3.5" /></Button>
              </Tooltip>
            )}
            <Tooltip content="Correct name or role">
              <Button size="icon-sm" variant="ghost" aria-label={`Edit ${name}`} data-testid="edit-speaker" onClick={() => setEditing(true)}><Pencil className="h-3.5 w-3.5" /></Button>
            </Tooltip>
          </div>
        )}
      </div>
      {!editing && label?.evidence?.length ? (
        <div className="mt-2 pl-11">
          <button className="flex items-center gap-1 text-xs text-muted hover:text-fg" onClick={() => setOpen(!open)} aria-expanded={open}>
            <ChevronDown className={clsx("h-3 w-3 transition-transform", open && "rotate-180")} /> Why
          </button>
          {open && <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-muted">{label.evidence.map((e, i) => <li key={i}>{e}</li>)}</ul>}
        </div>
      ) : null}
    </li>
  );
}

/**
 * The panel: the model choosers, Identify / Re-identify (streamed status), the cloud-review question, and
 * one SpeakerRow per speaker.
 */
export function SpeakersPanel({ transcript, onChange, busyTranscriber }: {
  transcript: Transcript; onChange: (t: Partial<Transcript>) => void; busyTranscriber: boolean;
}) {
  const { selectedModel, models } = useApp();
  const chosen = transcript.models;
  const [speakerModel, setSpeakerModel] = useState(chosen?.speaker_model ?? "");
  const [framesModel, setFramesModel] = useState(chosen?.frames_model ?? "");
  const [running, setRunning] = useState<"identify" | "split" | null>(null);
  const [ask, setAsk] = useState<{ model: string; provider: string; question: string } | null>(null);
  const [status, setStatus] = useState<string[]>([]);
  const [progress, setProgress] = useState<{ done: number; total: number; text: string } | null>(null);
  const localModel = selectedModel?.group === "local" ? selectedModel
    : models?.models.find((m) => m.group === "local" && m.status === "ready") ?? null;
  const textOnly = !transcript.diarization && transcript.speakers.length <= 1;
  const hasLabels = Object.keys(transcript.labels).length > 0;

  const run = async (kind: "identify" | "split", confirm?: "cloud" | "local") => {
    setRunning(kind);
    if (!confirm) setStatus([]);
    setProgress(null);
    try {
      let failed = false;
      const body = kind === "identify"
        ? { model_id: selectedModel?.id ?? localModel?.id ?? "", speaker_model: speakerModel,
            ...(confirm ? { confirm, review_only: true } : {}) }
        : { model_id: localModel?.id ?? "", speaker_model: speakerModel };
      await stream<StreamEvent>(`/api/meetings/transcripts/${transcript.id}/${kind === "identify" ? "identify" : "split-speakers"}`,
        body, (event) => {
          if (event.type === "status" || event.type === "notice") setStatus((s) => [...s, event.text]);
          if (event.type === "notice" && event.kind === "review_failed") toast.warning(event.text);
          if (event.type === "progress") setProgress(event);
          if (event.type === "labels") {
            onChange({ labels: event.labels, display_names: event.display_names,
                       ...(chosen ? { models: { ...chosen, speaker_model: speakerModel, speaker_used: event.model ?? "" } } : {}) });
          }
          if (event.type === "confirm" && event.kind === "sensitive" && event.stage === "speaker_review") {
            setAsk({ model: event.model ?? "the cloud model", provider: event.provider ?? "", question: event.question ?? "" });
          }
          if (event.type === "error") { failed = true; toast.error("Speaker labelling failed", { description: event.message }); }
          if (event.type === "done") {
            if (kind === "identify") {
              const review = (event.review as SpeakerReview | null) ?? null;
              onChange({ labels: event.labels as Transcript["labels"], display_names: event.display_names as Transcript["display_names"],
                         ...(review || !confirm ? { review } : {}) });
              const named = Object.values(event.labels as Record<string, SpeakerLabel>).filter((l) => l.name).length;
              if (review?.status === "done") {
                const n = review.changes.length;
                toast.success(n ? `${review.model} changed ${n} speaker name${n === 1 ? "" : "s"}` : `${review.model} agreed with all local names`,
                  { description: n ? "Keep or revert each change below." : undefined });
              } else if (!confirm) {
                toast.success(`Named ${named} of ${transcript.speakers.length} speakers`, { description: "Check the confidence and correct any that are wrong." });
              }
            } else {
              toast.success(`Split into ${event.speakers} speakers`);
              onChange({});
            }
          }
        });
      if (failed) return;
    } catch (err) {
      toast.error("Speaker labelling failed", { description: (err as Error).message });
    } finally {
      setRunning(null);
      setProgress(null);
    }
  };

  return (
    <div className="space-y-3" data-testid="speakers-panel">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs text-muted">
          {transcript.diarization ? (
            <span className="inline-flex items-center gap-1" title={transcript.has_voiceprints ? "Voice fingerprints saved: known voices are recognised" : "Transcribed before voice fingerprints were saved"}>
              <AudioLines className="h-3 w-3" /> Separated by voice{transcript.has_voiceprints ? " · voices remembered" : ""}
            </span>
          ) : textOnly ? "No speaker turns in this text" : "Speakers from the text"}
        </div>
        <div className="flex gap-1.5">
          {transcript.source_type !== "text" && (transcript.screen_info?.status !== "done" || framesModel !== (chosen?.frames_model ?? "")) && (
            <Button size="sm" variant="secondary" data-testid="read-screen-names" disabled={busyTranscriber}
                    title="For video recordings: read the names on the participants' tiles (on this Mac)"
                    onClick={async () => {
                      try {
                        await api.post(`/api/meetings/transcripts/${transcript.id}/screen-names`, { frames_model: framesModel });
                        toast.success("Reading names from the video", { description: "This runs in the transcriber; identify speakers again when it's done." });
                      } catch (err) {
                        toast.error("Couldn't read names from the video", { description: (err as Error).message });
                      }
                    }}>
              <MonitorPlay className="h-3.5 w-3.5" /> Read names from video
            </Button>
          )}
          {textOnly && (
            <Button size="sm" variant="secondary" onClick={() => run("split")} disabled={!!running || busyTranscriber || !localModel} loading={running === "split"}>
              <Split className="h-3.5 w-3.5" /> Split speakers
            </Button>
          )}
          <Button size="sm" variant="primary" onClick={() => run("identify")} disabled={!!running} loading={running === "identify"} data-testid="identify-speakers">
            <Sparkles className="h-3.5 w-3.5" /> {hasLabels ? "Re-identify" : "Identify speakers"}
          </Button>
        </div>
      </div>
      {running && (
        <div className="space-y-1.5 rounded-xl bg-surface-2 p-3 text-xs text-muted" aria-live="polite">
          {status.map((s, i) => <div key={i} className="flex items-center gap-1.5">{i === status.length - 1 ? <Spinner className="h-3 w-3" /> : <Check className="h-3 w-3" />} {s}</div>)}
          {progress && <><div>{progress.text}</div><Progress value={(progress.done / Math.max(1, progress.total)) * 100} /></>}
          {!status.length && !progress && <div className="flex items-center gap-1.5"><Spinner className="h-3 w-3" /> Starting…</div>}
        </div>
      )}
      <div className="grid gap-3 rounded-xl border border-border p-3 sm:grid-cols-2" data-testid="speaker-models">
        <div className="space-y-1">
          <ModelSelect kind="speaker" id="meeting-speaker-model" label="Reads the transcript" defaultLabel="Settings default"
            value={speakerModel} onChange={setSpeakerModel} disabled={!!running} />
          <p className="text-xs text-muted" data-testid="speaker-model-used">
            {chosen?.speaker_used ? `Last identified with ${chosen.speaker_used} on this Mac.` : "Not identified with a model yet."}
            {" "}Pick another and press {hasLabels ? "Re-identify" : "Identify speakers"}.
          </p>
        </div>
        {transcript.source_type !== "text" && (
          <div className="space-y-1">
            <ModelSelect kind="frames" id="meeting-frames-model" label="Reads the video frames" defaultLabel="Settings default"
              value={framesModel} onChange={setFramesModel} />
            <p className="text-xs text-muted" data-testid="frames-model-used">
              {chosen?.frames_used ? `Unclear frames were read by ${chosen.frames_used}.` : "No video frames read by a model yet."}
            </p>
          </div>
        )}
      </div>
      {transcript.review && <ReviewSummary transcript={transcript} review={transcript.review} onChange={onChange} />}
      {transcript.screen_info && transcript.screen_info.status === "done" && (
        <p className="flex items-center gap-1 text-xs text-muted" data-testid="screen-info">
          <MonitorPlay className="h-3 w-3" /> Names read from the video: {transcript.screen_info.resolved} of {transcript.screen_info.frames} frames
          {transcript.screen_info.vision_frames ? ` (${transcript.screen_info.vision_frames} by ${transcript.screen_info.vision_model})` : ""}
          {transcript.screen_info.screen_share ? ` · ${transcript.screen_info.screen_share} during screen share` : ""}
        </p>
      )}
      {!localModel && <Callout tone="warning">No local model is ready, so names come only from voices and name clues. Download Gemma 4 E4B in Settings → Local models.</Callout>}
      <ul className="space-y-2">{transcript.speakers.map((id) => <SpeakerRow key={id} id={id} transcript={transcript} onSaved={onChange} />)}</ul>
      <p className="text-xs text-faint">
        Names are guessed on this Mac with {chosen?.speaker_used || speakerModel || "the recommended local model"} from names on the video tiles, voices you labelled before,
        names mentioned in the meeting, your Memory, your past conversations and what each person says. Corrections are remembered.
        {selectedModel?.group === "cloud" && <> With {selectedModel.label} selected, it then reviews the names (Settings → Meetings).</>}
      </p>
      <Dialog open={ask !== null} onOpenChange={(o) => !o && setAsk(null)} title="Cloud review of speaker names"
        description={ask?.question}
        footer={<>
          <Button variant="ghost" onClick={() => { setAsk(null); toast.info("Kept the names found on this Mac"); }}>Keep on this Mac</Button>
          <Button variant="primary" data-testid="send-review" onClick={() => { setAsk(null); run("identify", "cloud"); }}>Send to {ask?.model}</Button>
        </>}>
        <p className="text-[13px] text-muted">
          {ask?.provider} would get the transcript, the speaker-name suggestions, names seen on screen and the people from your memory who may be in this
          meeting. Voice fingerprints and video frames are never sent.
        </p>
      </Dialog>
    </div>
  );
}
