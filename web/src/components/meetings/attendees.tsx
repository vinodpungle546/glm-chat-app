"use client";

/**
 * A meeting's attendees, kept in step with its speakers: everyone in the call's participants list (read from the
 * video), every named speaker, and people you added. For each: whether they spoke, when they first appeared in the
 * list and first spoke (play links). Add, rename or remove; renaming someone who spoke renames the speaker too, and
 * people who spoke can't be removed (move their lines instead). Your edits are never changed by the app.
 */
import { Check, Pencil, Plus, Trash2, Users, X } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { playFrom } from "@/components/meetings/player";
import { Badge, Button, Input } from "@/components/ui";
import { api, type Attendee, type Transcript } from "@/lib/api";
import { fmtTime } from "@/lib/format";

export function AttendeesSection({ transcript, onChange }: { transcript: Transcript; onChange: (t: Partial<Transcript>) => void }) {
  const view = transcript.attendees;
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<{ name: string; to: string } | null>(null);
  if (!view) return null;
  const rows = view.attendees;
  const change = async (action: "add" | "remove" | "rename", name: string, to = "") => {
    setBusy(`${action}:${name}`);
    try {
      const res = await api.post<Partial<Transcript>>(`/api/meetings/transcripts/${transcript.id}/attendees`, { action, name, to });
      onChange(res);
      if (action === "add") setAdding("");
      if (action === "rename") setRenaming(null);
    } catch (err) {
      toast.error("Couldn't change the attendees", { description: (err as Error).message });
    } finally {
      setBusy(null);
    }
  };
  const spoke = rows.filter((r) => r.spoke).length;
  return (
    <section className="space-y-2 rounded-xl border border-border p-3" data-testid="attendees">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <Users className="h-4 w-4" /> Attendees <span className="font-normal text-muted">({rows.length} · {spoke} spoke)</span>
        </h3>
        <span className="text-xs text-muted">{view.from_video ? "From the participants list in the video and the speakers" : "From the speakers (no participants list in the video)"}</span>
      </div>
      <ul className="divide-y divide-border">
        {rows.map((r) => (
          <AttendeeRow key={`${r.name}-${r.speaker ?? ""}`} row={r} busy={busy}
                       renaming={renaming?.name === r.name ? renaming : null} setRenaming={setRenaming} change={change} />
        ))}
      </ul>
      {view.invited.length > 0 && (
        <p className="text-xs text-muted" data-testid="attendee-invited">Invited, didn&apos;t join: {view.invited.join(", ")}</p>
      )}
      <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); if (adding.trim()) change("add", adding.trim()); }}>
        <Input value={adding} onChange={(e) => setAdding(e.target.value)} placeholder="Add someone who attended" aria-label="Add an attendee"
               className="h-8 text-[13px]" data-testid="attendee-add-input" />
        <Button size="sm" variant="secondary" type="submit" disabled={!adding.trim()} loading={busy === `add:${adding.trim()}`} data-testid="attendee-add">
          <Plus className="h-3.5 w-3.5" /> Add
        </Button>
      </form>
    </section>
  );
}

function AttendeeRow({ row, busy, renaming, setRenaming, change }: {
  row: Attendee; busy: string | null; renaming: { name: string; to: string } | null;
  setRenaming: (r: { name: string; to: string } | null) => void;
  change: (action: "add" | "remove" | "rename", name: string, to?: string) => void;
}) {
  const source = row.source === "video" ? "In the participants list" : row.source === "you" ? "Added by you" : "Speaker";
  const linked = row.speaker && row.speaker_name && row.speaker_name !== row.name ? row.speaker_name : "";
  return (
    <li className="flex flex-wrap items-center gap-2 py-1.5 text-[13px]" data-testid="attendee-row" data-name={row.name}
        data-attendee-speaker={row.speaker ?? undefined}>
      {renaming ? (
        <form className="flex min-w-0 flex-1 gap-1.5" onSubmit={(e) => { e.preventDefault(); change("rename", row.name, renaming.to); }}>
          <Input value={renaming.to} onChange={(e) => setRenaming({ name: row.name, to: e.target.value })} autoFocus
                 aria-label={`New name for ${row.name}`} className="h-7 text-[13px]" data-testid="attendee-rename-input" />
          <Button size="icon-sm" variant="primary" type="submit" aria-label="Save" loading={busy === `rename:${row.name}`}
                  data-testid="attendee-rename-save"><Check className="h-3.5 w-3.5" /></Button>
          <Button size="icon-sm" variant="ghost" type="button" aria-label="Cancel" onClick={() => setRenaming(null)}><X className="h-3.5 w-3.5" /></Button>
        </form>
      ) : (
        <>
          <span className="font-medium" data-testid="attendee-name">{row.name}</span>
          {linked && <span className="text-xs text-muted">speaks as {linked}</span>}
          <Badge tone={row.spoke ? "success" : "neutral"}>{row.spoke ? "Spoke" : "Didn't speak"}</Badge>
          <span className="text-xs text-muted">{source}</span>
          {row.first_seen != null && (
            <button type="button" className="font-mono text-xs text-accent hover:underline" onClick={() => playFrom(row.first_seen!)}
                    title="First seen in the participants list">in list {fmtTime(row.first_seen)}</button>
          )}
          {row.first_spoke != null && (
            <button type="button" className="font-mono text-xs text-accent hover:underline" onClick={() => playFrom(row.first_spoke!)}
                    title="First time they spoke">spoke {fmtTime(row.first_spoke)}</button>
          )}
          <span className="ml-auto flex gap-0.5">
            <Button size="icon-sm" variant="ghost" aria-label={`Rename ${row.name}`} data-testid="attendee-rename"
                    onClick={() => setRenaming({ name: row.name, to: row.name })}><Pencil className="h-3.5 w-3.5" /></Button>
            {!row.speaker && (
              <Button size="icon-sm" variant="ghost" aria-label={`Remove ${row.name}`} data-testid="attendee-remove"
                      loading={busy === `remove:${row.name}`} onClick={() => change("remove", row.name)}><Trash2 className="h-3.5 w-3.5" /></Button>
            )}
          </span>
        </>
      )}
    </li>
  );
}
