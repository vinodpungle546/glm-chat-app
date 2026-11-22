"use client";

/**
 * Recording a call on this Mac (Quorum Recorder, through the transcriber):
 *  - RecordTab: the "Record a call" tab of New meeting (what to record, microphone, live transcript, models),
 *    then the RecordingBar while the call runs (time, sound meters, warnings, Stop).
 *  - RecordingIndicator: "● 12:03" in the top bar on every page.
 *  - LivePanel: the live transcript, docked on the right, with Copy, Ask in chat and Actions so far.
 * Your microphone and the call's sound are kept as separate tracks, so your words are marked as yours.
 */
import { AlertTriangle, Copy, ListChecks, MessageSquare, Mic, MonitorSmartphone, Radio, Square, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { AlertsView, useOpenAlerts } from "@/components/meetings/meeting-check";
import { FramesNote, ModelSelect } from "@/components/meetings/model-select";
import type { StartOptions } from "@/components/meetings/new-meeting";
import { Button, Callout, Input, Label, Spinner, Switch } from "@/components/ui";
import {
  api, ApiError, type Job, type LiveDraft, type LiveSegment, type RecordSource, type RecordSources,
  type RecordStatus, type RecordStopResult, type Session,
} from "@/lib/api";
import { clsx } from "@/lib/format";

const OPTIONS_KEY = "quorum:record-options";
const SPEAKER_CHOICES = [0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20];
export const ACTIONS_PROMPT = "List the decisions and action items so far, with owners and due dates.";

/** '01:02:03' / '12:03' for a running clock. */
export function clock(seconds: number | null | undefined) {
  const s = Math.max(0, Math.round(seconds ?? 0));
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** A sound level (RMS 0..1) as 0..100 on a -60..0 dB scale. */
function meter(rms: number | undefined) {
  if (!rms || rms <= 0) return 0;
  return Math.max(0, Math.min(100, ((20 * Math.log10(rms) + 60) / 60) * 100));
}

/** The New meeting choices for this call, kept for Stop (which may be pressed on another page). */
function saveOptions(options: StartOptions) {
  try { window.sessionStorage.setItem(OPTIONS_KEY, JSON.stringify(options)); } catch { /* private mode */ }
}
function loadOptions(): Partial<StartOptions> {
  try { return JSON.parse(window.sessionStorage.getItem(OPTIONS_KEY) ?? "{}"); } catch { return {}; }
}

/** Stops the recording and starts its transcription with the choices made when it started. */
export async function stopRecording(modelId: string, transcribe = true): Promise<RecordStopResult> {
  const options = loadOptions();
  return api.post<RecordStopResult>("/api/meetings/record/stop", { transcribe, ...options, model_id: modelId });
}

/** Why recording can't start, with what to do about it. */
function PermissionHelp({ error }: { error: ApiError }) {
  const where = error.code === "permission_mic" ? "Microphone" : "Screen & System Audio Recording";
  if (error.code === "permission_screen" || error.code === "permission_mic") {
    return (
      <Callout tone="warning" title="macOS needs your permission" data-testid="record-permission">
        Open System Settings → Privacy &amp; Security → {where}, switch on <b>Quorum Recorder</b>, then try again.
        If it&apos;s already on, select it, remove it with <b>−</b>, try again and allow it when macOS asks. The
        switch can belong to an earlier build of the recorder. Nothing is recorded or sent anywhere until you allow it.
      </Callout>
    );
  }
  if (error.code === "cancelled") {
    return <Callout tone="info" title="Nothing chosen" data-testid="record-cancelled">{error.message}</Callout>;
  }
  if (error.code === "unavailable") {
    return <Callout tone="warning" title="Recording isn't set up">{error.message}</Callout>;
  }
  return <Callout tone="danger" title="Can't record" data-testid="record-error">{error.message}</Callout>;
}

/** The two sound meters: you (microphone) and the call. */
function Meters({ levels }: { levels: { system: number; mic: number } | null | undefined }) {
  return (
    <div className="grid gap-2 sm:grid-cols-2" data-testid="record-meters">
      {([["You", "mic"], ["Call audio", "system"]] as const).map(([label, key]) => (
        <div key={key} className="flex items-center gap-2 text-xs">
          <span className="w-16 shrink-0 text-muted">{label}</span>
          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-2" role="meter" aria-label={`${label} level`}
               aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(meter(levels?.[key]))}>
            <div className="h-full rounded-full bg-success transition-[width] duration-300" style={{ width: `${meter(levels?.[key])}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
}

const REASONS: Record<string, string> = {
  app_quit: "the meeting app was closed", disk_full: "the disk is almost full", capture_stopped: "macOS stopped the capture",
  interrupted: "the recorder stopped unexpectedly", stopped: "it was stopped",
};

/** While a call is recorded: time, meters, warnings, the live transcript button and Stop. */
export function RecordingBar({ onStopped }: { onStopped: (job: Job | null) => void }) {
  const { recording, refreshRecording, selectedModel, setLivePanelOpen } = useApp();
  const [stopping, setStopping] = useState(false);
  if (!recording || recording.state === "idle") return null;
  const stop = async () => {
    setStopping(true);
    try {
      const done = await stopRecording(selectedModel?.id ?? "");
      await refreshRecording();
      if (!done.recording.path) toast("Nothing was recorded");
      onStopped(done.job);
    } catch (err) {
      toast.error("Couldn't stop the recording", { description: (err as Error).message });
    } finally {
      setStopping(false);
    }
  };
  const ended = recording.state !== "recording";
  const live = recording.live_state;
  return (
    <div className="space-y-3" data-testid="recording-bar">
      <div className="flex flex-wrap items-center gap-3">
        <span className={clsx("flex items-center gap-2 text-sm font-medium", ended ? "text-warning" : "text-danger")}>
          <span className={clsx("h-2.5 w-2.5 rounded-full", ended ? "bg-warning" : "animate-pulse bg-danger")} />
          {ended ? "Recording ended" : "Recording"} <span className="tabular-nums" data-testid="record-elapsed">{clock(recording.elapsed)}</span>
        </span>
        <span className="text-xs text-muted">{recording.title || "Call"} · {recording.mic ?? "Mac input"}</span>
        <div className="ml-auto flex gap-2">
          {recording.live && (
            <Button variant="outline" size="sm" onClick={() => setLivePanelOpen(true)} data-testid="open-live">
              <Radio className="h-3.5 w-3.5" /> Live transcript
            </Button>
          )}
          <Button variant="primary" size="sm" onClick={stop} loading={stopping} data-testid="record-stop">
            <Square className="h-3.5 w-3.5" /> {ended ? "Transcribe what was saved" : "Stop and transcribe"}
          </Button>
        </div>
      </div>
      {ended ? (
        <Callout tone="warning" data-testid="record-ended">
          The recording ended because {REASONS[recording.reason ?? "interrupted"] ?? recording.reason}. What was recorded is kept.
        </Callout>
      ) : <Meters levels={recording.levels} />}
      {live?.state === "paused_memory" && (
        <p className="text-xs text-warning" data-testid="live-paused">Live transcript paused: the Mac is short of memory. It catches up later; the recording continues.</p>
      )}
      {(recording.warnings ?? []).map((w, i) => (
        <p key={`${w.kind}-${i}`} className={clsx("flex items-center gap-1.5 text-xs", w.kind === "mic_switched" ? "text-muted" : "text-warning")}
           data-testid={`record-warning-${w.kind}`}>
          {w.kind === "mic_switched" ? <Mic className="h-3 w-3" /> : <AlertTriangle className="h-3 w-3" />} {w.message}
        </p>
      ))}
    </div>
  );
}

/** "What to record" and the other choices, then Start. */
export function RecordTab({ onStarted }: { onStarted: (job: Job) => void }) {
  const { prefs, recording, refreshRecording, setLivePanelOpen } = useApp();
  const [sources, setSources] = useState<RecordSources | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [kind, setKind] = useState<RecordSource["kind"]>("app");
  const [app, setApp] = useState("");
  const [display, setDisplay] = useState<number>(0);
  const [mic, setMic] = useState("");
  const [live, setLive] = useState(false);
  const [title, setTitle] = useState("");
  const [speakers, setSpeakers] = useState(0);
  const [speakerModel, setSpeakerModel] = useState(prefs?.speaker_model ?? "");
  const [framesModel, setFramesModel] = useState(prefs?.vision_model ?? "");
  const [summaryModel, setSummaryModel] = useState(prefs?.summary_model ?? "");
  const [after, setAfter] = useState(false);
  const [starting, setStarting] = useState(false);
  const active = !!recording && recording.state !== "idle";

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await api.get<RecordSources>("/api/meetings/record/sources");
      setSources(data);
      const last = data.last.source;
      const lastApp = last?.kind === "app" && data.apps.some((a) => a.bundle_id === last.id) ? String(last.id) : "";
      const firstCall = data.apps.find((a) => a.meeting || a.remote)?.bundle_id ?? "";
      setKind(last?.kind === "display" || last?.kind === "audio" ? last.kind : "app");
      setApp(lastApp || firstCall || data.apps[0]?.bundle_id || "");
      setDisplay(last?.kind === "display" ? Number(last.id) : data.displays.find((d) => d.main)?.id ?? data.displays[0]?.id ?? 0);
      setMic(data.last.mic && data.microphones.some((m) => m.id === data.last.mic) ? data.last.mic : "");
      setLive(data.last.live ?? data.live_default);
    } catch (err) {
      setError(err as ApiError);
    }
  }, []);
  useEffect(() => { if (!active) load(); }, [active, load]);

  if (active) return <RecordingBar onStopped={(job) => job && onStarted(job)} />;
  if (error) {
    return (
      <div className="space-y-3">
        <PermissionHelp error={error} />
        <Button variant="outline" size="sm" onClick={load}>Try again</Button>
      </div>
    );
  }
  if (!sources) return <div className="flex items-center gap-2 text-[13px] text-muted"><Spinner /> Looking for open apps…</div>;

  const chosenMic = sources.microphones.find((m) => m.id === mic) ?? sources.microphones.find((m) => m.default);
  const picker = !!sources.picker;  // macOS's picker chooses the window or screen at Start
  const source: RecordSource = picker ? { kind }
    : kind === "app" ? { kind, id: app } : kind === "display" ? { kind, id: display } : { kind: "audio" };
  const start = async () => {
    setStarting(true);
    const options: StartOptions = { speakers: speakers || null, speaker_model: speakerModel, frames_model: framesModel,
                                    summary_model: summaryModel, after };
    try {
      saveOptions(options);
      await api.post<RecordStatus>("/api/meetings/record/start", { source, mic: mic || null, live, title });
      await refreshRecording();
      if (live) setLivePanelOpen(true);
      toast.success("Recording", { description: "Stop it here or from the top bar on any page." });
    } catch (err) {
      setError(err as ApiError);
    } finally {
      setStarting(false);
    }
  };
  const group = (label: string, list: typeof sources.apps) => list.length > 0 && (
    <optgroup label={label}>{list.map((a) => <option key={a.bundle_id} value={a.bundle_id}>{a.name}{a.windows[0]?.title ? ` — ${a.windows[0].title}` : ""}</option>)}</optgroup>
  );
  return (
    <div className="space-y-4" data-testid="record-tab">
      <p className="text-[13px] text-muted">Quorum records the call on this Mac and keeps <b>your microphone</b> and <b>the call&apos;s sound</b> apart,
        so your words are marked as yours. Everything stays on this Mac.</p>
      <fieldset className="space-y-2">
        <legend className="mb-1 text-[13px] font-medium">What to record</legend>
        {([["app", "Meeting app", "The call's window (Teams, Zoom, Meet…) or a remote desktop app. Other apps' sounds stay out."],
           ["display", "Entire screen", "Everything on one display, with every app's sound."],
           ["audio", "Audio only", "No video: names can't be read from the screen."]] as const).map(([value, label, hint]) => (
          <label key={value} className={clsx("flex cursor-pointer items-start gap-2 rounded-lg border px-3 py-2 text-[13px]",
            kind === value ? "border-accent/50 bg-accent-soft" : "border-border hover:bg-hover")}>
            <input type="radio" name="record-kind" className="mt-0.5" checked={kind === value} onChange={() => setKind(value)}
                   data-testid={`record-kind-${value}`} />
            <span className="min-w-0 flex-1"><span className="font-medium">{label}</span><span className="block text-xs text-muted">{hint}</span>
              {value === "app" && kind === "app" && !picker && (
                <select aria-label="Meeting app" value={app} onChange={(e) => setApp(e.target.value)} data-testid="record-app"
                        className="mt-2 h-8 w-full rounded-lg border border-border-strong bg-surface px-2 text-[13px]">
                  {sources.apps.length === 0 && <option value="">No app windows open</option>}
                  {group("Calls", sources.apps.filter((a) => a.meeting))}
                  {group("Remote desktops", sources.apps.filter((a) => a.remote))}
                  {group("Other apps", sources.apps.filter((a) => !a.meeting && !a.remote))}
                </select>
              )}
              {value === "display" && kind === "display" && !picker && sources.displays.length > 1 && (
                <select aria-label="Display" value={display} onChange={(e) => setDisplay(Number(e.target.value))} data-testid="record-display"
                        className="mt-2 h-8 w-full rounded-lg border border-border-strong bg-surface px-2 text-[13px]">
                  {sources.displays.map((d) => <option key={d.id} value={d.id}>{d.name}{d.main ? " (main)" : ""}</option>)}
                </select>
              )}
            </span>
          </label>
        ))}
        {picker && (
          <p className="text-xs text-muted" data-testid="record-picker-note">
            When you press Start, macOS shows its own picker: click the meeting window{kind === "app" ? "" : " or choose the screen"}.
            {kind === "audio" ? " (Audio only still asks for a screen, to know whose sound to record; no video is kept.)" : ""}
            {" "}Quorum doesn&apos;t need the Screen Recording permission for this.</p>
        )}
        <p className="flex items-center gap-1.5 text-xs text-muted"><MonitorSmartphone className="h-3 w-3" />
          Call inside a remote desktop? Choose the remote desktop app, or the entire screen.</p>
      </fieldset>

      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <Label htmlFor="record-title">Title</Label>
          <Input id="record-title" placeholder="Call" value={title} onChange={(e) => setTitle(e.target.value)} />
        </div>
        <div>
          <Label htmlFor="record-mic" hint="Pick the same microphone as the meeting app">Your microphone</Label>
          <select id="record-mic" value={mic} onChange={(e) => setMic(e.target.value)} data-testid="record-mic"
                  className="h-9 w-full rounded-lg border border-border-strong bg-surface px-3 text-sm">
            <option value="">Same as Mac input{sources.microphones.find((m) => m.default) ? ` (${sources.microphones.find((m) => m.default)!.name})` : ""}</option>
            {sources.microphones.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
        </div>
      </div>
      <p className="text-xs text-muted">Headphones (wired or Bluetooth) work and are best: the call is recorded from the app, not the
        speakers, and your microphone only hears you. Plugging in a headset mid-call is fine.
        {mic && chosenMic?.bluetooth ? " A Bluetooth headset's microphone switches it to call mode, which lowers what you hear; the meeting app usually does this anyway." : ""}</p>

      <Switch id="record-live" checked={live} onChange={setLive} label="Transcribe during the meeting"
              description="Lets you check actions, ask questions or copy what was said while the call runs. Uses about 2 GB more memory during the call; the final transcript is still made after Stop." />

      <div className="space-y-3 rounded-xl border border-border p-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <ModelSelect kind="speaker" id="record-speaker-model" label="Speaker identification" defaultLabel="Recommended"
            value={speakerModel} onChange={setSpeakerModel} />
          {kind !== "audio" && (
            <ModelSelect kind="frames" id="record-frames-model" label="Names from the video" defaultLabel="Recommended"
              value={framesModel} onChange={setFramesModel} />
          )}
          <ModelSelect kind="summary" id="record-summary-model" label="Summary" defaultLabel="Ask me when it's written"
            value={summaryModel} onChange={setSummaryModel} />
          <div>
            <Label htmlFor="record-speakers">People in the call</Label>
            <select id="record-speakers" value={speakers} onChange={(e) => setSpeakers(Number(e.target.value))}
                    className="h-9 w-full rounded-lg border border-border-strong bg-surface px-3 text-sm">
              {SPEAKER_CHOICES.map((n) => <option key={n} value={n}>{n === 0 ? "Detect automatically" : `${n} including you`}</option>)}
            </select>
          </div>
        </div>
        <FramesNote speakerModel={speakerModel} />
        <label className="flex items-start gap-2 text-[13px]">
          <input type="checkbox" className="mt-0.5" checked={after} onChange={(e) => setAfter(e.target.checked)} data-testid="record-after" />
          <span>Identify speakers and write the summary when transcription finishes</span>
        </label>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="primary" onClick={start} loading={starting} data-testid="record-start"
                disabled={kind === "app" && !app && !picker}>
          <span className="h-2 w-2 rounded-full bg-current" /> Start recording
        </Button>
        <span className="text-xs text-muted" data-testid="record-start-hint">
          {starting && picker ? "Choose the meeting window or screen in the macOS picker…" : "Let people know you're recording."}</span>
      </div>
    </div>
  );
}

/** '● 12:03' in the top bar while a call is recorded, on every page; opens the live transcript or Meetings. */
export function RecordingIndicator() {
  const { recording, setLivePanelOpen } = useApp();
  const router = useRouter();
  const open = useOpenAlerts(recording?.live ? recording.id : null);
  if (!recording || recording.state === "idle") return null;
  const ended = recording.state !== "recording";
  return (
    <div className="flex items-center gap-1" data-testid="recording-indicator">
      <button onClick={() => router.push("/meetings")} aria-label="Recording: open Meetings"
              className={clsx("flex items-center gap-1.5 rounded-lg px-2 py-1 text-[13px] font-medium tabular-nums",
                ended ? "bg-warning-soft text-warning" : "bg-danger-soft text-danger")}>
        <span className={clsx("h-2 w-2 rounded-full", ended ? "bg-warning" : "animate-pulse bg-danger")} />
        {ended ? "Recording ended" : clock(recording.elapsed)}
      </button>
      {recording.live && (
        <Button variant="ghost" size="icon-sm" aria-label={open ? `Live transcript, ${open} alert${open === 1 ? "" : "s"}` : "Live transcript"}
                data-testid="indicator-live" className="relative" onClick={() => setLivePanelOpen(true)}>
          <Radio className="h-3.5 w-3.5" />
          {open > 0 && <span className="absolute -right-1 -top-1 min-w-4 rounded-full bg-danger px-1 text-[10px] font-semibold leading-4 text-white"
                             data-testid="indicator-alerts">{open}</span>}
        </Button>
      )}
    </div>
  );
}

const LIVE_STATES: Record<string, string> = {
  starting: "Starting…", running: "Live", catching_up: "Catching up", paused_memory: "Paused (low memory)",
  done: "Call ended", error: "Stopped", final: "Final transcript ready",
};

/** The live transcript, docked on the right on every page: lines as they come, Copy, Ask in chat, Actions so far. */
export function LivePanel() {
  const { recording, setLivePanelOpen, selectedModel, refreshSessions } = useApp();
  const router = useRouter();
  const [draft, setDraft] = useState<LiveDraft | null>(null);
  const [lines, setLines] = useState<LiveSegment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const [tab, setTab] = useState<"transcript" | "alerts">("transcript");
  const recId = recording?.id;
  const count = useRef(0);

  useEffect(() => { count.current = 0; setLines([]); setDraft(null); }, [recId]);
  useEffect(() => {
    let stopped = false;
    const poll = async () => {
      try {
        const data = await api.get<LiveDraft>(`/api/meetings/record/live?since=${count.current}${recId ? `&id=${recId}` : ""}`);
        if (stopped) return;
        if (data.segments.length) {
          count.current += data.segments.length;
          setLines((l) => [...l, ...data.segments]);
        }
        setDraft(data);
        setError(null);
      } catch (err) {
        if (!stopped) setError((err as Error).message);
      }
    };
    poll();
    const timer = window.setInterval(poll, 3000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [recId]);
  useEffect(() => {
    const el = listRef.current;
    if (el && follow.current) el.scrollTo({ top: el.scrollHeight });
  }, [lines.length]);

  const id = draft?.id ?? recId;
  const copy = async () => {
    try {
      const text = await api.text(`/api/meetings/record/live/markdown${id ? `?id=${id}` : ""}`);
      await navigator.clipboard.writeText(text);
      toast.success("Copied the transcript so far");
    } catch (err) {
      toast.error("Couldn't copy", { description: (err as Error).message });
    }
  };
  const ask = async (question?: string) => {
    try {
      const s = await api.post<Session>("/api/sessions", { model_id: selectedModel?.id ?? "" });
      if (draft?.transcript_id) await api.post(`/api/sessions/${s.id}/transcripts`, { transcript_id: draft.transcript_id });
      else await api.post(`/api/sessions/${s.id}/attachments/live`, { recording_id: id ?? null });
      if (question) {
        try { window.sessionStorage.setItem(`quorum:ask:${s.id}`, question); } catch { /* private mode */ }
      }
      await refreshSessions();
      router.push(`/c/${s.id}`);
    } catch (err) {
      toast.error("Couldn't start a chat", { description: (err as Error).message });
    }
  };
  const state = draft?.state ?? "starting";
  const alerts = useOpenAlerts(id);
  const showLine = (seconds: number) => {
    setTab("transcript");
    window.setTimeout(() => {
      const el = listRef.current?.querySelector<HTMLElement>(`[data-start="${Math.floor(seconds)}"]`);
      el?.scrollIntoView({ block: "center" });
      el?.classList.add("bg-accent-soft");
    }, 50);
  };
  return (
    <aside className="flex w-full max-w-[380px] shrink-0 flex-col border-l border-border bg-surface max-lg:fixed max-lg:inset-y-0 max-lg:right-0 max-lg:z-40 max-lg:shadow-xl"
           aria-label="Live transcript" data-testid="live-panel">
      <div className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-3">
        <Radio className={clsx("h-4 w-4", state === "running" || state === "catching_up" ? "text-danger" : "text-muted")} />
        <span className="text-sm font-semibold">Live transcript</span>
        <span className="text-xs text-muted" data-testid="live-state">{LIVE_STATES[state] ?? state}
          {draft?.behind_seconds && draft.behind_seconds > 45 ? ` · ${clock(draft.behind_seconds)} behind` : ""}</span>
        <Button variant="ghost" size="icon-sm" className="ml-auto" aria-label="Close live transcript" onClick={() => setLivePanelOpen(false)}>
          <X className="h-4 w-4" />
        </Button>
      </div>
      <div className="flex flex-wrap gap-1.5 border-b border-border px-3 py-2">
        <Button size="sm" variant="outline" onClick={copy} data-testid="live-copy"><Copy className="h-3.5 w-3.5" /> Copy</Button>
        <Button size="sm" variant="outline" onClick={() => ask()} data-testid="live-ask"><MessageSquare className="h-3.5 w-3.5" /> Ask in chat</Button>
        <Button size="sm" variant="outline" onClick={() => ask(ACTIONS_PROMPT)} data-testid="live-actions"><ListChecks className="h-3.5 w-3.5" /> Actions so far</Button>
      </div>
      {error && <p className="px-3 py-2 text-xs text-danger">{error}</p>}
      <div className="flex gap-1 border-b border-border px-3 pt-1.5" role="tablist" aria-label="Live transcript views">
        {(["transcript", "alerts"] as const).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)} data-testid={`live-tab-${t}`}
                  className={clsx("-mb-px border-b-2 px-2 py-1.5 text-[13px] font-medium",
                    tab === t ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg")}>
            {t === "transcript" ? "Transcript" : "Alerts"}
            {t === "alerts" && alerts > 0 && <span className="ml-1 rounded-full bg-danger px-1.5 text-[10px] text-white">{alerts}</span>}
          </button>
        ))}
      </div>
      {tab === "alerts" ? (
        <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3" data-testid="live-alerts">
          {id ? <AlertsView target={{ recording_id: id }} onLine={showLine} compact /> : <p className="text-[13px] text-muted">No call yet.</p>}
        </div>
      ) : (
      <div ref={listRef} className="min-h-0 flex-1 space-y-2 overflow-y-auto px-3 py-3 text-[13px]" data-testid="live-lines"
           onScroll={(e) => { const el = e.currentTarget; follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60; }}>
        {lines.length === 0 ? (
          <p className="text-muted">{state === "paused_memory" ? "Paused while the Mac is short of memory." : "Lines appear about every 30 seconds."}</p>
        ) : lines.map((l, i) => (
          <p key={i} data-start={Math.floor(l.start)} className="rounded transition-colors"><span className="mr-1 text-xs tabular-nums text-faint">{clock(l.start)}</span>
            <span className={clsx("font-medium", l.speaker === "You" ? "text-accent" : "text-fg")}>{l.speaker}:</span> {l.text}</p>
        ))}
      </div>
      )}
      <p className="border-t border-border px-3 py-2 text-[11px] text-faint">A rough draft: “You” is your microphone, other names are read from the screen.
        The final transcript replaces it after Stop.</p>
    </aside>
  );
}
