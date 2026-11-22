"use client";

/**
 * Settings → Skills: the built-in meeting-notes skill (on/off, its templates) and the project's own
 * skills.
 */
import { FileCode2, NotebookPen, Pencil, Plus, Puzzle, RotateCcw, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { Markdown } from "@/components/markdown";
import { SectionTitle, useActiveProject } from "@/components/settings/settings-dialog";
import { Badge, Button, Dialog, EmptyState, Input, Label, Switch, Textarea } from "@/components/ui";
import { api, type BuiltinSkill, type Skill } from "@/lib/api";

/** Edit one notes template; Reset goes back to the built-in version. */
function TemplateEditor({ name, label, onClose, onSaved }: { name: string; label: string; onClose: () => void; onSaved: () => void }) {
  const [text, setText] = useState<string | null>(null);
  const [customized, setCustomized] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.get<{ text: string; customized: boolean }>(`/api/skills/meeting-notes/templates/${name}`)
      .then((t) => { setText(t.text); setCustomized(t.customized); }).catch(() => setText(""));
  }, [name]);
  const save = async () => {
    setBusy(true);
    try {
      await api.put(`/api/skills/meeting-notes/templates/${name}`, { text });
      toast.success(`Template “${label}” saved`, { description: "Used the next time notes are generated or re-rendered." });
      onSaved();
      onClose();
    } catch (err) {
      toast.error("Couldn't save the template", { description: (err as Error).message });
    } finally {
      setBusy(false);
    }
  };
  const reset = async () => {
    const t = await api.del<{ text: string; customized: boolean }>(`/api/skills/meeting-notes/templates/${name}`);
    setText(t.text);
    setCustomized(false);
    onSaved();
    toast.success("Reset to the default template");
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} title={`Template: ${label}`} className="max-w-3xl"
      description={<>Markdown with placeholders: <code>{"{{meeting.title}}"}</code>, sections <code>{"{{#decisions}}…{{/decisions}}"}</code>, shared parts <code>{"{{> actions}}"}</code>. The front matter sets the label, detection keywords and guidance for the model.</>}
      footer={<>
        {customized && <Button variant="ghost" onClick={reset} data-testid="reset-template"><RotateCcw className="h-3.5 w-3.5" /> Reset to default</Button>}
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" onClick={save} loading={busy} disabled={!text?.trim()} data-testid="save-template">Save template</Button>
      </>}>
      {text === null ? <div className="skeleton h-64" /> : (
        <Textarea value={text} onChange={(e) => setText(e.target.value)} rows={18} spellCheck={false} aria-label="Template text"
                  data-testid="template-text" className="font-mono text-xs leading-5" />
      )}
    </Dialog>
  );
}

/** The built-in skills, with their templates. */
function BuiltinSkills() {
  const { updatePrefs } = useApp();
  const [skill, setSkill] = useState<BuiltinSkill | null>(null);
  const [view, setView] = useState(false);
  const [editing, setEditing] = useState<{ name: string; label: string } | null>(null);
  const load = () => api.get<BuiltinSkill[]>("/api/skills/builtin").then((l) => setSkill(l[0] ?? null)).catch(() => setSkill(null));
  useEffect(() => { load(); }, []);
  if (!skill) return null;
  const toggle = async (enabled: boolean) => {
    setSkill(await api.patch<BuiltinSkill>("/api/skills/meeting-notes", { enabled }));
    await updatePrefs({});
    toast.success(enabled ? "Meeting notes skill on" : "Meeting notes skill off",
      { description: enabled ? "Summaries use the Professional Meeting Notes format." : "Summaries use simple free-form minutes." });
  };
  const types = skill.templates.filter((t) => t.type !== "partial");
  const partials = skill.templates.filter((t) => t.type === "partial");
  return (
    <section className="mb-8" data-testid="builtin-skill">
      <SectionTitle title="Built-in" description="Skills that ship with Quorum." />
      <div className="rounded-xl border border-border p-4">
        <div className="flex items-start gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-accent-soft text-accent"><NotebookPen className="h-4 w-4" /></div>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2 text-sm font-semibold">{skill.name}
              <Badge tone="accent">Default for meeting summaries</Badge>{!skill.enabled && <Badge tone="warning">Off</Badge>}</div>
            <p className="mt-0.5 text-[13px] text-muted">{skill.description}</p>
            <p className="mt-1 text-xs text-faint">Folder: {skill.folder} (SKILL.md, schema.json, templates/)</p>
          </div>
        </div>
        <div className="mt-2 border-t border-border">
          <Switch id="notes-skill" checked={skill.enabled} onChange={toggle} label="Use for meeting summaries"
                  description="Structured JSON from any model, validated, then rendered with the template for the meeting type." />
        </div>
        <div className="mt-2 flex flex-wrap gap-2">
          <Button size="sm" variant="secondary" onClick={() => setView(true)} data-testid="view-skill"><FileCode2 className="h-3.5 w-3.5" /> View instructions</Button>
        </div>
        <div className="mt-4">
          <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-faint">Templates · meeting types</div>
          <ul className="grid gap-1.5 sm:grid-cols-2">
            {types.map((t) => (
              <li key={t.name} className="flex items-center justify-between gap-2 rounded-lg border border-border px-3 py-1.5 text-[13px]" data-testid="template-row">
                <span className="truncate">{t.label}{t.customized && <Badge tone="warning" className="ml-1.5">Customised</Badge>}</span>
                <Button size="icon-sm" variant="ghost" aria-label={`Edit template ${t.label}`} onClick={() => setEditing({ name: t.name, label: t.label })}><Pencil className="h-3.5 w-3.5" /></Button>
              </li>
            ))}
          </ul>
          <div className="mb-1.5 mt-3 text-xs font-semibold uppercase tracking-wide text-faint">Shared sections</div>
          <div className="flex flex-wrap gap-1.5">
            {partials.map((t) => (
              <button key={t.name} onClick={() => setEditing({ name: t.name, label: t.label })} aria-label={`Edit section ${t.label}`}
                      className="rounded-md border border-border px-2 py-0.5 text-xs text-muted hover:bg-hover hover:text-fg">
                {t.label}{t.customized ? " •" : ""}
              </button>
            ))}
          </div>
        </div>
      </div>
      <Dialog open={view} onOpenChange={setView} title={skill.name} className="max-w-3xl" description="SKILL.md — the instructions every model follows.">
        <div className="max-h-[60vh] overflow-y-auto" data-testid="skill-instructions"><Markdown>{skill.instructions}</Markdown>
          <details className="mt-4 text-xs"><summary className="cursor-pointer text-muted">schema.json</summary>
            <pre className="mt-2 overflow-x-auto rounded-lg bg-surface-2 p-3">{JSON.stringify(skill.schema, null, 2)}</pre></details>
        </div>
      </Dialog>
      {editing && <TemplateEditor name={editing.name} label={editing.label} onClose={() => setEditing(null)} onSaved={load} />}
    </section>
  );
}

const EMPTY: Skill = { name: "", description: "", content: "" };

/** The Skills tab: built-in skills and the project's skills (add, edit, delete). */
export function SkillsTab() {
  const { project, save } = useActiveProject();
  const [editing, setEditing] = useState<{ index: number | null; skill: Skill } | null>(null);
  const skills = project?.skills ?? [];

  const commit = async () => {
    if (!editing || !editing.skill.name.trim()) return;
    const next = [...skills];
    if (editing.index === null) next.push(editing.skill);
    else next[editing.index] = editing.skill;
    try {
      await save({ skills: next });
      setEditing(null);
      toast.success("Skill saved");
    } catch (err) {
      toast.error("Couldn't save", { description: (err as Error).message });
    }
  };

  return (
    <div>
      <BuiltinSkills />
      <SectionTitle title={`Skills${project ? ` · ${project.name}` : ""}`}
        description="Reusable instructions. The assistant loads one when it fits the request, or type / in the message box to pick one."
        action={!editing && project && <Button size="sm" variant="primary" onClick={() => setEditing({ index: null, skill: EMPTY })}><Plus className="h-3.5 w-3.5" /> New skill</Button>} />
      {editing && (
        <div className="mb-4 space-y-3 rounded-xl border border-accent/40 p-4" data-testid="skill-editor">
          <div>
            <Label htmlFor="s-name">Name</Label>
            <Input id="s-name" value={editing.skill.name} placeholder="e.g. Action items"
                   onChange={(e) => setEditing({ ...editing, skill: { ...editing.skill, name: e.target.value } })} />
          </div>
          <div>
            <Label htmlFor="s-desc" hint="When should the assistant use it?">Description</Label>
            <Input id="s-desc" value={editing.skill.description}
                   onChange={(e) => setEditing({ ...editing, skill: { ...editing.skill, description: e.target.value } })} />
          </div>
          <div>
            <Label htmlFor="s-content">Instructions</Label>
            <Textarea id="s-content" rows={6} value={editing.skill.content}
                      onChange={(e) => setEditing({ ...editing, skill: { ...editing.skill, content: e.target.value } })} />
          </div>
          <div className="flex gap-2">
            <Button variant="primary" onClick={commit} disabled={!editing.skill.name.trim()}>Save skill</Button>
            <Button variant="ghost" onClick={() => setEditing(null)}>Cancel</Button>
          </div>
        </div>
      )}
      {skills.length === 0 && !editing ? (
        <EmptyState icon={<Puzzle className="h-5 w-5" />} title="No skills yet">
          The built-in “Meeting minutes” skill is always available when a transcript is attached.
        </EmptyState>
      ) : (
        <div className="space-y-2">
          {skills.map((s, i) => (
            <div key={`${s.name}-${i}`} className="flex items-start justify-between gap-3 rounded-xl border border-border px-3.5 py-2.5" data-testid="skill-row">
              <div className="min-w-0">
                <div className="text-sm font-medium">/{s.name}</div>
                {s.description && <p className="text-xs text-muted">{s.description}</p>}
              </div>
              <div className="flex gap-1">
                <Button size="icon-sm" variant="ghost" aria-label={`Edit ${s.name}`} onClick={() => setEditing({ index: i, skill: s })}><Pencil className="h-3.5 w-3.5" /></Button>
                <Button size="icon-sm" variant="ghost" aria-label={`Delete ${s.name}`} onClick={() => save({ skills: skills.filter((_, j) => j !== i) })}><Trash2 className="h-3.5 w-3.5" /></Button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
