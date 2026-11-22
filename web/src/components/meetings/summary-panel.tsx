"use client";

/**
 * The Summary panel of a meeting: generate meeting notes with the chosen model, change type, detail and
 * sections, edit them in place, and export (copy, Word, PDF, CSV, follow-up email).
 */
import * as Menu from "@radix-ui/react-dropdown-menu";
import * as Popover from "@radix-ui/react-popover";
import { Check, ChevronDown, Cloud, Copy, Download, Lock, Mail, Pencil, RotateCcw, SlidersHorizontal, Sparkles } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { Markdown } from "@/components/markdown";
import { ModelSelect } from "@/components/meetings/model-select";
import { ModelAvailability } from "@/components/model-availability";
import { Badge, Button, Callout, ConfirmDialog, Dialog, Input, Progress, Spinner, Switch, Textarea } from "@/components/ui";
import { api, API_URL, stream, type DetailLevel, type MeetingType, type StreamEvent, type Summary, type Transcript } from "@/lib/api";
import { clsx, fmtDuration, shortDate } from "@/lib/format";

const DETAILS: { id: DetailLevel; label: string; hint: string }[] = [
  { id: "brief", label: "Brief", hint: "Summary, decisions and actions" },
  { id: "standard", label: "Standard", hint: "All sections" },
  { id: "detailed", label: "Detailed", hint: "Adds per-topic notes and quotes" },
];
const OPTIONAL_SECTIONS: { id: string; label: string }[] = [
  { id: "type", label: "Meeting-type sections" },
  { id: "discussion", label: "Agenda & discussion" },
  { id: "risks", label: "Risks, issues & blockers" },
  { id: "questions", label: "Open questions" },
  { id: "next", label: "Next steps & next meeting" },
  { id: "appendix", label: "Appendix (quotes, transcript link)" },
];
const EXPORTS = [
  { format: "md", label: "Markdown (.md)" },
  { format: "docx", label: "Word (.docx)" },
  { format: "pdf", label: "PDF" },
  { format: "html", label: "Email-ready HTML" },
  { format: "csv", label: "Action items (.csv)" },
];

/** Which optional sections a detail level shows (brief = core sections only). */
function defaultSections(detail: DetailLevel): Record<string, boolean> {
  return Object.fromEntries(OPTIONAL_SECTIONS.map((s) => [s.id, detail !== "brief"]));
}

/** The Brief / Standard / Detailed switch. */
function Segmented({ value, onChange, disabled }: { value: DetailLevel; onChange: (d: DetailLevel) => void; disabled?: boolean }) {
  return (
    <div className="inline-flex rounded-lg bg-surface-2 p-0.5" role="radiogroup" aria-label="Detail level">
      {DETAILS.map((d) => (
        <button key={d.id} role="radio" aria-checked={value === d.id} title={d.hint} disabled={disabled} data-testid={`detail-${d.id}`}
                onClick={() => onChange(d.id)}
                className={clsx("rounded-md px-2.5 py-1 text-xs font-medium disabled:opacity-50",
                  value === d.id ? "bg-surface text-fg shadow-sm" : "text-muted hover:text-fg")}>
          {d.label}
        </button>
      ))}
    </div>
  );
}

/** A menu of checkboxes to show or hide optional sections. */
function SectionsPicker({ value, onChange, disabled }: { value: Record<string, boolean>; onChange: (v: Record<string, boolean>) => void; disabled?: boolean }) {
  const [local, setLocal] = useState(value);
  useEffect(() => { setLocal(value); }, [value]);
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <Button size="sm" variant="ghost" disabled={disabled} data-testid="sections-button"><SlidersHorizontal className="h-3.5 w-3.5" /> Sections</Button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content align="start" sideOffset={6} className="z-50 w-64 rounded-xl border border-border bg-surface p-2 shadow-card" data-testid="sections-popover">
          <p className="px-2 pb-1 text-[11px] text-muted">Header, summary, decisions and action items are always included.</p>
          {OPTIONAL_SECTIONS.map((s) => (
            <label key={s.id} className="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-[13px] hover:bg-hover">
              <input type="checkbox" checked={local[s.id] ?? true} aria-label={s.label}
                     onChange={(e) => { const next = { ...local, [s.id]: e.target.checked }; setLocal(next); onChange(next); }} /> {s.label}
            </label>
          ))}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

/** Rendered notes, split at "## " headings; each section can be edited in place. */
function EditableNotes({ markdown, onSave, tid }: { markdown: string; onSave: (md: string) => Promise<void>; tid: string }) {
  const parts = useMemo(() => markdown.split(/\n(?=## )/), [markdown]);
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const onClick = (e: React.MouseEvent) => {
    const a = (e.target as HTMLElement).closest("a");
    const href = a?.getAttribute("href") ?? "";
    const m = href.match(new RegExp(`/meetings/${tid}\\?(t|line)=(\\d+)`));
    if (m) {
      e.preventDefault();
      window.dispatchEvent(new CustomEvent("quorum:seek", { detail: { [m[1] === "t" ? "seconds" : "line"]: Number(m[2]) } }));
    }
  };
  const save = async () => {
    setSaving(true);
    try {
      const next = parts.map((p, i) => (i === editing ? draft.trimEnd() : p)).join("\n");
      await onSave(next);
      setEditing(null);
    } finally {
      setSaving(false);
    }
  };
  return (
    <div onClickCapture={onClick} className="notes-view" data-testid="notes">
      {parts.map((part, i) => (
        <div key={i} className="group relative rounded-lg" data-testid="notes-section">
          {editing === i ? (
            <div className="space-y-2 py-2">
              <Textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={Math.min(18, draft.split("\n").length + 2)}
                        className="font-mono text-xs leading-5" aria-label="Edit section" data-testid="section-editor" autoFocus />
              <div className="flex gap-2">
                <Button size="sm" variant="primary" onClick={save} loading={saving} data-testid="save-section"><Check className="h-3.5 w-3.5" /> Save</Button>
                <Button size="sm" variant="ghost" onClick={() => setEditing(null)}>Cancel</Button>
              </div>
            </div>
          ) : (
            <>
              <Markdown className="overflow-x-auto">{part}</Markdown>
              <button aria-label={`Edit section ${i + 1}`} data-testid="edit-section"
                      onClick={() => { setEditing(i); setDraft(part); }}
                      className="absolute right-0 top-1 rounded-md bg-surface/90 p-1 text-muted opacity-0 shadow-sm hover:text-fg focus:opacity-100 group-hover:opacity-100">
                <Pencil className="h-3.5 w-3.5" />
              </button>
            </>
          )}
        </div>
      ))}
    </div>
  );
}

/** Shows the drafted follow-up email (subject and text) with copy buttons. */
function EmailDialog({ tid, onClose }: { tid: string; onClose: () => void }) {
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState<string | null>(null);
  useEffect(() => {
    api.get<{ subject: string; text: string }>(`/api/meetings/transcripts/${tid}/summary/email`)
      .then((e) => { setSubject(e.subject); setBody(e.text); }).catch((err) => toast.error("Couldn't draft the email", { description: err.message }));
  }, [tid]);
  const mailto = `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent((body ?? "").slice(0, 1800))}`;
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} title="Follow-up email" className="max-w-2xl"
      description="A neutral recap built from the notes (including your edits). Nothing is invented; review before sending."
      footer={<>
        <Button variant="ghost" onClick={() => navigator.clipboard.writeText(`Subject: ${subject}\n\n${body}`).then(() => toast.success("Email copied"))} data-testid="copy-email">
          <Copy className="h-3.5 w-3.5" /> Copy</Button>
        <a href={mailto}><Button variant="primary"><Mail className="h-3.5 w-3.5" /> Open in email app</Button></a>
      </>}>
      {body === null ? <div className="skeleton h-48" /> : (
        <div className="space-y-2" data-testid="email-draft">
          <Input value={subject} onChange={(e) => setSubject(e.target.value)} aria-label="Subject" />
          <Textarea value={body} onChange={(e) => setBody(e.target.value)} rows={14} aria-label="Email body" className="text-[13px]" />
        </div>
      )}
    </Dialog>
  );
}

/**
 * The panel. Generate streams the notes from the API; with no summary model chosen it first asks which
 * model to use, and a cloud model asks before the transcript is sent.
 */
export function SummaryPanel({ transcript, onSaved }: { transcript: Transcript; onSaved: (s: Transcript["summary"]) => void }) {
  const { selectedModel, prefs, models, updatePrefs } = useApp();
  const summary = transcript.summary;
  // "" = no model chosen for this meeting: Settings' choice is used, and without one the user is asked.
  const [summaryModel, setSummaryModel] = useState(transcript.models?.summary_model ?? "");
  const chosenId = summaryModel || prefs?.summary_model || "";
  // the model that will write the summary (never the chat model by default)
  const writer = chosenId ? models?.models.find((m) => m.id === chosenId) ?? null : null;
  // "Which model should write the summary?" dialog: the picked model, and whether to make it the default
  const [askModel, setAskModel] = useState(false);
  const [pick, setPick] = useState("");
  const [pickDefault, setPickDefault] = useState(false);
  const skillOn = prefs?.meeting_notes_skill ?? true;
  const [text, setText] = useState("");
  const [running, setRunning] = useState(false);
  const [statuses, setStatuses] = useState<string[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number; text: string } | null>(null);
  const [confirm, setConfirm] = useState<{ reasons: string[]; localModel: string | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [model, setModel] = useState<string>("");
  const [types, setTypes] = useState<MeetingType[]>([]);
  const [detected, setDetected] = useState<{ type: string; label: string } | null>(null);
  const [meetingType, setMeetingType] = useState<string>(summary?.meeting_type ?? "auto");
  const [detail, setDetail] = useState<DetailLevel>(summary?.detail ?? "standard");
  const [sections, setSections] = useState<Record<string, boolean>>(summary?.sections ?? defaultSections("standard"));
  const [sensitive, setSensitive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [discard, setDiscard] = useState<null | (() => void)>(null);
  const [emailOpen, setEmailOpen] = useState(false);
  const [typeHint, setTypeHint] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    api.get<MeetingType[]>("/api/meetings/types").then(setTypes).catch(() => setTypes([]));
    api.get<{ type: string; label: string }>(`/api/meetings/transcripts/${transcript.id}/meeting-type`).then(setDetected).catch(() => setDetected(null));
  }, [transcript.id]);

  const generate = async (answer?: "local" | "cloud", modelOverride?: string) => {
    const useModel = modelOverride ?? summaryModel;
    if (!answer && !modelOverride && !(useModel || prefs?.summary_model)) {
      setAskModel(true);  // no model chosen yet: ask instead of using the chat model
      return;
    }
    setRunning(true);
    setText("");
    setStatuses([]);
    setNotice(null);
    setProgress(null);
    setConfirm(null);
    setError(null);
    setTypeHint(false);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await stream<StreamEvent>(`/api/meetings/transcripts/${transcript.id}/summary`, {
        model_id: selectedModel?.id ?? "", summary_model: useModel, confirm: answer ?? null, meeting_type: meetingType, detail,
        sections: { header: true, tldr: true, decisions: true, actions: true, ...sections }, sensitive_detail: sensitive,
      }, (event) => {
        if (event.type === "start") setModel(event.model);
        if (event.type === "delta") { setProgress(null); setText((t) => t + event.text); }
        if (event.type === "status") setStatuses((s) => (s.length && s[s.length - 1].split(" · ")[0] === event.text.split(" · ")[0] ? [...s.slice(0, -1), event.text] : [...s, event.text]));
        if (event.type === "notice") setNotice(event.text.replace(/^ℹ️\s*/, ""));
        if (event.type === "progress") setProgress(event);
        if (event.type === "confirm" && event.kind === "sensitive") setConfirm({ reasons: event.reasons, localModel: event.local_model ?? null });
        if (event.type === "choose_model") setAskModel(true);
        if (event.type === "error") setError(event.message);
        if (event.type === "done") {
          const s = event.summary as Summary;
          onSaved(s);
          if (s.meeting_type) setMeetingType(s.meeting_type);
          setText("");
          toast.success(s.skill ? "Meeting notes ready" : "Summary ready",
            { description: s.warnings?.length ? `${s.warnings.length} note${s.warnings.length === 1 ? "" : "s"} to check` : undefined });
        }
      }, controller.signal);
    } catch (err) {
      if ((err as Error).name !== "AbortError") setError((err as Error).message);
    } finally {
      setRunning(false);
      abortRef.current = null;
    }
  };

  const rerender = async (body: { meeting_type?: string; detail?: DetailLevel; sections?: Record<string, boolean> }) => {
    const go = async () => {
      setBusy(true);
      try {
        const s = await api.post<Summary>(`/api/meetings/transcripts/${transcript.id}/summary/render`, body);
        onSaved(s);
        setDetail(s.detail ?? "standard");
        setSections(s.sections ?? defaultSections(s.detail ?? "standard"));
        if (body.meeting_type) setTypeHint(true);
      } catch (err) {
        toast.error("Couldn't update the notes", { description: (err as Error).message });
      } finally {
        setBusy(false);
      }
    };
    if (summary?.edited) setDiscard(() => go);
    else await go();
  };

  const saveEdit = async (markdown: string) => {
    try {
      onSaved(await api.put<Summary>(`/api/meetings/transcripts/${transcript.id}/summary`, { markdown }));
      toast.success("Edits saved");
    } catch (err) {
      toast.error("Couldn't save the edit", { description: (err as Error).message });
      throw err;
    }
  };

  const copy = async () => {
    try {
      const clip = await api.get<{ html: string; text: string }>(`/api/meetings/transcripts/${transcript.id}/summary/clipboard`);
      if (typeof ClipboardItem !== "undefined" && navigator.clipboard.write) {
        await navigator.clipboard.write([new ClipboardItem({
          "text/html": new Blob([clip.html], { type: "text/html" }), "text/plain": new Blob([clip.text], { type: "text/plain" }) })]);
      } else {
        await navigator.clipboard.writeText(clip.text);
      }
      toast.success("Copied", { description: "Pastes as formatted text in email, Teams and Confluence, and as plain text in Slack." });
    } catch (err) {
      toast.error("Couldn't copy", { description: (err as Error).message });
    }
  };

  const unavailable = writer && writer.status !== "ready";
  const structured = summary?.skill === "meeting-notes";
  const typeOptions = (
    <select value={meetingType} onChange={(e) => { setMeetingType(e.target.value); if (structured && e.target.value !== "auto") rerender({ meeting_type: e.target.value }); }}
            aria-label="Meeting type" data-testid="meeting-type" disabled={running || busy}
            className="h-8 rounded-lg border border-border bg-surface px-2 text-[13px]">
      {!structured && <option value="auto">Auto-detect{detected ? ` (${detected.label})` : ""}</option>}
      {types.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
    </select>
  );

  return (
    <div className="space-y-3" data-testid="summary-panel">
      {unavailable && writer && <ModelAvailability model={writer} compact />}
      {transcript.followup?.state === "needs_model" && !summary && !running && (
        <Callout tone="info" data-testid="summary-needs-model">
          The speakers were identified after transcription. Choose which model writes the summary, then press Generate.
        </Callout>
      )}
      {transcript.followup?.state === "needs_ok" && !summary && !running && (
        <Callout tone="info" data-testid="summary-needs-ok">
          The speakers were identified after transcription. The summary model is a cloud model, so it waits for you:
          press Generate to choose whether the transcript may be sent, or pick a local model below.
        </Callout>
      )}
      {!running && (
        <div className="flex flex-wrap items-end gap-3" data-testid="summary-model-row">
          <ModelSelect kind="summary" id="meeting-summary-model" label="Writes the summary"
            defaultLabel={prefs?.summary_model ? `Settings default (${models?.models.find((m) => m.id === prefs.summary_model)?.label ?? prefs.summary_model})` : "Ask me"}
            value={summaryModel} onChange={setSummaryModel} />
          <span className="pb-2 text-xs text-muted" data-testid="summary-model-used">
            {summary?.model ? `Last written by ${summary.model}.` : ""} Kept for this meeting.
          </span>
        </div>
      )}
      {confirm && (
        <Callout tone="warning" title="Send this meeting to the cloud?" role="alertdialog">
          <span>{confirm.reasons.join("; ")}. Summarise it on a local model instead (nothing leaves this Mac)?</span>
          {!confirm.localModel && <p className="mt-1 text-[12px]">No local model is installed yet: download one in Settings → Models.</p>}
          <div className="mt-2.5 flex flex-wrap gap-2">
            <Button size="sm" variant="primary" onClick={() => generate("local")} disabled={!confirm.localModel} data-testid="summary-local"><Lock className="h-3.5 w-3.5" /> Use local model{confirm.localModel ? ` (${confirm.localModel})` : ""}</Button>
            <Button size="sm" variant="secondary" onClick={() => generate("cloud")} data-testid="summary-cloud"><Cloud className="h-3.5 w-3.5" /> Send to cloud anyway</Button>
            <Button size="sm" variant="ghost" onClick={() => setConfirm(null)}>Cancel</Button>
          </div>
        </Callout>
      )}
      {running && (
        <div className="space-y-2" aria-live="polite">
          {model && <div className="text-xs text-muted">Writing with {model}</div>}
          {notice && <Callout tone="info" data-testid="summary-notice">{notice}</Callout>}
          {statuses.length > 0 && (
            <ul className="space-y-1 text-xs text-muted" data-testid="summary-status">
              {statuses.map((s, i) => <li key={i} className="flex items-center gap-1.5">{i === statuses.length - 1 ? <Spinner className="h-3 w-3" /> : <Check className="h-3 w-3" />} {s}</li>)}
            </ul>
          )}
          {progress && (
            <div className="space-y-1.5" data-testid="summary-progress">
              <div className="text-xs text-muted">{progress.text}</div>
              <Progress value={(progress.done / Math.max(1, progress.total)) * 100} />
            </div>
          )}
          {text ? <Markdown streaming>{text}</Markdown> : !progress && !statuses.length && <div className="flex items-center gap-2 text-sm text-muted"><Spinner /> Reading the meeting…</div>}
          <Button size="sm" variant="ghost" onClick={() => abortRef.current?.abort()}>Stop</Button>
        </div>
      )}
      {error && !running && <Callout tone="danger" title="Couldn't write the notes" data-testid="summary-error"
        action={<Button size="sm" onClick={() => generate()}><RotateCcw className="h-3.5 w-3.5" /> Retry</Button>}>{error}</Callout>}

      {!running && summary && structured && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
            <span>{summary.model} · {shortDate(summary.created_at)}{summary.seconds ? ` · ${fmtDuration(summary.seconds)}` : ""}
              {summary.parts && summary.parts > 1 ? ` · ${summary.parts} parts` : ""}
              {summary.edited && <Badge tone="warning" className="ml-1.5">Edited</Badge>}</span>
          </div>
          <div className="flex flex-wrap items-center gap-2" data-testid="notes-toolbar">
            {typeOptions}
            <Segmented value={summary.detail ?? detail} onChange={(d) => rerender({ detail: d })} disabled={busy} />
            <SectionsPicker value={summary.sections ?? sections} onChange={(v) => rerender({ sections: { header: true, tldr: true, decisions: true, actions: true, ...v } })} disabled={busy} />
            {busy && <Spinner className="h-3.5 w-3.5" />}
          </div>
          <div className="flex flex-wrap gap-1.5">
            <Button size="sm" variant="secondary" onClick={copy} data-testid="copy-notes"><Copy className="h-3.5 w-3.5" /> Copy</Button>
            <Menu.Root>
              <Menu.Trigger asChild>
                <Button size="sm" variant="secondary" data-testid="export-menu"><Download className="h-3.5 w-3.5" /> Export <ChevronDown className="h-3 w-3" /></Button>
              </Menu.Trigger>
              <Menu.Portal>
                <Menu.Content align="start" className="z-50 min-w-48 rounded-xl border border-border bg-surface p-1 shadow-card">
                  {EXPORTS.map((x) => (
                    <Menu.Item key={x.format} asChild>
                      <a href={`${API_URL}/api/meetings/transcripts/${transcript.id}/summary/export?format=${x.format}`} data-testid={`export-${x.format}`}
                         className="flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-[13px] outline-none data-[highlighted]:bg-hover">
                        {x.label}
                      </a>
                    </Menu.Item>
                  ))}
                </Menu.Content>
              </Menu.Portal>
            </Menu.Root>
            <Button size="sm" variant="secondary" onClick={() => setEmailOpen(true)} data-testid="draft-email"><Mail className="h-3.5 w-3.5" /> Follow-up email</Button>
            <Button size="sm" variant="ghost" onClick={() => generate()} disabled={!!unavailable} data-testid="regenerate"><RotateCcw className="h-3.5 w-3.5" /> Regenerate</Button>
          </div>
          {typeHint && summary.detected_type && summary.meeting_type !== summary.detected_type.type && (
            <p className="text-xs text-muted" data-testid="type-hint">Shown as {types.find((t) => t.id === summary.meeting_type)?.label}. Regenerate to extract the details this type needs.</p>
          )}
          {transcript.summary?.stale && (
            <Callout tone="info" title="The transcript was corrected after these notes" data-testid="notes-stale"
                     action={<Button size="sm" onClick={() => generate()} disabled={!!unavailable}><RotateCcw className="h-3.5 w-3.5" /> Regenerate</Button>}>
              Words or speakers changed with another transcript of the meeting. Regenerate to use them.
            </Callout>
          )}
          {!!summary.warnings?.length && (
            <Callout tone="warning" title="Check these" data-testid="notes-warnings">
              <ul className="list-disc pl-4">{summary.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
            </Callout>
          )}
          <EditableNotes markdown={summary.markdown} onSave={saveEdit} tid={transcript.id} />
        </div>
      )}

      {!running && summary && !structured && (
        <div>
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
            <span>{summary.model} · {shortDate(summary.created_at)}{summary.seconds ? ` · ${fmtDuration(summary.seconds)}` : ""}</span>
            <span className="flex gap-1">
              <Button size="sm" variant="ghost" onClick={() => navigator.clipboard.writeText(summary.markdown).then(() => toast("Copied"))}><Copy className="h-3.5 w-3.5" /> Copy</Button>
              <Button size="sm" variant="ghost" onClick={() => generate()} disabled={!!unavailable}><RotateCcw className="h-3.5 w-3.5" /> Regenerate</Button>
            </span>
          </div>
          <Markdown>{summary.markdown}</Markdown>
        </div>
      )}

      {!running && !summary && !confirm && (
        <div className="rounded-xl border border-dashed border-border-strong p-4" data-testid="notes-options">
          {skillOn ? (
            <div className="space-y-3">
              <p className="text-[13px] text-muted">Professional meeting notes: a summary, decisions, action items with owners and dates, risks and next steps — every item linked to its moment in the transcript.</p>
              <div className="flex flex-wrap items-center gap-2">{typeOptions}<Segmented value={detail} onChange={(d) => { setDetail(d); setSections(defaultSections(d)); }} />
                <SectionsPicker value={sections} onChange={setSections} /></div>
              <Switch id="sensitive-detail" checked={sensitive} onChange={setSensitive} label="Include sensitive details"
                      description="HR, salary and personal matters are summarised at a high level unless you turn this on." />
            </div>
          ) : (
            <p className="text-center text-[13px] text-muted">Minutes with a summary, decisions, action items (owner, due date) and open questions.</p>
          )}
          <div className="mt-3 text-center">
            <Button variant="primary" onClick={() => generate()} disabled={!!unavailable} data-testid="generate-summary">
              <Sparkles className="h-4 w-4" /> {skillOn ? "Generate notes" : "Generate summary"}{writer ? ` with ${writer.label}` : " (choose a model)"}
            </Button>
            <p className="mt-2 text-xs text-faint">Long meetings are processed in parts, then merged — nothing is skipped.</p>
          </div>
        </div>
      )}
      {emailOpen && <EmailDialog tid={transcript.id} onClose={() => setEmailOpen(false)} />}
      <Dialog open={askModel} onOpenChange={(o) => !o && setAskModel(false)} title="Which model should write the summary?"
        description="Summaries don't follow the chat model. The model you pick is kept for this meeting."
        footer={<>
          <Button variant="ghost" onClick={() => setAskModel(false)}>Cancel</Button>
          <Button variant="primary" data-testid="summary-model-ok" disabled={!pick} onClick={async () => {
            setAskModel(false);
            setSummaryModel(pick);
            if (pickDefault) await updatePrefs({ summary_model: pick });
            generate(undefined, pick);
          }}>Write the summary</Button>
        </>}>
        <div className="space-y-3" data-testid="summary-model-dialog">
          <ModelSelect kind="summary" id="ask-summary-model" label="Model" defaultLabel="Choose a model…" value={pick} onChange={setPick}
            hint="On this Mac: nothing leaves the laptop. Cloud: you're asked before the transcript is sent." />
          <label className="flex items-center gap-2 text-[13px]">
            <input type="checkbox" checked={pickDefault} onChange={(e) => setPickDefault(e.target.checked)} data-testid="summary-model-default" />
            Use this model for all meetings (Settings → Meetings)
          </label>
        </div>
      </Dialog>
      <ConfirmDialog open={!!discard} onOpenChange={(o) => !o && setDiscard(null)} title="Discard your edits?" confirmLabel="Discard edits"
        description="Changing the type, detail level or sections re-creates the notes from the extracted data, so your inline edits are lost."
        onConfirm={() => { discard?.(); setDiscard(null); }} />
    </div>
  );
}
