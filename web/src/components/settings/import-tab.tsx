"use client";

/**
 * Settings → Import: import a claude.ai export (memory, people, conversations). Shows a preview first; you
 * choose what to import.
 */
import { Download, FileArchive, FileJson, FileText, MessageSquare, Trash2, Upload, UserRound } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { SectionTitle } from "@/components/settings/settings-dialog";
import { Badge, Button, Callout, ConfirmDialog, Textarea } from "@/components/ui";
import { api, type ImportPreview, type ImportStatus, type ItemStatus } from "@/lib/api";
import { clsx } from "@/lib/format";

const STATUS: Record<ItemStatus, { label: string; tone: "accent" | "warning" | "neutral" }> = {
  new: { label: "New", tone: "accent" },
  update: { label: "Update", tone: "warning" },
  move: { label: "Moves to its folder", tone: "neutral" },
  unchanged: { label: "Already imported", tone: "neutral" },
};

/** Which items are ticked, with toggle / all / none (new, updated and moved items start ticked). */
function useSelection(keys: string[], initial: (k: string) => boolean) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set(keys.filter(initial)));
  const toggle = (k: string) => setSelected((s) => { const n = new Set(s); if (n.has(k)) n.delete(k); else n.add(k); return n; });
  return { selected, toggle, all: () => setSelected(new Set(keys)), none: () => setSelected(new Set()) };
}

/** A group of preview items (memory, people or conversations) with All / None buttons. */
function Group({ title, icon, count, total, onAll, onNone, children, testid }: {
  title: string; icon: React.ReactNode; count: number; total: number; onAll: () => void; onNone: () => void;
  children: React.ReactNode; testid: string;
}) {
  if (!total) return null;
  return (
    <section className="rounded-xl border border-border" data-testid={testid}>
      <header className="flex items-center justify-between gap-2 border-b border-border px-3.5 py-2">
        <span className="flex items-center gap-2 text-[13px] font-medium">{icon} {title}
          <span className="font-normal text-muted" data-testid={`${testid}-count`}>{count} of {total} selected</span></span>
        <span className="flex gap-1">
          <Button size="sm" variant="ghost" onClick={onAll}>All</Button>
          <Button size="sm" variant="ghost" onClick={onNone}>None</Button>
        </span>
      </header>
      <ul className="max-h-64 divide-y divide-border overflow-y-auto">{children}</ul>
    </section>
  );
}

/** One preview item with its checkbox and status badge (New, Update, Moves…). */
function Row({ checked, onChange, label, children, status }: {
  checked: boolean; onChange: () => void; label: string; children: React.ReactNode; status: ItemStatus;
}) {
  return (
    <li>
      <label className="flex cursor-pointer items-start gap-2.5 px-3.5 py-2 hover:bg-hover/60">
        <input type="checkbox" checked={checked} onChange={onChange} aria-label={label} className="mt-0.5 accent-[var(--accent)]" />
        <span className="min-w-0 flex-1 text-[13px]">{children}</span>
        <Badge tone={STATUS[status].tone}>{STATUS[status].label}</Badge>
      </label>
    </li>
  );
}

/** The import preview and the Import button. */
function PreviewView({ preview, onDone, onCancel }: { preview: ImportPreview; onDone: () => void; onCancel: () => void }) {
  const { refreshSessions } = useApp();
  const memory = useSelection(preview.memory.map((m) => m.key), (k) => preview.memory.find((m) => m.key === k)?.status !== "unchanged");
  const people = useSelection(preview.people.map((p) => p.key), (k) => preview.people.find((p) => p.key === k)?.status !== "unchanged");
  const convs = useSelection(preview.conversations.map((c) => c.uuid), (k) => preview.conversations.find((c) => c.uuid === k)?.status !== "unchanged");
  const [busy, setBusy] = useState(false);
  const total = memory.selected.size + people.selected.size + convs.selected.size;

  const commit = async () => {
    setBusy(true);
    try {
      const res = await api.post<Record<string, number>>("/api/imports/claude/commit", {
        preview_id: preview.preview_id, memory: [...memory.selected], people: [...people.selected],
        conversations: [...convs.selected],
      });
      const added = res.memory_added + res.people_added + res.conversations_added;
      const updated = res.memory_updated + res.people_updated + res.conversations_updated;
      const tidied = [
        res.memory_moved ? `${res.memory_moved} moved to their folders` : "",
        res.memory_tidied ? `${res.memory_tidied} file-header lines removed` : "",
        res.entries_updated ? `${res.entries_updated} descriptions or aliases added` : "",
      ].filter(Boolean).join(" · ");
      toast.success(`Imported ${added} new item${added === 1 ? "" : "s"}${updated ? `, updated ${updated}` : ""}`, {
        description: `${res.conversations_added + res.conversations_updated} conversations · ${res.memory_added + res.memory_updated} memories · ${res.people_added + res.people_updated} people`
          + (tidied ? ` · ${tidied}` : ""),
      });
      await refreshSessions();
      onDone();
    } catch (err) {
      toast.error("Import failed", { description: (err as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const range = preview.date_range;
  return (
    <div className="space-y-3" data-testid="import-preview">
      <div className="flex flex-wrap items-center gap-2 text-[13px]" data-testid="import-summary">
        <Badge tone="accent">{preview.counts.conversations} conversations</Badge>
        <Badge tone="accent">{preview.counts.memory} memories</Badge>
        <Badge tone="accent">{preview.counts.people} people</Badge>
        {range && <span className="text-muted" data-testid="import-date-range">{range[0].slice(0, 10)} → {range[1].slice(0, 10)}</span>}
        {preview.account_name && <span className="text-muted">· account: {preview.account_name}</span>}
      </div>
      <ul className="flex flex-wrap gap-1.5 text-xs text-muted">
        {preview.files.map((f, i) => (
          <li key={`${i}-${f.name}`} title={f.name} className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-0.5" data-testid="import-file">
            {f.kind === "conversations" ? <FileJson className="h-3 w-3" /> : f.kind === "memory" ? <FileText className="h-3 w-3" /> : <FileArchive className="h-3 w-3" />}
            {f.name} · <span className={clsx(f.kind === "error" || f.kind === "unknown" ? "text-danger" : "")}>{f.kind}</span>
          </li>
        ))}
      </ul>
      {preview.warnings.length > 0 && (
        <Callout tone="warning" title={`${preview.warnings.length} warning${preview.warnings.length === 1 ? "" : "s"}`} data-testid="import-warnings">
          <ul className="list-disc pl-4">{preview.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
        </Callout>
      )}
      <Group title="Conversations" icon={<MessageSquare className="h-3.5 w-3.5" />} count={convs.selected.size} total={preview.conversations.length}
             onAll={convs.all} onNone={convs.none} testid="import-conversations">
        {preview.conversations.map((c, i) => (
          <Row key={`${i}-${c.uuid}`} checked={convs.selected.has(c.uuid)} onChange={() => convs.toggle(c.uuid)} label={`Import ${c.title}`} status={c.status}>
            <span className="font-medium">{c.title}</span>
            <span className="block text-xs text-muted">{c.created_at.slice(0, 10)} · {c.message_count} messages{c.project ? ` · ${c.project}` : ""}</span>
          </Row>
        ))}
      </Group>
      <Group title="Memory" icon={<FileText className="h-3.5 w-3.5" />} count={memory.selected.size} total={preview.memory.length}
             onAll={memory.all} onNone={memory.none} testid="import-memory">
        {preview.memory.map((m, i) => (
          <Row key={`${i}-${m.key}`} checked={memory.selected.has(m.key)} onChange={() => memory.toggle(m.key)} label={`Import memory: ${m.text}`} status={m.status}>
            {m.text}
            <span className="block text-xs text-muted">{m.section}{m.replaces ? ` · replaces “${m.replaces}”` : ""}</span>
          </Row>
        ))}
      </Group>
      <Group title="People" icon={<UserRound className="h-3.5 w-3.5" />} count={people.selected.size} total={preview.people.length}
             onAll={people.all} onNone={people.none} testid="import-people">
        {preview.people.map((p, i) => (
          <Row key={`${i}-${p.key}`} checked={people.selected.has(p.key)} onChange={() => people.toggle(p.key)} label={`Import person: ${p.name}`} status={p.status}>
            <span className="font-medium">{p.name}</span>{p.is_user && <span className="text-muted"> (you)</span>}
            <span className="block text-xs text-muted">
              {[p.role, p.team && `${p.team} team`, p.projects.length ? `projects: ${p.projects.join(", ")}` : "", ...p.relationships].filter(Boolean).join(" · ")}
              {p.changes.length ? ` · adds ${p.changes.join(", ")}` : ""}
            </span>
          </Row>
        ))}
      </Group>
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="primary" onClick={commit} loading={busy} disabled={!total} data-testid="import-commit">
          <Download className="h-4 w-4" /> Import {total} selected
        </Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
        <span className="text-xs text-muted">Stays on this Mac. Already-imported items are updated, never duplicated.</span>
      </div>
    </div>
  );
}

/** The Import tab: upload or paste, preview, import, and what's been imported so far. */
export function ImportTab() {
  const { refreshSessions } = useApp();
  const [files, setFiles] = useState<File[]>([]);
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [status, setStatus] = useState<ImportStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const loadStatus = () => api.get<ImportStatus>("/api/imports/claude/status").then(setStatus).catch(() => setStatus(null));
  useEffect(() => { loadStatus(); }, []);

  const runPreview = async () => {
    setBusy(true);
    try {
      const form = new FormData();
      files.forEach((f) => form.append("files", f));
      form.append("text", text);
      setPreview(await api.upload<ImportPreview>("/api/imports/claude/preview", form));
    } catch (err) {
      toast.error("Couldn't read the export", { description: (err as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const deleteAll = async () => {
    try {
      const res = await api.del<{ deleted: Record<string, number> }>("/api/imports/claude");
      toast.success("Deleted all imported data", { description: `${res.deleted.conversations} conversations · ${res.deleted.memory} memories · ${res.deleted.people} people` });
      await refreshSessions();
      loadStatus();
    } catch (err) {
      toast.error("Couldn't delete", { description: (err as Error).message });
    }
  };

  const fileList = useMemo(() => files.map((f) => f.name).join(", "), [files]);
  if (preview) {
    return (
      <div>
        <SectionTitle title="Import from Claude · preview" description="Check what was found and choose what to import." />
        <PreviewView preview={preview} onCancel={() => setPreview(null)}
                     onDone={() => { setPreview(null); setFiles([]); setText(""); loadStatus(); }} />
      </div>
    );
  }
  return (
    <div className="space-y-6">
      <SectionTitle title="Import from Claude"
        description="Bring your claude.ai memory and chats into Quorum. claude.ai has no API for this, so you export your data there and upload the files here. Nothing is sent anywhere." />
      <ol className="space-y-2 rounded-xl bg-surface-2 p-4 text-[13px]" data-testid="import-instructions">
        <li><span className="font-medium">Full export (memory + all chats):</span> claude.ai → Settings → Privacy → <span className="font-medium">Export data</span>. You get an email with a ZIP of JSON files; upload the ZIP as it is.</li>
        <li><span className="font-medium">Memory only:</span> claude.ai → Settings → memory → <span className="font-medium">export memory</span> (text/Markdown), then upload the file or paste the text below.</li>
        <li className="text-muted">You can also upload single JSON files from the ZIP (e.g. conversations.json). Re-importing a newer export updates what you imported before.</li>
      </ol>
      <div
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); setFiles([...files, ...Array.from(e.dataTransfer.files)]); }}
        className={clsx("rounded-xl border border-dashed p-5 text-center", dragging ? "border-accent bg-accent-soft" : "border-border-strong")}>
        <Upload className="mx-auto h-5 w-5 text-muted" />
        <p className="mt-2 text-[13px] text-muted">Drop the export ZIP, JSON files or memory text here, or</p>
        <Button size="sm" className="mt-2" onClick={() => fileRef.current?.click()}>Choose files</Button>
        <input ref={fileRef} type="file" multiple hidden accept=".zip,.json,.txt,.md,.markdown" data-testid="import-file-input"
               onChange={(e) => { setFiles([...files, ...Array.from(e.target.files ?? [])]); e.target.value = ""; }} />
        {files.length > 0 && (
          <p className="mt-2 text-xs" data-testid="import-chosen">{fileList} <button className="text-accent hover:underline" onClick={() => setFiles([])}>clear</button></p>
        )}
      </div>
      <div>
        <label htmlFor="memory-paste" className="mb-1 block text-[13px] font-medium">Or paste exported memory</label>
        <Textarea id="memory-paste" rows={4} value={text} onChange={(e) => setText(e.target.value)} data-testid="import-text"
                  placeholder={"**Work context**\n- Cian is the release manager for Project Atlas…"} />
      </div>
      <Button variant="primary" onClick={runPreview} loading={busy} disabled={!files.length && !text.trim()} data-testid="import-preview-button">
        Preview import
      </Button>
      <section className="border-t border-border pt-5">
        <SectionTitle title="Imported data" description="Memory appears in Settings → Memory; chats in the sidebar with an “Imported” tag." />
        {status && (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-[13px] text-muted" data-testid="import-status">
              {status.conversations} conversations · {status.memory} memories · {status.people} people
              {status.last_import ? ` · last import ${String(status.last_import.at).slice(0, 16).replace("T", " ")}` : ""}
            </p>
            <Button variant="outline" size="sm" onClick={() => setConfirm(true)} data-testid="delete-imported"
                    disabled={!status.conversations && !status.memory && !status.people}>
              <Trash2 className="h-3.5 w-3.5" /> Delete all imported data
            </Button>
          </div>
        )}
      </section>
      <ConfirmDialog open={confirm} onOpenChange={setConfirm} title="Delete all imported data?" confirmLabel="Delete all"
        description="Removes every chat, memory and person imported from claude.ai. People you also labelled in meetings are kept, without the imported details. This can't be undone."
        onConfirm={deleteAll} />
    </div>
  );
}
