"use client";

/**
 * Route '/meetings': start a new transcription (upload, watch folder or pasted text), watch its progress,
 * and see the list of transcripts.
 */
import { Mic, Search, Trash2, Users } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { JobPanel } from "@/components/meetings/job-panel";
import { MyActions } from "@/components/meetings/meeting-check";
import { NewMeeting } from "@/components/meetings/new-meeting";
import { Badge, Button, Callout, ConfirmDialog, EmptyState, Input } from "@/components/ui";
import { api, type Job, type TranscriptMeta } from "@/lib/api";
import { fmtDuration, shortDate } from "@/lib/format";

/** The Meetings page: NewMeeting on top, the running job's JobPanel, then the transcript list. */
export default function MeetingsPage() {
  const router = useRouter();
  const [jobId, setJobId] = useState<string | null>(null);
  const [transcripts, setTranscripts] = useState<TranscriptMeta[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [deleting, setDeleting] = useState<TranscriptMeta | null>(null);

  const load = useCallback(async () => {
    try {
      setTranscripts(await api.get<TranscriptMeta[]>("/api/meetings/transcripts"));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
      setTranscripts([]);
    }
  }, []);

  useEffect(() => {
    document.title = "Meetings · Quorum";
    load();
    // Pick up a transcription that is still running (e.g. after a reload).
    api.get<Job[]>("/api/meetings/jobs").then((jobs) => {
      const running = jobs.find((j) => j.status === "running" || j.status === "queued");
      if (running) setJobId((current) => current ?? running.id);
    }).catch(() => undefined);
  }, [load]);

  const started = (job: Job) => {
    setJobId(null); // clear the previous output before the new run shows
    window.setTimeout(() => setJobId(job.id), 0);
    toast("Transcription started", { description: job.name });
  };

  const onFinished = useCallback(() => { load(); }, [load]);

  const remove = async (t: TranscriptMeta) => {
    try {
      await api.del(`/api/meetings/transcripts/${t.id}`);
      toast.success("Transcript deleted");
      load();
    } catch (err) {
      toast.error("Couldn't delete", { description: (err as Error).message });
    }
  };

  const shown = (transcripts ?? []).filter((t) => t.title.toLowerCase().includes(query.toLowerCase()));
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl space-y-6 px-4 py-6 sm:py-8">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Meetings</h1>
          <p className="mt-1 text-sm text-muted">Transcribe recordings offline with speaker labels, then get minutes or ask questions.</p>
        </div>
        {error && <Callout tone="danger" title="The local transcriber isn’t reachable">{error}</Callout>}
        <NewMeeting onStarted={started} onImported={(t) => { load(); router.push(`/meetings/${t.id}`); }} />
        {jobId && <JobPanel key={jobId} jobId={jobId} onFinished={onFinished} />}
        <MyActions />

        <section aria-label="Transcripts">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h2 className="text-[15px] font-semibold">Transcripts</h2>
            <div className="relative w-56">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-faint" />
              <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search" aria-label="Search transcripts" className="h-8 pl-8 text-[13px]" />
            </div>
          </div>
          {transcripts === null ? (
            <div className="space-y-2">{[0, 1, 2].map((i) => <div key={i} className="skeleton h-14" />)}</div>
          ) : shown.length === 0 ? (
            <EmptyState icon={<Mic className="h-5 w-5" />} title={query ? "No matches" : "No transcripts yet"}>
              {query ? `Nothing matches “${query}”.` : "Upload a recording above, pick one from your watch folder, or paste a conversation."}
            </EmptyState>
          ) : (
            <ul className="divide-y divide-border overflow-hidden rounded-2xl border border-border" data-testid="transcript-list">
              {shown.map((t) => (
                <li key={t.id} className="group flex items-center gap-3 bg-surface px-4 py-3 hover:bg-hover/50">
                  <Link href={`/meetings/${t.id}`} className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium">{t.title}</div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-muted">
                      <span>{shortDate(t.created_at)}</span>
                      {t.duration > 0 && <span>· {fmtDuration(t.duration)}</span>}
                      <span className="flex items-center gap-1">· <Users className="h-3 w-3" /> {t.speakers.length}</span>
                      {t.source_type === "text" && <Badge>Text import</Badge>}
                    </div>
                  </Link>
                  <Button size="icon-sm" variant="ghost" aria-label={`Delete ${t.title}`} className="opacity-60 group-hover:opacity-100"
                          onClick={() => setDeleting(t)}><Trash2 className="h-3.5 w-3.5" /></Button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
      <ConfirmDialog open={!!deleting} onOpenChange={(o) => !o && setDeleting(null)} title="Delete transcript?"
        description={`“${deleting?.title}” and its summary and speaker labels are deleted. The recording itself is kept.`}
        onConfirm={() => deleting && remove(deleting)} />
    </div>
  );
}
