"use client";

/**
 * Settings → Projects: create, rename, switch and delete projects, and edit a project's instructions and
 * documents.
 */
import { Check, FileText, Plus, Trash2, Upload } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { SectionTitle, useActiveProject } from "@/components/settings/settings-dialog";
import { Badge, Button, Callout, ConfirmDialog, Input, Label, Textarea } from "@/components/ui";
import { api, type Project } from "@/lib/api";
import { clsx } from "@/lib/format";

/**
 * The Projects tab. Documents added here go into every chat of the project (meeting transcripts excepted:
 * attach those to a chat).
 */
export function ProjectsTab() {
  const { updatePrefs, refreshSessions } = useApp();
  const [list, setList] = useState<Project[]>([]);
  const { project, reload, save } = useActiveProject();
  const [name, setName] = useState("");
  const [draft, setDraft] = useState({ name: "", instructions: "", confidential_terms: "" });
  const [deleting, setDeleting] = useState<Project | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const loadList = () => api.get<Project[]>("/api/projects").then(setList).catch(() => setList([]));
  useEffect(() => { loadList(); }, []);
  useEffect(() => {
    if (project) setDraft({ name: project.name, instructions: project.instructions, confidential_terms: project.confidential_terms.join(", ") });
  }, [project]);

  const activate = async (id: string) => {
    await updatePrefs({ active_project: id });
    await Promise.all([reload(), loadList(), refreshSessions()]);
    toast.success("Project switched");
  };
  const create = async () => {
    if (!name.trim()) return;
    try {
      const p = await api.post<Project>("/api/projects", { name });
      setName("");
      await activate(p.id);
    } catch (err) {
      toast.error("Couldn't create the project", { description: (err as Error).message });
    }
  };
  const saveDetails = async () => {
    try {
      await save({ name: draft.name, instructions: draft.instructions,
                   confidential_terms: draft.confidential_terms.split(",").map((t) => t.trim()).filter(Boolean) });
      loadList();
      toast.success("Project saved");
    } catch (err) {
      toast.error("Couldn't save", { description: (err as Error).message });
    }
  };
  const upload = async (files: FileList | null) => {
    if (!project || !files?.length) return;
    for (const file of Array.from(files)) {
      const form = new FormData();
      form.append("file", file);
      try {
        await api.upload(`/api/projects/${project.id}/documents`, form);
        toast.success(`${file.name} added to the project`);
      } catch (err) {
        toast.error(`Couldn't add ${file.name}`, { description: (err as Error).message });
      }
    }
    reload();
  };
  const removeDoc = async (doc: string) => {
    if (!project) return;
    await api.del(`/api/projects/${project.id}/documents/${encodeURIComponent(doc)}`);
    reload();
  };
  const remove = async (p: Project) => {
    try {
      await api.del(`/api/projects/${p.id}`);
      await Promise.all([reload(), loadList(), refreshSessions()]);
      toast.success(`Deleted ${p.name}`);
    } catch (err) {
      toast.error("Couldn't delete", { description: (err as Error).message });
    }
  };

  return (
    <div className="space-y-8">
      <section>
        <SectionTitle title="Projects" description="Each project has its own chats, instructions, documents, memory and skills." />
        <div className="space-y-1.5" data-testid="project-list">
          {list.map((p) => (
            <div key={p.id} className={clsx("flex items-center justify-between rounded-xl border px-3.5 py-2.5",
              p.active ? "border-accent/40 bg-accent-soft" : "border-border")}>
              <div className="min-w-0">
                <div className="flex items-center gap-2 text-sm font-medium">{p.name} {p.active && <Badge tone="accent"><Check className="h-3 w-3" /> Active</Badge>}</div>
              </div>
              <div className="flex gap-1">
                {!p.active && <Button size="sm" variant="secondary" onClick={() => activate(p.id)}>Switch</Button>}
                {p.id !== "default" && <Button size="icon-sm" variant="ghost" aria-label={`Delete ${p.name}`} onClick={() => setDeleting(p)}><Trash2 className="h-3.5 w-3.5" /></Button>}
              </div>
            </div>
          ))}
        </div>
        <div className="mt-3 flex gap-2">
          <Input placeholder="New project name" value={name} aria-label="New project name" onChange={(e) => setName(e.target.value)}
                 onKeyDown={(e) => e.key === "Enter" && create()} />
          <Button variant="primary" onClick={create} disabled={!name.trim()}><Plus className="h-4 w-4" /> Create</Button>
        </div>
      </section>

      {project && (
        <section className="space-y-4">
          <SectionTitle title={`${project.name} settings`} />
          <div>
            <Label htmlFor="p-name">Name</Label>
            <Input id="p-name" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
          </div>
          <div>
            <Label htmlFor="p-instructions" hint="Added to every chat in this project">Instructions</Label>
            <Textarea id="p-instructions" rows={4} value={draft.instructions}
                      placeholder="e.g. You support the mobile payments team. Answer in British English."
                      onChange={(e) => setDraft({ ...draft, instructions: e.target.value })} />
          </div>
          <div>
            <Label htmlFor="p-terms" hint="Comma-separated; treated as confidential">Confidential terms</Label>
            <Input id="p-terms" value={draft.confidential_terms} placeholder="Client names, project codenames"
                   onChange={(e) => setDraft({ ...draft, confidential_terms: e.target.value })} />
          </div>
          <Button variant="primary" onClick={saveDetails}>Save project</Button>

          <div>
            <Label>Context documents</Label>
            {project.documents.length === 0 ? <p className="text-[13px] text-muted">No documents. Add files the assistant should always know about.</p> : (
              <ul className="space-y-1">
                {project.documents.map((d) => (
                  <li key={d.name} className="flex items-center justify-between rounded-lg border border-border px-3 py-1.5 text-[13px]">
                    <span className="flex items-center gap-2"><FileText className="h-3.5 w-3.5 text-muted" />{d.name}
                      {/* Transcripts stored here (by the old app) are no longer sent with every chat: attach the meeting instead. */}
                      {d.kind === "transcript" && <span className="text-xs text-muted" data-testid="doc-not-sent">· meeting transcript, not sent with chats (attach the meeting to a chat instead)</span>}
                    </span>
                    <Button size="icon-sm" variant="ghost" aria-label={`Remove ${d.name}`} onClick={() => removeDoc(d.name)}><Trash2 className="h-3.5 w-3.5" /></Button>
                  </li>
                ))}
              </ul>
            )}
            <input ref={fileRef} type="file" multiple hidden onChange={(e) => upload(e.target.files)} />
            <Button size="sm" className="mt-2" onClick={() => fileRef.current?.click()}><Upload className="h-3.5 w-3.5" /> Add documents</Button>
          </div>
        </section>
      )}
      {!project && <Callout tone="warning">Projects couldn’t be loaded. Is the API running?</Callout>}
      <ConfirmDialog open={!!deleting} onOpenChange={(o) => !o && setDeleting(null)} title={`Delete ${deleting?.name}?`}
                     description="Deletes the project with its chats, documents, memory and skills." onConfirm={() => deleting && remove(deleting)} />
    </div>
  );
}
