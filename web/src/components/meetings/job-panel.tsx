"use client";

/**
 * Live view of one transcription job: stage, progress, time left, the text so far, Cancel, the result, and
 * what happens after it (identify speakers, write the summary).
 */
import { AlertTriangle, CheckCircle2, ExternalLink, Square, XCircle } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Button, Callout, Progress, Spinner } from "@/components/ui";
import { api, type FollowUp, type Job, type Transcript } from "@/lib/api";
import { fmtDuration, fmtTime, speakerColor } from "@/lib/format";

const STAGES: Record<string, string> = {
  queued: "Waiting for the previous job", starting: "Loading models", extract: "Extracting audio",
  transcribe: "Transcribing", diarize: "Separating speakers", screen: "Reading names from the video", finish: "Saving",
  done: "Done", cancelled: "Cancelled", freeing_memory: "Waiting for the local model to free its memory",
  waiting_for_call: "Waiting until the call being recorded (and its live transcript) is finished",
};

const FOLLOWUP_END = ["done", "needs_ok", "needs_model", "error", "cancelled"];

/**
 * One line about the after-transcription work: waiting, identifying speakers, writing the summary, done,
 * or what needs the user (choose a model, approve the cloud).
 */
function FollowUpLine({ followup }: { followup: FollowUp }) {
  const active = !FOLLOWUP_END.includes(followup.state);
  const text = followup.state === "waiting" ? "When the transcript is ready: identify speakers, then write the summary"
    : followup.message;
  return (
    <div data-testid="followup" data-state={followup.state}
         className="flex items-start gap-2 rounded-xl bg-surface-2 px-3 py-2 text-[13px]">
      {active ? <Spinner className="mt-0.5 h-3.5 w-3.5" />
        : followup.state === "done" ? <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 text-success" />
        : <AlertTriangle className="mt-0.5 h-3.5 w-3.5 text-warning" />}
      <span>{text}{followup.state === "done" && followup.speaker_model_used
        ? ` Speakers by ${followup.speaker_model_used}${followup.summary_model_used ? `, summary by ${followup.summary_model_used}` : ""}.` : ""}</span>
    </div>
  );
}

/** Live progress of one transcription, with the text streaming in as each part finishes. */
export function JobPanel({ jobId, onFinished }: { jobId: string; onFinished?: (job: Job) => void }) {
  const [job, setJob] = useState<Job | null>(null);
  const [partial, setPartial] = useState<Job["partial"] | null>(null);
  const [result, setResult] = useState<Transcript | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const finished = useRef(false);
  const tailRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    finished.current = false;
    let timer: number;
    const tick = async () => {
      try {
        const data = await api.get<Job>(`/api/meetings/jobs/${jobId}?partial=true`);
        setJob(data);
        setError(null);
        if (data.partial?.segments.length) setPartial(data.partial);
        if (["done", "error", "cancelled"].includes(data.status)) {
          if (!finished.current) {
            finished.current = true;
            if (data.status === "done" && data.transcript_id) {
              api.get<Transcript>(`/api/meetings/transcripts/${data.transcript_id}`).then(setResult).catch(() => undefined);
              toast.success("Transcript ready", { description: data.name });
            } else if (data.status === "error") {
              toast.error("Transcription failed", { description: data.error ?? data.name });
            }
            onFinished?.(data);
          }
          // keep watching while speakers and the summary are worked out after the transcript
          if (!data.followup || FOLLOWUP_END.includes(data.followup.state)) return;
        }
      } catch (err) {
        setError((err as Error).message);
      }
      timer = window.setTimeout(tick, 1500);
    };
    tick();
    return () => window.clearTimeout(timer);
  }, [jobId, onFinished]);

  useEffect(() => {
    tailRef.current?.scrollTo({ top: tailRef.current.scrollHeight, behavior: "smooth" });
  }, [partial?.segments.length]);

  const cancel = async () => {
    setCancelling(true);
    try {
      await api.post(`/api/meetings/jobs/${jobId}/cancel`);
    } catch (err) {
      toast.error("Couldn't cancel", { description: (err as Error).message });
      setCancelling(false);
    }
  };

  if (!job) {
    return <div className="flex items-center gap-2 rounded-2xl border border-border p-5 text-sm text-muted"><Spinner /> Starting…</div>;
  }
  const active = job.status === "queued" || job.status === "running";
  const pct = Math.round(job.progress * 100);
  return (
    <section className="space-y-4 rounded-2xl border border-border bg-surface p-5 shadow-card" data-testid="job-panel" data-status={job.status}
             aria-label="Transcription output">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-semibold">
            {job.status === "done" ? <CheckCircle2 className="h-4 w-4 text-success" />
              : job.status === "error" ? <XCircle className="h-4 w-4 text-danger" />
              : job.status === "cancelled" ? <Square className="h-4 w-4 text-muted" /> : <Spinner />}
            <span className="truncate">{job.name}</span>
          </div>
          <p className="mt-0.5 text-[13px] text-muted" data-testid="job-stage">
            {cancelling && active ? "Cancelling — stops after the current step…" : STAGES[job.stage] ?? job.stage}
            {active && ` · ${pct}%`}
            {job.elapsed ? ` · ${fmtDuration(job.elapsed)} elapsed` : ""}
            {active && job.eta ? ` · about ${fmtDuration(job.eta)} left` : ""}
          </p>
        </div>
        {active && <Button size="sm" variant="secondary" onClick={cancel} disabled={cancelling} data-testid="cancel-job">
          <Square className="h-3 w-3 fill-current" /> Cancel</Button>}
        {result && <Link href={`/meetings/${result.id}`}><Button size="sm" variant="primary">Open transcript <ExternalLink className="h-3.5 w-3.5" /></Button></Link>}
      </div>
      {active && <Progress value={pct} label="Transcription progress" />}
      {error && active && <Callout tone="warning">Lost contact with the transcriber: {error}. Retrying…</Callout>}
      {job.status === "error" && <Callout tone="danger" title="Transcription failed">{job.error}</Callout>}
      {job.status === "cancelled" && <Callout tone="info">Cancelled. Starting the same recording again resumes from the parts already done.</Callout>}
      {job.followup && <FollowUpLine followup={job.followup} />}

      {active && partial && partial.segments.length > 0 && (
        <div>
          <div className="mb-1.5 text-xs font-medium text-muted">Live transcript <span className="font-normal">· speakers are added at the end</span></div>
          <div ref={tailRef} className="max-h-72 space-y-1.5 overflow-y-auto rounded-xl bg-surface-2 p-3 text-[13px] leading-relaxed" data-testid="live-transcript">
            {partial.segments.map((s, i) => (
              <p key={i}><span className="mr-2 font-mono text-xs text-faint">{fmtTime(s.start)}</span>{s.text}</p>
            ))}
          </div>
        </div>
      )}
      {active && (!partial || partial.segments.length === 0) && job.status === "running" && (
        <p className="text-[13px] text-muted">The first lines appear here after about two minutes of audio are transcribed.</p>
      )}
      {result && (
        <div data-testid="job-result">
          <div className="mb-1.5 text-xs font-medium text-muted">
            {fmtDuration(result.duration)} · {result.speakers.length} speaker{result.speakers.length === 1 ? "" : "s"} · {result.segments.length} lines
          </div>
          <div className="max-h-72 space-y-2 overflow-y-auto rounded-xl bg-surface-2 p-3 text-[13px] leading-relaxed">
            {result.segments.slice(0, 40).map((s, i) => (
              <p key={i}>
                <span className="mr-2 font-mono text-xs text-faint">{fmtTime(s.start)}</span>
                <span className="mr-1.5 font-medium" style={{ color: speakerColor(s.speaker, result.speakers) }}>{result.display_names[s.speaker] ?? s.speaker}</span>
                {s.text}
              </p>
            ))}
            {result.segments.length > 40 && <p className="text-xs text-muted">… open the transcript to see all {result.segments.length} lines.</p>}
          </div>
        </div>
      )}
    </section>
  );
}
