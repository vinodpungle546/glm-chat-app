"use client";

/**
 * Route '/meetings/<id>': one meeting. The transcript on the left, the Speakers and Summary panels on the
 * right (as tabs on narrow screens).
 */
import * as Tabs from "@radix-ui/react-tabs";
import { ArrowLeft, Download, FilePlus2, MessageSquare, Search, Trash2 } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { AddTranscriptDialog, CorrectionsPanel, MarkedText, useCorrectionJobs } from "@/components/meetings/corrections";
import { AlertsView } from "@/components/meetings/meeting-check";
import { MeetingPlayer, playFrom } from "@/components/meetings/player";
import { SpeakersPanel } from "@/components/meetings/speakers-panel";
import { SummaryPanel } from "@/components/meetings/summary-panel";
import { Badge, Button, Callout, ConfirmDialog, Input } from "@/components/ui";
import { api, API_URL, type Job, type Session, type Transcript } from "@/lib/api";
import { clsx, fmtDuration, fmtTime, shortDate, speakerColor } from "@/lib/format";

/**
 * True when the window is at least 1024 px wide (then the panels sit next to the transcript instead of in
 * tabs). Updates when the window is resized.
 */
function useIsDesktop() {
  const [desktop, setDesktop] = useState(true);
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1024px)");
    const update = () => setDesktop(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  return desktop;
}

/**
 * The transcript lines with speaker names and times. Highlights matches of `query`, and scrolls to a line
 * when a notes link or the 'quorum:seek' event asks for a time or line number. A line's time plays the recording
 * from there; the line being played ('quorum:playing') is marked.
 */
function Segments({ transcript, query }: { transcript: Transcript; query: string }) {
  const [focus, setFocus] = useState<number | null>(null);
  const [heard, setHeard] = useState<number | null>(null);
  useEffect(() => {
    const onPlaying = (e: Event) => {
      const t = (e as CustomEvent<{ seconds: number }>).detail.seconds;
      const segs = transcript.segments;
      let index = -1;
      for (let i = 0; i < segs.length; i++) {
        if (segs[i].start <= t + 0.05) index = i;
        else break;
      }
      setHeard(index >= 0 && t <= segs[index].end + 2 ? index : null);
    };
    window.addEventListener("quorum:playing", onPlaying);
    return () => window.removeEventListener("quorum:playing", onPlaying);
  }, [transcript.segments]);
  const params = useSearchParams();
  useEffect(() => {
    const seek = (detail: { seconds?: number; line?: number }) => {
      const segs = transcript.segments;
      let index = -1;
      if (detail.line !== undefined) index = detail.line - 1;
      else if (detail.seconds !== undefined) {
        index = segs.findIndex((s, i) => s.start <= detail.seconds! && (i === segs.length - 1 || segs[i + 1].start > detail.seconds!));
      }
      if (index < 0 || index >= segs.length) return;
      setFocus(index);
      window.setTimeout(() => document.querySelector(`[data-seg="${index}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" }), 50);
    };
    const t = params.get("t");
    const line = params.get("line");
    if (t !== null) seek({ seconds: Number(t) });
    else if (line !== null) seek({ line: Number(line) });
    const onSeek = (e: Event) => seek((e as CustomEvent).detail);
    window.addEventListener("quorum:seek", onSeek);
    return () => window.removeEventListener("quorum:seek", onSeek);
  }, [transcript.segments, params]);
  const q = query.trim().toLowerCase();
  const segs = q ? transcript.segments.filter((s) => s.text.toLowerCase().includes(q)) : transcript.segments;
  const marks = transcript.correction && !transcript.correction.undone ? transcript.correction.marks : {};
  const changes = new Map((transcript.correction?.changes ?? []).map((c) => [c.id, c]));
  const timed = transcript.timestamps !== false;
  if (!segs.length) return <p className="py-6 text-center text-[13px] text-muted">{q ? `No lines contain “${query}”.` : "This transcript is empty."}</p>;
  return (
    <ol className="space-y-3" data-testid="segments">
      {segs.map((s, i) => {
        const name = transcript.display_names[s.speaker] ?? s.speaker;
        const prev = segs[i - 1];
        const continued = prev && prev.speaker === s.speaker && !q;
        const index = q ? transcript.segments.indexOf(s) : i;
        return (
          <li key={`${s.start}-${i}`} data-seg={index} data-focused={focus === index || undefined} data-heard={heard === index || undefined}
              className={clsx("grid grid-cols-[3.5rem_1fr] gap-x-3 rounded-lg transition-colors", focus === index && "bg-accent-soft ring-1 ring-accent/30",
                              heard === index && focus !== index && "bg-surface-2")}>
            {timed ? (
              <button type="button" onClick={() => playFrom(s.start)} title="Play from here" data-testid="seg-play"
                      className="self-start rounded pt-0.5 text-right font-mono text-xs text-faint hover:text-accent hover:underline">
                {fmtTime(s.start)}
              </button>
            ) : <span />}
            <div className="min-w-0">
              {!continued && (
                <div className="mb-0.5 text-[13px] font-semibold" style={{ color: speakerColor(s.speaker, transcript.speakers) }}>{name}</div>
              )}
              <p className="text-[14px] leading-relaxed"><MarkedText text={s.text} spans={marks[String(index)]} changes={changes} /></p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/**
 * Loads the meeting (transcript, labels, summary, model choices) and lays out the transcript and the
 * Speakers and Summary panels. Child panels report changes through `onChange` so the page stays in sync.
 */
export default function TranscriptPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { selectedModel, refreshSessions } = useApp();
  const desktop = useIsDesktop();
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [adding, setAdding] = useState(false);
  const [busyTranscriber, setBusyTranscriber] = useState(false);
  const params = useSearchParams();
  const [tab, setTab] = useState(params.get("tab") === "summary" ? "summary" : "speakers");

  const load = useCallback(async () => {
    try {
      const t = await api.get<Transcript>(`/api/meetings/transcripts/${id}`);
      setTranscript(t);
      document.title = `${t.title} · Quorum`;
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, [id]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    api.get<Job[]>("/api/meetings/jobs").then((jobs) => setBusyTranscriber(jobs.some((j) => j.status === "running"))).catch(() => undefined);
  }, []);

  const patch = (p: Partial<Transcript>) => (Object.keys(p).length ? setTranscript((t) => (t ? { ...t, ...p } : t)) : load());
  const reload = useCallback(() => { void load(); }, [load]);

  const chat = async () => {
    try {
      const s = await api.post<Session>(`/api/meetings/transcripts/${id}/chat`, { model_id: selectedModel?.id ?? "" });
      await refreshSessions();
      router.push(`/c/${s.id}`);
    } catch (err) {
      toast.error("Couldn't start a chat", { description: (err as Error).message });
    }
  };
  const remove = async () => {
    try {
      await api.del(`/api/meetings/transcripts/${id}`);
      toast.success("Transcript deleted");
      router.push("/meetings");
    } catch (err) {
      toast.error("Couldn't delete", { description: (err as Error).message });
    }
  };

  const named = useMemo(() => (transcript ? Object.values(transcript.labels).filter((l) => l.name).length : 0), [transcript]);

  if (error) {
    return (
      <div className="mx-auto max-w-lg p-8">
        <Callout tone="danger" title="Couldn't open this transcript" action={<Link href="/meetings"><Button size="sm">Back to Meetings</Button></Link>}>{error}</Callout>
      </div>
    );
  }
  if (!transcript) {
    return <div className="mx-auto max-w-5xl space-y-3 p-6"><div className="skeleton h-8 w-1/2" /><div className="skeleton h-4 w-1/3" /><div className="skeleton h-64" /></div>;
  }

  const transcriptPane = (
    <div className="space-y-3">
      <MeetingPlayer transcriptId={id} fallbackDuration={transcript.duration} />
      <div className="relative">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-faint" />
        <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search the transcript" aria-label="Search the transcript" className="h-8 pl-8 text-[13px]" />
      </div>
      <Segments transcript={transcript} query={query} />
    </div>
  );
  const speakers = <SpeakersPanel transcript={transcript} onChange={patch} busyTranscriber={busyTranscriber} />;
  const corrections = <CorrectionsPanel transcript={transcript} onChange={reload} onAdd={() => setAdding(true)} />;
  const summary = <SummaryPanel transcript={transcript} onSaved={(s) => patch({ summary: s })} />;
  const seekTo = (seconds: number) => window.dispatchEvent(new CustomEvent("quorum:seek", { detail: { seconds } }));
  const check = (
    <div className="space-y-2">
      <p className="text-[13px] text-muted">Conflicts with earlier meetings, things to raise with everyone, items still pending, and actions for you.</p>
      <AlertsView target={{ transcript_id: id }} onLine={seekTo} />
    </div>
  );
  const tabClass = "-mb-px border-b-2 border-transparent px-3 py-2 text-[13px] font-medium text-muted hover:text-fg data-[state=active]:border-accent data-[state=active]:text-fg";

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-5 sm:py-6">
        <Link href="/meetings" className="mb-3 inline-flex items-center gap-1 text-[13px] text-muted hover:text-fg"><ArrowLeft className="h-3.5 w-3.5" /> Meetings</Link>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="truncate text-xl font-semibold tracking-tight" data-testid="transcript-title">{transcript.title}</h1>
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-muted">
              <span>{shortDate(transcript.created_at)}</span>
              {transcript.duration > 0 && <span>· {fmtDuration(transcript.duration)}</span>}
              <span>· {transcript.speakers.length} speakers{named ? ` (${named} named)` : ""}</span>
              {transcript.language && transcript.language !== "?" && <span>· {transcript.language.toUpperCase()}</span>}
              {transcript.source_type === "text" && <Badge>Text import</Badge>}
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" onClick={chat} data-testid="chat-about"><MessageSquare className="h-4 w-4" /> Ask about this meeting</Button>
            {transcript.source_type !== "text" && (
              <Button variant="secondary" onClick={() => setAdding(true)} data-testid="add-transcript">
                <FilePlus2 className="h-4 w-4" /> Add another transcript
              </Button>
            )}
            <a href={`${API_URL}/api/meetings/transcripts/${id}/markdown`} download={`${transcript.title}.md`}>
              <Button variant="secondary" aria-label="Download as Markdown"><Download className="h-4 w-4" /> .md</Button>
            </a>
            <Button variant="ghost" aria-label="Delete transcript" onClick={() => setConfirmDelete(true)}><Trash2 className="h-4 w-4" /></Button>
          </div>
        </div>
        {transcript.diarization_error && <Callout tone="warning" className="mt-4">Speaker separation failed for this recording ({transcript.diarization_error}). The text is complete.</Callout>}

        {desktop ? (
          <div className={clsx("mt-5 grid gap-6", tab === "summary" ? "grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)]" : "grid-cols-[minmax(0,1fr)_380px]")}>
            <section aria-label="Transcript" className="min-w-0">{transcriptPane}</section>
            <aside className="min-w-0 space-y-4">
              <Tabs.Root value={tab} onValueChange={setTab} className="rounded-2xl border border-border bg-surface shadow-card">
                <Tabs.List className="flex gap-1 border-b border-border px-3 pt-1.5" aria-label="Meeting tools">
                  <Tabs.Trigger value="speakers" className={tabClass} data-testid="tab-speakers">Speakers</Tabs.Trigger>
                  <Tabs.Trigger value="summary" className={tabClass} data-testid="tab-summary">Summary</Tabs.Trigger>
                  <Tabs.Trigger value="check" className={tabClass} data-testid="tab-check">Check</Tabs.Trigger>
                  <Tabs.Trigger value="corrections" className={tabClass} data-testid="tab-corrections">Corrections</Tabs.Trigger>
                </Tabs.List>
                <Tabs.Content value="speakers" className="p-4">{speakers}</Tabs.Content>
                <Tabs.Content value="corrections" className="p-4">{corrections}</Tabs.Content>
                <Tabs.Content value="summary" className="p-4">{summary}</Tabs.Content>
                <Tabs.Content value="check" className="p-4">{check}</Tabs.Content>
              </Tabs.Root>
            </aside>
          </div>
        ) : (
          <Tabs.Root defaultValue={tab === "summary" ? "summary" : "transcript"} className="mt-4">
            <Tabs.List className="flex gap-1 border-b border-border" aria-label="Meeting views">
              <Tabs.Trigger value="transcript" className={tabClass}>Transcript</Tabs.Trigger>
              <Tabs.Trigger value="speakers" className={tabClass} data-testid="tab-speakers">Speakers</Tabs.Trigger>
              <Tabs.Trigger value="summary" className={tabClass} data-testid="tab-summary">Summary</Tabs.Trigger>
              <Tabs.Trigger value="check" className={tabClass} data-testid="tab-check">Check</Tabs.Trigger>
              <Tabs.Trigger value="corrections" className={tabClass} data-testid="tab-corrections">Corrections</Tabs.Trigger>
            </Tabs.List>
            <Tabs.Content value="transcript" className="pt-4">{transcriptPane}</Tabs.Content>
            <Tabs.Content value="speakers" className="pt-4">{speakers}</Tabs.Content>
            <Tabs.Content value="summary" className="pt-4">{summary}</Tabs.Content>
            <Tabs.Content value="check" className="pt-4">{check}</Tabs.Content>
            <Tabs.Content value="corrections" className="pt-4">{corrections}</Tabs.Content>
          </Tabs.Root>
        )}
      </div>
      <CorrectionJobs transcript={transcript} reload={reload} adding={adding} setAdding={setAdding} />
      <ConfirmDialog open={confirmDelete} onOpenChange={setConfirmDelete} title="Delete transcript?"
                     description="The transcript, its summary and speaker labels are deleted. The recording is kept." onConfirm={remove} />
    </div>
  );
}

/** The "Add another transcript" dialog, and watching a Voice Memos recording being transcribed (reloads after). */
function CorrectionJobs({ transcript, reload, adding, setAdding }: {
  transcript: Transcript; reload: () => void; adding: boolean; setAdding: (open: boolean) => void;
}) {
  const { watch } = useCorrectionJobs(transcript, reload);
  return <AddTranscriptDialog open={adding} onOpenChange={setAdding} transcriptId={transcript.id} onDone={reload} onWatch={watch} />;
}
