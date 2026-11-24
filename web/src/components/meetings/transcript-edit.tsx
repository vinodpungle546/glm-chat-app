"use client";

/**
 * Editing the transcript yourself, while watching the recording: change a line's words, who said it (one line or
 * several selected lines, to a speaker, an attendee or someone new), split a line between two people at a word, or
 * merge it with the next line. Edits are applied after every automatic correction, so nothing overwrites them;
 * each can be undone, or all at once. After lines move to a named person, Quorum asks before updating that person's
 * voice fingerprint from them.
 */
import * as Popover from "@radix-ui/react-popover";
import { Check, History, Merge, Pencil, Scissors, Undo2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { playFrom } from "@/components/meetings/player";
import { Badge, Button, Dialog, Input } from "@/components/ui";
import { api, type Job, type Segment, type Transcript } from "@/lib/api";

/** Who said it: an existing speaker label, or a person's name (an attendee, or someone new). */
export type Who = { speaker?: string; name?: string };

export interface EditContext {
  editing: boolean;
  setEditing: (v: boolean) => void;
  selected: Set<number>;
  toggle: (i: number) => void;
  clear: () => void;
  busy: boolean;
  submit: (edit: Record<string, unknown>, done?: string) => Promise<boolean>;
  undo: (id?: string) => Promise<void>;
}

/** The editing state of a meeting page; `replace` takes the whole meeting the API returns after each edit. */
export function useTranscriptEdits(transcriptId: string, replace: (t: Transcript) => void) {
  const [editing, setEditingState] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [voice, setVoice] = useState<{ speaker: string; name: string } | null>(null);
  const base = `/api/meetings/transcripts/${transcriptId}`;
  const setEditing = (v: boolean) => { setEditingState(v); if (!v) setSelected(new Set()); };
  const toggle = (i: number) => setSelected((s) => { const n = new Set(s); if (n.has(i)) n.delete(i); else n.add(i); return n; });
  const submit = async (edit: Record<string, unknown>, done?: string) => {
    setBusy(true);
    try {
      const res = await api.post<Transcript>(`${base}/edits`, edit);
      replace(res);
      setSelected(new Set());
      if (done) toast.success(done);
      if (res.voice_question) setVoice(res.voice_question);
      return true;
    } catch (err) {
      toast.error("Couldn't save your edit", { description: (err as Error).message });
      return false;
    } finally {
      setBusy(false);
    }
  };
  const undo = async (id?: string) => {
    setBusy(true);
    try {
      replace(await api.del<Transcript>(id ? `${base}/edits/${id}` : `${base}/edits`));
      toast.success(id ? "Edit undone" : "All your edits undone");
    } catch (err) {
      toast.error("Couldn't undo", { description: (err as Error).message });
    } finally {
      setBusy(false);
    }
  };
  const ctx: EditContext = { editing, setEditing, selected, toggle, clear: () => setSelected(new Set()), busy, submit, undo };
  return { ctx, voice, setVoice };
}

/** A choice of who said something: the meeting's speakers, attendees who haven't spoken, or a new name. */
export function WhoPicker({ transcript, value, onPick, testid, label }: {
  transcript: Transcript; value?: string; onPick: (who: Who) => void; testid: string; label: string;
}) {
  const [typing, setTyping] = useState(false);
  const [name, setName] = useState("");
  const listeners = (transcript.attendees?.attendees ?? []).filter((a) => !a.speaker).map((a) => a.name);
  if (typing) {
    return (
      <form className="inline-flex gap-1" onSubmit={(e) => { e.preventDefault(); if (name.trim()) { onPick({ name: name.trim() }); setTyping(false); setName(""); } }}>
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Their name" autoFocus aria-label="Who said it"
               className="h-7 w-40 text-[13px]" data-testid={`${testid}-name`} />
        <Button size="icon-sm" type="submit" variant="primary" aria-label="Use this name" data-testid={`${testid}-name-save`}><Check className="h-3.5 w-3.5" /></Button>
        <Button size="icon-sm" type="button" variant="ghost" aria-label="Cancel" onClick={() => setTyping(false)}><X className="h-3.5 w-3.5" /></Button>
      </form>
    );
  }
  return (
    <select aria-label={label} data-testid={testid} value={value ? `spk:${value}` : ""}
            className="h-7 max-w-48 rounded-md border border-border bg-surface px-1.5 text-[13px]"
            onChange={(e) => {
              const v = e.target.value;
              if (v === "__new") setTyping(true);
              else if (v.startsWith("spk:")) onPick({ speaker: v.slice(4) });
              else if (v.startsWith("name:")) onPick({ name: v.slice(5) });
            }}>
      {!value && <option value="">{label}…</option>}
      <optgroup label="Speakers">
        {transcript.speakers.map((spk) => <option key={spk} value={`spk:${spk}`}>{transcript.display_names[spk] ?? spk}</option>)}
      </optgroup>
      {listeners.length > 0 && (
        <optgroup label="Attendees who haven't spoken">
          {listeners.map((n) => <option key={n} value={`name:${n}`}>{n}</option>)}
        </optgroup>
      )}
      <option value="__new">Someone else…</option>
    </select>
  );
}

/** The bar above the transcript: Edit on/off, moving the selected lines, and your edits (Undo, Undo all). */
export function EditBar({ ctx, transcript }: { ctx: EditContext; transcript: Transcript }) {
  const edits = transcript.edits ?? [];
  const n = ctx.selected.size;
  return (
    <div className="flex flex-wrap items-center gap-2 text-[13px]" data-testid="edit-bar">
      <Button size="sm" variant={ctx.editing ? "primary" : "secondary"} onClick={() => ctx.setEditing(!ctx.editing)} data-testid="edit-mode">
        <Pencil className="h-3.5 w-3.5" /> {ctx.editing ? "Done editing" : "Edit transcript"}
      </Button>
      {ctx.editing && (
        n > 0 ? (
          <span className="flex items-center gap-1.5" data-testid="edit-selected">
            {n} line{n === 1 ? "" : "s"} selected · said by
            <WhoPicker transcript={transcript} testid="assign-selected" label="Who said them"
              onPick={(who) => ctx.submit({ kind: "speaker", lines: [...ctx.selected].sort((a, b) => a - b), ...who },
                `${n} line${n === 1 ? "" : "s"} moved`)} />
            <Button size="sm" variant="ghost" onClick={ctx.clear}>Clear</Button>
          </span>
        ) : (
          <span className="text-xs text-muted">Click a line&apos;s words to change them, or its speaker to reassign it. Tick several lines to move them together. Times play the video.</span>
        )
      )}
      {edits.length > 0 && (
        <Popover.Root>
          <Popover.Trigger asChild>
            <Button size="sm" variant="ghost" className="ml-auto" data-testid="edits-list"><History className="h-3.5 w-3.5" /> Your edits ({edits.length})</Button>
          </Popover.Trigger>
          <Popover.Portal>
            <Popover.Content align="end" className="z-50 w-96 max-w-[90vw] space-y-1 rounded-xl border border-border bg-surface p-2 shadow-card">
              <ul className="max-h-80 space-y-1 overflow-y-auto">
                {[...edits].reverse().map((e) => (
                  <li key={e.id} className="flex items-start gap-2 rounded-md px-1.5 py-1 text-[13px]" data-testid="edit-item">
                    <span className="min-w-0 flex-1 break-words">{e.text}
                      {e.state === "stale" && <span className="text-warning"> (its line is gone: not applied)</span>}</span>
                    <Button size="sm" variant="ghost" className="h-7 px-2 text-xs" disabled={ctx.busy} onClick={() => ctx.undo(e.id)}
                            data-testid="edit-undo"><Undo2 className="h-3 w-3" /> Undo</Button>
                  </li>
                ))}
              </ul>
              <div className="flex justify-end border-t border-border pt-1.5">
                <Button size="sm" variant="outline" disabled={ctx.busy} onClick={() => ctx.undo()} data-testid="edits-undo-all">Undo all</Button>
              </div>
            </Popover.Content>
          </Popover.Portal>
        </Popover.Root>
      )}
    </div>
  );
}

/** One line while editing: its speaker (reassign), its words (click to change), Split and Merge. */
export function EditLine({ ctx, transcript, index, seg, name }: {
  ctx: EditContext; transcript: Transcript; index: number; seg: Segment; name: string;
}) {
  const [mode, setMode] = useState<"view" | "text" | "split">("view");
  const [text, setText] = useState(seg.text);
  const [at, setAt] = useState<number | null>(null);
  const [second, setSecond] = useState<Who | null>(null);
  const words = seg.text.split(/\s+/).filter(Boolean);
  const last = index === transcript.segments.length - 1;
  useEffect(() => { setText(seg.text); }, [seg.text]);
  const saveText = async () => {
    if (text.trim() === seg.text.trim()) { setMode("view"); return; }
    if (await ctx.submit({ kind: "text", lines: [index], text }, "Line changed")) setMode("view");
  };
  const saveSplit = async () => {
    if (at === null || !second) return;
    const parts = [{ text: words.slice(0, at).join(" "), speaker: seg.speaker },
                   { text: words.slice(at).join(" "), ...second }];
    if (await ctx.submit({ kind: "split", lines: [index], parts }, "Line split")) { setMode("view"); setAt(null); setSecond(null); }
  };
  return (
    <div className="min-w-0 space-y-1" data-testid="edit-line">
      <div className="flex flex-wrap items-center gap-1.5">
        <input type="checkbox" checked={ctx.selected.has(index)} onChange={() => ctx.toggle(index)} aria-label={`Select line ${index + 1}`}
               data-testid="seg-select" />
        <WhoPicker transcript={transcript} value={seg.speaker} testid="seg-speaker" label={`Who said line ${index + 1}`}
          onPick={(who) => who.speaker !== seg.speaker && ctx.submit({ kind: "speaker", lines: [index], ...who }, `Line now said by ${who.name ?? transcript.display_names[who.speaker ?? ""] ?? name}`)} />
        {seg.edited && <Badge tone="accent">edited</Badge>}
        <span className="ml-auto flex gap-0.5">
          <Button size="sm" variant="ghost" className="h-7 px-2 text-xs" onClick={() => { setMode(mode === "split" ? "view" : "split"); playFrom(seg.start); }}
                  disabled={words.length < 2 || ctx.busy} data-testid="seg-split"><Scissors className="h-3 w-3" /> Split</Button>
          {!last && (
            <Button size="sm" variant="ghost" className="h-7 px-2 text-xs" disabled={ctx.busy} data-testid="seg-merge"
                    onClick={() => ctx.submit({ kind: "merge", lines: [index] }, "Lines merged")}><Merge className="h-3 w-3" /> Merge with next</Button>
          )}
        </span>
      </div>
      {mode === "text" ? (
        <div className="space-y-1">
          <textarea value={text} onChange={(e) => setText(e.target.value)} rows={Math.min(8, Math.max(2, Math.ceil(text.length / 80)))}
                    autoFocus className="w-full rounded-md border border-border bg-surface px-2 py-1.5 text-[14px] leading-relaxed"
                    aria-label={`Words of line ${index + 1}`} data-testid="seg-text-input"
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void saveText(); }
                      if (e.key === "Escape") { setText(seg.text); setMode("view"); }
                    }} />
          <div className="flex gap-1.5">
            <Button size="sm" variant="primary" onClick={saveText} loading={ctx.busy} data-testid="seg-text-save"><Check className="h-3.5 w-3.5" /> Save</Button>
            <Button size="sm" variant="ghost" onClick={() => { setText(seg.text); setMode("view"); }}>Cancel</Button>
          </div>
        </div>
      ) : mode === "split" ? (
        <div className="space-y-1.5 rounded-md border border-dashed border-border p-2" data-testid="split-panel">
          <p className="text-xs text-muted">Click the first word the other person said.</p>
          <p className="text-[14px] leading-relaxed">
            {words.map((w, k) => (
              <span key={k}>
                {k > 0 && at === k && <span className="mx-0.5 inline-block h-4 w-0.5 translate-y-0.5 bg-accent" />}
                <button type="button" disabled={k === 0} onClick={() => setAt(k)} data-testid="split-word"
                        className={at !== null && k >= at ? "rounded bg-accent-soft px-0.5" : "rounded px-0.5 hover:bg-hover"}>{w}</button>{" "}
              </span>
            ))}
          </p>
          {at !== null && (
            <div className="flex flex-wrap items-center gap-1.5 text-xs">
              From “{words[at]}” said by
              <WhoPicker transcript={transcript} value={second?.speaker} testid="split-speaker" label="Who said the rest" onPick={setSecond} />
              {second?.name && <span className="font-medium">{second.name}</span>}
              <Button size="sm" variant="primary" disabled={!second} onClick={saveSplit} loading={ctx.busy} data-testid="split-save">Split</Button>
              <Button size="sm" variant="ghost" onClick={() => { setMode("view"); setAt(null); setSecond(null); }}>Cancel</Button>
            </div>
          )}
        </div>
      ) : (
        <button type="button" className="w-full cursor-text rounded text-left text-[14px] leading-relaxed hover:bg-hover"
                onClick={() => { setMode("text"); playFrom(seg.start); }} data-testid="seg-text" title="Change the words">
          {seg.text}
        </button>
      )}
    </div>
  );
}

/** After lines moved to a named person: update their voice fingerprint from the lines they now have? (A transcriber
 *  job with the speaker-embedding model, then the new fingerprint is saved to that person.) */
export function VoiceQuestion({ transcript, ask, onClose }: {
  transcript: Transcript; ask: { speaker: string; name: string } | null; onClose: () => void;
}) {
  const [job, setJob] = useState<Job | null>(null);
  const [target, setTarget] = useState<{ speaker: string; name: string } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const base = `/api/meetings/transcripts/${transcript.id}`;
  const name = ask?.name ?? target?.name ?? "";
  const finish = useCallback(async (j: Job, who: { speaker: string; name: string } | null) => {
    if (j.status === "done" && who) {
      const r = await api.post<{ learned: string[] }>(`${base}/voiceprints/learn`, { speakers: [who.speaker] });
      toast.success(r.learned.length ? `Updated ${who.name}'s voice fingerprint` : "No voice fingerprint could be made from those lines");
    } else if (j.status === "error") {
      toast.error("Couldn't update the voice fingerprint", { description: j.error ?? undefined });
    }
    setJob(null);
    setTarget(null);
  }, [base]);
  useEffect(() => {
    if (!job || (job.status !== "queued" && job.status !== "running")) return;
    timer.current = setTimeout(async () => {
      try {
        const next = await api.get<Job>(`/api/meetings/jobs/${job.id}`);
        if (next.status === "queued" || next.status === "running") setJob(next);
        else void finish(next, target);
      } catch {
        setJob({ ...job });
      }
    }, 2000);
    return () => { if (timer.current) clearTimeout(timer.current); };
  }, [job, finish, target]);
  const start = async () => {
    if (!ask) return;
    const who = ask;
    onClose();
    try {
      const j = await api.post<Job>(`${base}/voiceprints/refresh`, { speakers: [who.speaker] });
      setTarget(who);
      setJob(j);
      if (j.status !== "queued" && j.status !== "running") void finish(j, who);
      toast(`Updating ${who.name}'s voice fingerprint`, { description: "Made on this Mac from the lines they now have." });
    } catch (err) {
      toast.error("Couldn't update the voice fingerprint", { description: (err as Error).message });
    }
  };
  return (
    <>
      {job && target && (
        <p className="text-xs text-muted" data-testid="voice-refresh">Updating {target.name}&apos;s voice fingerprint…</p>
      )}
      <Dialog open={ask !== null} onOpenChange={(o) => !o && onClose()} title={`Update ${name}'s voice fingerprint?`}
        description="Quorum can make their voice fingerprint again from the lines they now have, so they're recognised in later meetings."
        footer={<>
          <Button variant="ghost" onClick={onClose} data-testid="voice-not-now">Not now</Button>
          <Button variant="primary" onClick={start} data-testid="voice-update">Update</Button>
        </>}>
        <p className="text-[13px] text-muted">It runs on this Mac (about a minute). Voice fingerprints never leave it.</p>
      </Dialog>
    </>
  );
}
