"use client";

/**
 * Settings → Memory: everything Quorum remembers, organised like claude.ai memory (you, people, projects,
 * topics). Search, edit, merge duplicates, delete and export.
 */
import { AudioLines, Brain, ChevronDown, ChevronRight, Combine, Download, FolderKanban, Hash, MonitorPlay, Pencil, Plus,
  Search, Trash2, UserRound } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { SectionTitle, useActiveProject } from "@/components/settings/settings-dialog";
import { Badge, Button, Callout, ConfirmDialog, EmptyState, Input, Textarea } from "@/components/ui";
import { API_URL, api, type MemoryEntry, type MemoryFact, type MemoryResponse } from "@/lib/api";

const KINDS: { id: MemoryEntry["kind"]; label: string; icon: React.ReactNode }[] = [
  { id: "profile", label: "Profile", icon: <UserRound className="h-3.5 w-3.5" /> },
  { id: "person", label: "People", icon: <UserRound className="h-3.5 w-3.5" /> },
  { id: "project", label: "Projects", icon: <FolderKanban className="h-3.5 w-3.5" /> },
  { id: "area", label: "Areas", icon: <Hash className="h-3.5 w-3.5" /> },
];
const SOURCE_TONE: Record<string, "accent" | "neutral" | "success" | "warning"> = {
  "claude.ai": "accent", chat: "neutral", meeting: "success", user: "neutral", cloud_review: "warning",
};

/** One fact: its text (editable), heading, where it came from and when; can be selected for merging. */
function FactRow({ entry, fact, sources, selected, onSelect, onChange }: {
  entry: MemoryEntry; fact: MemoryFact; sources: Record<string, string>; selected: boolean;
  onSelect: () => void; onChange: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(fact.text);
  const save = async () => {
    try {
      await api.patch(`/api/memory/entries/${entry.id}/facts/${fact.id}`, { text });
      setEditing(false);
      onChange();
    } catch (err) {
      toast.error("Couldn't save", { description: (err as Error).message });
    }
  };
  return (
    <li className="flex items-start gap-2.5 rounded-lg px-2 py-1.5 text-[13px] hover:bg-hover" data-testid="memory-fact">
      <input type="checkbox" checked={selected} onChange={onSelect} aria-label={`Select: ${fact.text}`} className="mt-1" />
      <div className="min-w-0 flex-1">
        {editing ? (
          <div className="space-y-2">
            <Textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} aria-label="Edit fact" autoFocus />
            <div className="flex gap-2"><Button size="sm" variant="primary" onClick={save}>Save</Button>
              <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>Cancel</Button></div>
          </div>
        ) : <p>{fact.text}</p>}
        <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-muted">
          <Badge tone={SOURCE_TONE[fact.source] ?? "neutral"}>{sources[fact.source] ?? fact.source}</Badge>
          {fact.date && <span>{fact.date.slice(0, 10)}</span>}
          {fact.edited && <span>· edited</span>}
        </div>
      </div>
      {!editing && (
        <div className="flex gap-1">
          <Button size="icon-sm" variant="ghost" aria-label={`Edit fact: ${fact.text}`} onClick={() => setEditing(true)}><Pencil className="h-3.5 w-3.5" /></Button>
          <Button size="icon-sm" variant="ghost" aria-label={`Delete fact: ${fact.text}`}
                  onClick={async () => { await api.del(`/api/memory/entries/${entry.id}/facts/${fact.id}`); onChange(); }}>
            <Trash2 className="h-3.5 w-3.5" /></Button>
        </div>
      )}
    </li>
  );
}

/** Form for an entry's name, description, aliases and (for people) role and team. */
function EntryEditor({ entry, onDone }: { entry: MemoryEntry; onDone: (saved: boolean) => void }) {
  const [draft, setDraft] = useState({ name: entry.name, description: entry.description, aliases: entry.aliases.join(", "),
    role: entry.role ?? "", team: entry.team ?? "", projects: (entry.projects ?? []).join(", ") });
  const list = (v: string) => v.split(",").map((x) => x.trim()).filter(Boolean);
  const save = async () => {
    try {
      const fields: Record<string, unknown> = { name: draft.name, description: draft.description, aliases: list(draft.aliases) };
      if (entry.kind === "person") Object.assign(fields, { role: draft.role, team: draft.team, projects: list(draft.projects) });
      await api.patch(`/api/memory/entries/${entry.id}`, fields);
      onDone(true);
    } catch (err) {
      toast.error("Couldn't save", { description: (err as Error).message });
    }
  };
  return (
    <div className="space-y-2 rounded-lg border border-accent/40 p-3">
      <div className="grid gap-2 sm:grid-cols-2">
        <Input value={draft.name} aria-label="Name" onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
        <Input value={draft.aliases} aria-label="Aliases" placeholder="Also known as, comma-separated" onChange={(e) => setDraft({ ...draft, aliases: e.target.value })} />
        {entry.kind === "person" && <>
          <Input value={draft.role} aria-label="Role" placeholder="Role" onChange={(e) => setDraft({ ...draft, role: e.target.value })} />
          <Input value={draft.team} aria-label="Team" placeholder="Team" onChange={(e) => setDraft({ ...draft, team: e.target.value })} />
          <Input value={draft.projects} aria-label="Projects" placeholder="Projects, comma-separated" className="sm:col-span-2"
                 onChange={(e) => setDraft({ ...draft, projects: e.target.value })} />
        </>}
      </div>
      <Textarea rows={2} value={draft.description} aria-label="Description" placeholder="One line about this entry"
                onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
      <div className="flex gap-2"><Button size="sm" variant="primary" onClick={save}>Save</Button>
        <Button size="sm" variant="ghost" onClick={() => onDone(false)}>Cancel</Button></div>
    </div>
  );
}

/**
 * One entry (a person, project or topic), collapsible, with its facts and actions (edit, merge, delete,
 * forget voice).
 */
function EntryCard({ entry, data, open, onToggle, onChange }: {
  entry: MemoryEntry; data: MemoryResponse; open: boolean; onToggle: () => void; onChange: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [confirm, setConfirm] = useState<"delete" | "voice" | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [fact, setFact] = useState("");
  const groups = useMemo(() => {
    const out = new Map<string, MemoryFact[]>();
    for (const f of entry.facts) out.set(f.heading || "", [...(out.get(f.heading || "") ?? []), f]);
    return [...out.entries()];
  }, [entry.facts]);
  const duplicateFacts = data.duplicates.facts[entry.id] ?? [];
  const addFact = async () => {
    if (!fact.trim()) return;
    await api.post(`/api/memory/entries/${entry.id}/facts`, { text: fact });
    setFact("");
    onChange();
  };
  const mergeFacts = async (ids: string[]) => {
    await api.post(`/api/memory/entries/${entry.id}/facts/merge`, { ids });
    setSelected(new Set());
    toast.success("Merged");
    onChange();
  };
  const isPerson = entry.kind === "person";
  const details = isPerson ? [entry.role, entry.team && `${entry.team} team`,
    entry.projects?.length ? `projects: ${entry.projects.join(", ")}` : "", ...(entry.relationships ?? [])].filter(Boolean) : [];
  const screen = [...new Map((entry.screen_names ?? []).map((s) => [s.name, s])).values()];

  return (
    <div className="rounded-xl border border-border" data-testid={isPerson ? "person-row" : "memory-entry"} data-kind={entry.kind}>
      <div className="flex items-start justify-between gap-3 px-3.5 py-2.5">
        <button className="flex min-w-0 flex-1 items-start gap-2 text-left" onClick={onToggle} aria-expanded={open}
                aria-label={`${open ? "Collapse" : "Expand"} ${entry.name}`}>
          {open ? <ChevronDown className="mt-0.5 h-4 w-4 shrink-0 text-muted" /> : <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-muted" />}
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
              {entry.kind === "area" && entry.project ? <span className="font-normal text-muted">{entry.project} ›</span> : null}
              {entry.name}
              {isPerson && entry.role && <span className="font-normal text-muted">· {entry.role}</span>}
              {entry.is_user && <Badge>You</Badge>}
              <Badge>{entry.facts.length} fact{entry.facts.length === 1 ? "" : "s"}</Badge>
            </div>
            {entry.description && <p className="mt-0.5 text-xs text-muted">{entry.description}</p>}
            {details.length > 0 && <p className="mt-0.5 text-xs text-muted">{details.join(" · ")}</p>}
            {isPerson && (
              <div className="mt-1 flex flex-wrap gap-1.5">
                <Badge>{entry.meetings?.length ?? 0} meeting{entry.meetings?.length === 1 ? "" : "s"}</Badge>
                {(entry.voiceprint_count ?? 0) > 0 && (
                  <Badge tone="accent" title={Object.entries(entry.voiceprint_sources ?? {}).map(([k, v]) => `${v} from ${k}`).join(", ")}>
                    <AudioLines className="h-3 w-3" /> Voice known · {entry.voiceprint_count} fingerprint{entry.voiceprint_count === 1 ? "" : "s"}</Badge>
                )}
                {screen.length > 0 && <Badge title="Names read on their video tile"><MonitorPlay className="h-3 w-3" /> On screen: {screen.map((s) => s.name).join(", ")}</Badge>}
                {entry.source && <Badge tone="accent"><Download className="h-3 w-3" /> {entry.source}</Badge>}
                {entry.aliases.length > 0 && <Badge title="Also known as">aka {entry.aliases.join(", ")}</Badge>}
              </div>
            )}
          </div>
        </button>
        <div className="flex gap-1">
          {isPerson && (entry.voiceprint_count ?? 0) > 0 && (
            <Button size="sm" variant="ghost" onClick={() => setConfirm("voice")} aria-label={`Forget ${entry.name}'s voice`}>
              <AudioLines className="h-3.5 w-3.5" /> Forget voice</Button>
          )}
          <Button size="icon-sm" variant="ghost" aria-label={`Edit ${entry.name}`} onClick={() => { setEditing(true); if (!open) onToggle(); }}><Pencil className="h-3.5 w-3.5" /></Button>
          {entry.kind !== "profile" && <Button size="icon-sm" variant="ghost" aria-label={`Forget ${entry.name}`} onClick={() => setConfirm("delete")}><Trash2 className="h-3.5 w-3.5" /></Button>}
        </div>
      </div>
      {open && (
        <div className="space-y-3 border-t border-border px-3.5 py-3">
          {editing && <EntryEditor entry={entry} onDone={(saved) => { setEditing(false); if (saved) onChange(); }} />}
          {duplicateFacts.length > 0 && (
            <div className="space-y-1 rounded-lg bg-warning-soft p-2.5 text-[12px]" data-testid="fact-duplicates">
              {duplicateFacts.map((group) => (
                <div key={group.join()} className="flex items-center justify-between gap-2">
                  <span className="text-muted">{group.length} similar facts</span>
                  <Button size="sm" variant="secondary" onClick={() => mergeFacts(group)}><Combine className="h-3.5 w-3.5" /> Merge</Button>
                </div>
              ))}
            </div>
          )}
          {groups.length === 0 ? <p className="text-[13px] text-muted">No facts yet.</p> : groups.map(([heading, facts]) => (
            <div key={heading}>
              {heading && <div className="mb-1 text-xs font-semibold text-muted">{heading}</div>}
              <ul className="space-y-0.5">
                {facts.map((f) => (
                  <FactRow key={f.id} entry={entry} fact={f} sources={data.sources} selected={selected.has(f.id)} onChange={onChange}
                           onSelect={() => setSelected((s) => { const n = new Set(s); if (n.has(f.id)) n.delete(f.id); else n.add(f.id); return n; })} />
                ))}
              </ul>
            </div>
          ))}
          {selected.size >= 2 && <Button size="sm" onClick={() => mergeFacts([...selected])} data-testid="merge-selected"><Combine className="h-3.5 w-3.5" /> Merge {selected.size} facts</Button>}
          <div className="flex gap-2">
            <Input placeholder="Add a fact" aria-label={`New fact for ${entry.name}`} value={fact}
                   onChange={(e) => setFact(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addFact()} />
            <Button onClick={addFact} disabled={!fact.trim()}><Plus className="h-4 w-4" /> Add</Button>
          </div>
        </div>
      )}
      <ConfirmDialog open={confirm === "delete"} onOpenChange={(o) => setConfirm(o ? "delete" : null)} title={`Forget ${entry.name}?`}
        confirmLabel="Forget" description={isPerson ? "Their facts, role, notes and voice fingerprints are deleted. Transcripts keep the names already given."
          : "This entry and its facts are deleted."}
        onConfirm={async () => { await api.del(`/api/memory/entries/${entry.id}`); onChange(); }} />
      <ConfirmDialog open={confirm === "voice"} onOpenChange={(o) => setConfirm(o ? "voice" : null)} title={`Forget ${entry.name}'s voice?`}
        confirmLabel="Forget voice" description="Their voice fingerprints are deleted. Later meetings won't recognise them by voice until they're labelled again."
        onConfirm={async () => { await api.del(`/api/people/${entry.id}/voice`); toast.success("Voice forgotten"); onChange(); }} />
    </div>
  );
}

/** The Memory tab: counts, search, the entries by kind, and export as a claude.ai ZIP. */
export function MemoryTab() {
  const { project, save } = useActiveProject();
  const [data, setData] = useState<MemoryResponse | null>(null);
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<MemoryEntry["kind"] | "all">("all");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [fact, setFact] = useState("");
  const [newPerson, setNewPerson] = useState({ name: "", role: "" });
  const [newEntry, setNewEntry] = useState<{ kind: "project" | "area"; name: string }>({ kind: "area", name: "" });
  const load = useCallback(() => api.get<MemoryResponse>("/api/memory").then(setData).catch(() => setData(null)), []);
  useEffect(() => { load(); }, [load]);

  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  const matches = (e: MemoryEntry) => !terms.length || terms.every((t) =>
    [e.name, e.description, e.role, e.team, ...(e.aliases ?? []), ...(e.projects ?? []), ...e.facts.map((f) => `${f.text} ${f.heading}`)]
      .join(" ").toLowerCase().includes(t));
  const shown = (data?.entries ?? []).filter((e) => (kind === "all" || e.kind === kind) && matches(e));
  const nameOf = (id: string) => data?.entries.find((e) => e.id === id)?.name ?? id;

  const addProjectFact = async () => {
    if (!project || !fact.trim()) return;
    try {
      await save({ memory: [...project.memory, fact.trim()] });
      setFact("");
      load();
    } catch (err) {
      toast.error("Couldn't save", { description: (err as Error).message });
    }
  };
  const addPerson = async () => {
    if (!newPerson.name.trim()) return;
    try {
      await api.post("/api/people", newPerson);
      setNewPerson({ name: "", role: "" });
      load();
    } catch (err) {
      toast.error("Couldn't add", { description: (err as Error).message });
    }
  };
  const addEntry = async () => {
    try {
      await api.post("/api/memory/entries", newEntry);
      setNewEntry({ ...newEntry, name: "" });
      load();
    } catch (err) {
      toast.error("Couldn't add", { description: (err as Error).message });
    }
  };
  const mergeEntries = async (ids: string[]) => {
    const byId = new Map((data?.entries ?? []).map((e) => [e.id, e]));
    const keep = [...ids].sort((a, b) => (byId.get(b)?.name.length ?? 0) - (byId.get(a)?.name.length ?? 0))[0];
    await api.post("/api/memory/merge", { keep, ids: ids.filter((id) => id !== keep) });
    toast.success(`Merged into ${byId.get(keep)?.name}`);
    load();
  };
  const toggle = (id: string) => setOpen((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });

  return (
    <div className="space-y-6">
      <SectionTitle title="Memory" description={<>One store on this Mac, organised like claude.ai memory: you, people, projects and topics.
        Each fact shows where it came from. Used in chats, to identify speakers and in meeting notes.</>}
        action={<a href={`${API_URL}/api/memory/export`} download><Button size="sm"><Download className="h-3.5 w-3.5" /> Export ZIP</Button></a>} />

      {data?.backup && (
        <Callout tone="info" title="Memory was combined into one store">
          People, imported claude.ai memory and project facts now live here. The previous files were backed up to {data.backup}.
        </Callout>
      )}

      <section>
        <SectionTitle title={`This project${project ? ` · ${project.name}` : ""}`} description="Facts the assistant uses in this project's chats. New ones are added automatically from chats." />
        {project && (project.memory.length === 0 ? <p className="text-[13px] text-muted">Nothing remembered yet.</p> : (
          <ul className="space-y-1.5" data-testid="memory-list">
            {project.memory.map((m, i) => (
              <li key={`${i}-${m}`} className="flex items-start justify-between gap-3 rounded-lg border border-border px-3 py-2 text-[13px]">
                <span>{m}</span>
                <Button size="icon-sm" variant="ghost" aria-label={`Forget: ${m}`}
                        onClick={async () => { await save({ memory: project.memory.filter((_, j) => j !== i) }); load(); }}><Trash2 className="h-3.5 w-3.5" /></Button>
              </li>
            ))}
          </ul>
        ))}
        <div className="mt-3 flex gap-2">
          <Input placeholder="Add a fact, e.g. “Sprint reviews are on Fridays”" aria-label="New fact" value={fact} disabled={!project}
                 onChange={(e) => setFact(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addProjectFact()} />
          <Button onClick={addProjectFact} disabled={!project || !fact.trim()}><Plus className="h-4 w-4" /> Add</Button>
        </div>
      </section>

      <section className="space-y-3">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted" />
            <Input className="pl-8" placeholder="Search memory" aria-label="Search memory" value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
          <div className="flex flex-wrap gap-1" role="tablist" aria-label="Memory kind">
            {[{ id: "all" as const, label: "All" }, ...KINDS].map((k) => (
              <button key={k.id} role="tab" aria-selected={kind === k.id} onClick={() => setKind(k.id)}
                      className={`rounded-full border px-2.5 py-1 text-xs ${kind === k.id ? "border-accent bg-accent-soft text-accent" : "border-border text-muted hover:bg-hover"}`}>
                {k.label}{k.id !== "all" && data ? ` ${data.counts[k.id] ?? 0}` : ""}
              </button>
            ))}
          </div>
        </div>

        {data && data.duplicates.entries.length > 0 && (
          <div className="space-y-1.5 rounded-xl bg-warning-soft p-3 text-[13px]" data-testid="people-duplicates">
            <div className="font-medium">Possibly the same</div>
            {data.duplicates.entries.map((group) => (
              <div key={group.join()} className="flex items-center justify-between gap-2">
                <span className="text-muted">{group.map(nameOf).join(" · ")}</span>
                <Button size="sm" variant="secondary" onClick={() => mergeEntries(group)} data-testid="merge-people"><Combine className="h-3.5 w-3.5" /> Merge</Button>
              </div>
            ))}
          </div>
        )}

        {data === null ? <div className="skeleton h-24" /> : shown.length === 0 ? (
          <EmptyState icon={<Brain className="h-5 w-5" />} title={query ? "Nothing matches" : "Nothing remembered yet"}>
            Import your claude.ai memory, label speakers in a meeting, or add people and facts here.
          </EmptyState>
        ) : (
          KINDS.filter((k) => kind === "all" || k.id === kind).map((k) => {
            const list = shown.filter((e) => e.kind === k.id);
            if (!list.length) return null;
            return (
              <div key={k.id} className="space-y-2" data-testid={`memory-group-${k.id}`}>
                <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted">{k.icon} {k.label}</div>
                {list.map((e) => <EntryCard key={e.id} entry={e} data={data} open={open.has(e.id) || terms.length > 0}
                                            onToggle={() => toggle(e.id)} onChange={load} />)}
              </div>
            );
          })
        )}

        <div className="grid gap-3 rounded-xl border border-border p-3.5 sm:grid-cols-2">
          <div className="space-y-2">
            <div className="text-xs font-medium text-muted">Add a person</div>
            <Input placeholder="Name" aria-label="Person name" value={newPerson.name} onChange={(e) => setNewPerson({ ...newPerson, name: e.target.value })} />
            <Input placeholder="Role (e.g. QA lead)" aria-label="Person role" value={newPerson.role} onChange={(e) => setNewPerson({ ...newPerson, role: e.target.value })}
                   onKeyDown={(e) => e.key === "Enter" && addPerson()} />
            <Button onClick={addPerson} disabled={!newPerson.name.trim()}><Plus className="h-4 w-4" /> Add person</Button>
          </div>
          <div className="space-y-2">
            <div className="text-xs font-medium text-muted">Add a project or topic</div>
            <select aria-label="Entry kind" value={newEntry.kind} onChange={(e) => setNewEntry({ ...newEntry, kind: e.target.value as "project" | "area" })}
                    className="h-9 w-full rounded-lg border border-border bg-surface px-2.5 text-sm">
              <option value="area">Topic (area)</option>
              <option value="project">Project</option>
            </select>
            <Input placeholder="Name" aria-label="Entry name" value={newEntry.name} onChange={(e) => setNewEntry({ ...newEntry, name: e.target.value })}
                   onKeyDown={(e) => e.key === "Enter" && newEntry.name.trim() && addEntry()} />
            <Button onClick={addEntry} disabled={!newEntry.name.trim()}><Plus className="h-4 w-4" /> Add</Button>
          </div>
        </div>
      </section>
    </div>
  );
}
