"use client";

/**
 * MeetingPlayer: plays a meeting's recording on its page, from this Mac (served by the API from the transcriber).
 *
 * - A call Quorum recorded has the call and your microphone on separate tracks: they play as two sounds with their
 *   own volume ("Call", "You"), next to the (muted) video. Other recordings play their own sound (several tracks are
 *   mixed once by the transcriber).
 * - `quorum:play` {seconds} (a transcript line's time) seeks there and plays; while playing, `quorum:playing`
 *   {seconds} tells the transcript which line is being heard.
 * - Maximize (browser full screen, Esc leaves), three video sizes on the page, and captions (CC): the line being
 *   spoken with the speaker's name, from the transcript as shown (corrections and name edits included).
 * - FindRecording: when the recording moved (an upload copy deleted, the original still on the Desktop), the
 *   transcriber looks for it by name in Desktop, Downloads, Movies, Documents and the meetings folders; a file as
 *   long as the meeting is linked with one click (cloned into uploads), else the recording can be uploaded again.
 */
import {
  AlertTriangle, Captions, CaptionsOff, Gauge, Headphones, Maximize2, Mic, Minimize2, Pause, Play, RotateCcw, RotateCw,
  Search, Upload, Video, VideoOff,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui";
import { api, API_URL, uploadInChunks } from "@/lib/api";
import { clsx, fmtBytes, fmtTime } from "@/lib/format";

interface MediaInfo {
  available: boolean;
  reason?: "text" | "missing" | "unreadable";
  message?: string;
  path?: string;
  name?: string;
  video?: boolean;
  parts?: { part: "call" | "mic" | "mix" | "audio"; label: string }[];
  duration?: number;
}

const RATES = [1, 1.25, 1.5, 2, 0.75];
const DRIFT = 0.3;  // seconds a follower may lag before it is moved back in step
const VOLUME_KEY = "quorum:player-volume";
const VIEW_KEY = "quorum:player-view";  // {size, captions}: remembered on this browser
const SIZES = { s: "max-h-48", m: "max-h-[50vh]", l: "max-h-[75vh]" } as const;
type VideoSize = keyof typeof SIZES;

/** One caption: a transcript line with its speaker's display name. */
export interface Caption { start: number; end: number; name: string; text: string }

function loadView(): { size: VideoSize; captions: boolean } {
  try {
    const v = JSON.parse(localStorage.getItem(VIEW_KEY) ?? "{}") as { size?: VideoSize; captions?: boolean };
    return { size: v.size && v.size in SIZES ? v.size : "s", captions: v.captions ?? true };
  } catch {
    return { size: "s", captions: true };
  }
}

/** The caption at `t`: the last line that started by then, while it lasts (plus a moment to read it). */
function captionAt(captions: Caption[], t: number): Caption | null {
  let lo = 0, hi = captions.length - 1, found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (captions[mid].start <= t + 0.05) { found = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return found >= 0 && t <= captions[found].end + 1 ? captions[found] : null;
}

function loadVolumes(): Record<string, number> {
  try {
    return JSON.parse(localStorage.getItem(VOLUME_KEY) ?? "{}") as Record<string, number>;
  } catch {
    return {};
  }
}

/** Asks the meeting page's player to play from `seconds`. */
export function playFrom(seconds: number) {
  window.dispatchEvent(new CustomEvent("quorum:play", { detail: { seconds } }));
}

export function MeetingPlayer({ transcriptId, fallbackDuration, captions = [] }: {
  transcriptId: string; fallbackDuration: number; captions?: Caption[];
}) {
  const [info, setInfo] = useState<MediaInfo | null>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(fallbackDuration);
  const [rate, setRate] = useState(1);
  const [volumes, setVolumes] = useState<Record<string, number>>({});
  const [showVideo, setShowVideo] = useState(true);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const audioRefs = useRef<Record<string, HTMLAudioElement | null>>({});
  const boxRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState<VideoSize>("s");
  const [cc, setCc] = useState(true);
  const [full, setFull] = useState(false);

  useEffect(() => {
    const v = loadView();
    setSize(v.size);
    setCc(v.captions);
    const onFull = () => setFull(!!boxRef.current && document.fullscreenElement === boxRef.current);
    document.addEventListener("fullscreenchange", onFull);
    return () => document.removeEventListener("fullscreenchange", onFull);
  }, []);
  const saveView = (next: { size?: VideoSize; captions?: boolean }) => {
    try { localStorage.setItem(VIEW_KEY, JSON.stringify({ size, captions: cc, ...next })); } catch { /* not remembered */ }
  };
  const toggleFull = async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await boxRef.current?.requestFullscreen();
    } catch (err) {
      setError(`Full screen isn't available here (${(err as Error).message}).`);
    }
  };

  useEffect(() => {
    setVolumes(loadVolumes());
    api.get<MediaInfo>(`/api/meetings/transcripts/${transcriptId}/media`)
      .then(setInfo)
      .catch((err: Error) => setInfo({ available: false, reason: "unreadable", message: err.message }));
  }, [transcriptId]);

  const parts = useMemo(() => info?.parts ?? [], [info]);
  const src = (part: string) => `${API_URL}/api/meetings/transcripts/${transcriptId}/media/${part}`;
  /** Every element, the one that keeps time first (the first sound, else the video). */
  const elements = useCallback((): HTMLMediaElement[] => {
    const sounds = parts.map((p) => audioRefs.current[p.part]).filter(Boolean) as HTMLMediaElement[];
    const video = videoRef.current ? [videoRef.current] : [];
    return sounds.length ? [...sounds, ...video] : video;
  }, [parts]);
  const master = useCallback(() => elements()[0] ?? null, [elements]);

  const seek = useCallback((seconds: number) => {
    const t = Math.max(0, seconds);
    for (const el of elements()) el.currentTime = t;
    setTime(t);
  }, [elements]);
  const play = useCallback(async () => {
    const all = elements();
    if (!all.length) return;
    const t = all[0].currentTime;
    for (const el of all.slice(1)) el.currentTime = t;
    try {
      await Promise.all(all.map((el) => el.play()));
    } catch (err) {
      setError(`The browser couldn't play the recording (${(err as Error).message}).`);
    }
  }, [elements]);
  const pause = useCallback(() => { for (const el of elements()) el.pause(); }, [elements]);

  // A transcript line's time: seek and play.
  useEffect(() => {
    const onPlay = (e: Event) => {
      const seconds = (e as CustomEvent<{ seconds: number }>).detail.seconds;
      seek(seconds);
      void play();
    };
    window.addEventListener("quorum:play", onPlay);
    return () => window.removeEventListener("quorum:play", onPlay);
  }, [seek, play]);

  // Keep time from the master; move the others back in step when they drift.
  useEffect(() => {
    const m = master();
    if (!m || !info?.available) return;
    const onTime = () => {
      setTime(m.currentTime);
      for (const el of elements().slice(1)) {
        if (!el.paused && Math.abs(el.currentTime - m.currentTime) > DRIFT) el.currentTime = m.currentTime;
      }
      if (!m.paused) window.dispatchEvent(new CustomEvent("quorum:playing", { detail: { seconds: m.currentTime } }));
    };
    const onPlay = () => setPlaying(true);
    const onPause = () => { setPlaying(false); for (const el of elements().slice(1)) el.pause(); };
    const onMeta = () => { if (Number.isFinite(m.duration) && m.duration > 0) setDuration(m.duration); setReady(true); };
    const onError = () => setError("This browser can't play the recording.");
    m.addEventListener("timeupdate", onTime);
    m.addEventListener("play", onPlay);
    m.addEventListener("pause", onPause);
    m.addEventListener("ended", onPause);
    m.addEventListener("loadedmetadata", onMeta);
    m.addEventListener("error", onError);
    if (m.readyState >= 1) onMeta();
    return () => {
      m.removeEventListener("timeupdate", onTime);
      m.removeEventListener("play", onPlay);
      m.removeEventListener("pause", onPause);
      m.removeEventListener("ended", onPause);
      m.removeEventListener("loadedmetadata", onMeta);
      m.removeEventListener("error", onError);
    };
  }, [info, master, elements]);

  // Volumes and speed on every sound.
  useEffect(() => {
    for (const p of parts) {
      const el = audioRefs.current[p.part];
      if (el) el.volume = volumes[p.part] ?? 1;
    }
    if (videoRef.current && !parts.length) videoRef.current.volume = volumes.video ?? 1;
    for (const el of elements()) el.playbackRate = rate;
  }, [volumes, rate, parts, elements]);

  const setVolume = (key: string, value: number) => {
    setVolumes((v) => {
      const next = { ...v, [key]: value };
      try { localStorage.setItem(VOLUME_KEY, JSON.stringify(next)); } catch { /* private mode: not remembered */ }
      return next;
    });
  };

  if (!info) return null;
  if (!info.available) {
    if (info.reason === "text") return null;
    if (info.reason === "missing") {
      return <FindRecording transcriptId={transcriptId} info={info} meetingSeconds={fallbackDuration} onLinked={setInfo} />;
    }
    return (
      <p className="rounded-lg border border-dashed border-border px-3 py-2 text-xs text-muted" data-testid="player-unavailable">
        Can&apos;t play this meeting: {info.message}{info.path ? <> (<span className="font-mono">{info.path}</span>)</> : null}
      </p>
    );
  }

  const volumeRows = parts.length ? parts.map((p) => ({ key: p.part, label: p.label })) : [{ key: "video", label: "Sound" }];
  const line = cc && captions.length ? captionAt(captions, time) : null;
  const overVideo = !!info.video && showVideo;
  const caption = line && (
    <span className={clsx("inline-block max-w-full rounded-md bg-black/75 px-2.5 py-1 text-center text-white", full ? "text-2xl" : "text-sm")}
          data-testid="player-caption">
      <span className="font-semibold">{line.name}:</span> {line.text}
    </span>
  );
  const icon = (key: string) => (key === "mic" ? <Mic className="h-3.5 w-3.5" /> : <Headphones className="h-3.5 w-3.5" />);
  return (
    <div ref={boxRef} data-testid="player" data-time={time.toFixed(1)} data-playing={playing || undefined}
         data-size={size} data-fullscreen={full || undefined}
         className={clsx("space-y-2 border border-border bg-surface p-3 shadow-card",
                         full ? "flex flex-col justify-center rounded-none" : "sticky top-0 z-10 rounded-xl")}>
      {info.video && (
        <div className={clsx("relative", !showVideo && "hidden", full && "flex-1 min-h-0")}>
          <video ref={videoRef} src={src("video")} muted={parts.length > 0} playsInline preload="metadata"
                 className={clsx("w-full rounded-lg bg-black object-contain", full ? "h-full max-h-[calc(100vh-9rem)]" : SIZES[size])}
                 data-testid="player-video" onClick={() => (playing ? pause() : play())} onDoubleClick={() => void toggleFull()} />
          {overVideo && caption && <div className="pointer-events-none absolute inset-x-3 bottom-3 flex justify-center">{caption}</div>}
        </div>
      )}
      {!overVideo && caption && <div className="flex justify-center">{caption}</div>}
      {parts.map((p) => (
        <audio key={p.part} ref={(el) => { audioRefs.current[p.part] = el; }} src={src(p.part)} preload="metadata"
               data-testid={`player-audio-${p.part}`} />
      ))}
      <div className="flex items-center gap-2">
        <Button size="icon-sm" variant="primary" aria-label={playing ? "Pause" : "Play"} onClick={() => (playing ? pause() : play())}
                data-testid="player-play" className="shrink-0">
          {playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
        </Button>
        <Button size="icon-sm" variant="ghost" aria-label="Back 10 seconds" className="shrink-0" onClick={() => seek(time - 10)}>
          <RotateCcw className="h-3.5 w-3.5" />
        </Button>
        <Button size="icon-sm" variant="ghost" aria-label="Forward 10 seconds" className="shrink-0" onClick={() => seek(time + 10)}>
          <RotateCw className="h-3.5 w-3.5" />
        </Button>
        <span className="w-24 shrink-0 font-mono text-xs tabular-nums text-muted" data-testid="player-clock">
          {fmtTime(time)} / {fmtTime(duration)}
        </span>
        <input type="range" min={0} max={Math.max(duration, 1)} step={0.1} value={Math.min(time, duration)}
               onChange={(e) => seek(Number(e.target.value))} aria-label="Position" data-testid="player-seek"
               className="min-w-0 flex-1 accent-[var(--accent)]" />
        <Button size="sm" variant="ghost" className="shrink-0 px-2 font-mono text-xs" aria-label="Playback speed"
                data-testid="player-rate" onClick={() => setRate(RATES[(RATES.indexOf(rate) + 1) % RATES.length])}>
          <Gauge className="h-3.5 w-3.5" /> {rate}×
        </Button>
        {captions.length > 0 && (
          <Button size="icon-sm" variant="ghost" className={clsx("shrink-0", cc && "text-accent")} aria-pressed={cc}
                  aria-label={cc ? "Turn captions off" : "Turn captions on"} data-testid="player-captions"
                  onClick={() => { setCc(!cc); saveView({ captions: !cc }); }}>
            {cc ? <Captions className="h-3.5 w-3.5" /> : <CaptionsOff className="h-3.5 w-3.5" />}
          </Button>
        )}
        {info.video && !full && showVideo && (
          <Button size="sm" variant="ghost" className="shrink-0 px-2 font-mono text-xs" aria-label="Video size"
                  data-testid="player-size" title="Video size on the page"
                  onClick={() => { const next = ({ s: "m", m: "l", l: "s" } as const)[size]; setSize(next); saveView({ size: next }); }}>
            {size.toUpperCase()}
          </Button>
        )}
        {info.video && (
          <Button size="icon-sm" variant="ghost" className="shrink-0" aria-label={full ? "Exit full screen" : "Maximize"}
                  data-testid="player-maximize" onClick={() => void toggleFull()}>
            {full ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
          </Button>
        )}
        {info.video && !full && (
          <Button size="icon-sm" variant="ghost" className="shrink-0" aria-label={showVideo ? "Hide video" : "Show video"}
                  data-testid="player-video-toggle" onClick={() => setShowVideo((v) => !v)}>
            {showVideo ? <VideoOff className="h-3.5 w-3.5" /> : <Video className="h-3.5 w-3.5" />}
          </Button>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        {volumeRows.map((v) => (
          <label key={v.key} className="flex items-center gap-1.5 text-xs text-muted">
            {icon(v.key)} {v.label}
            <input type="range" min={0} max={1} step={0.05} value={volumes[v.key] ?? 1} aria-label={`${v.label} volume`}
                   onChange={(e) => setVolume(v.key, Number(e.target.value))} data-testid={`player-volume-${v.key}`}
                   className="w-24 accent-[var(--accent)]" />
          </label>
        ))}
        {!ready && !error && <span className="text-xs text-faint">Loading the recording…</span>}
        {parts.length === 2 && <span className="text-xs text-faint">Turn one down to hear only the call or only you.</span>}
      </div>
      {error && (
        <p className="text-xs text-warning" data-testid="player-error">
          {error} Open it in QuickTime instead: <span className="font-mono">{info.path}</span>
        </p>
      )}
    </div>
  );
}

interface Candidate {
  path: string;
  name: string;
  folder: string;
  size: number;
  modified: string;
  duration: number | null;
  match: boolean;
}

/** The recording moved: find it again on this Mac (by name and length), or upload it, then play from it. */
function FindRecording({ transcriptId, info, meetingSeconds, onLinked }: {
  transcriptId: string; info: MediaInfo; meetingSeconds: number; onLinked: (info: MediaInfo) => void;
}) {
  const [found, setFound] = useState<{ candidates: Candidate[]; folders: string[] } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const base = `/api/meetings/transcripts/${transcriptId}/recording`;

  useEffect(() => {
    let cancelled = false;
    api.get<{ candidates: Candidate[]; folders: string[] }>(`${base}/candidates`)
      .then((r) => { if (!cancelled) setFound(r); })
      .catch((err: Error) => { if (!cancelled) { setFound({ candidates: [], folders: [] }); setError(err.message); } });
    return () => { cancelled = true; };
  }, [base]);

  const link = async (path: string) => {
    setBusy(path);
    setError(null);
    try {
      onLinked(await api.post<MediaInfo>(base, { path }));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };
  const uploadFile = async (file: File) => {
    setError(null);
    setProgress(0);
    try {
      const path = await uploadInChunks(file, setProgress);
      setProgress(null);
      await link(path);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setProgress(null);
    }
  };

  const matches = found?.candidates.filter((c) => c.match) ?? [];
  const others = found?.candidates.filter((c) => !c.match) ?? [];
  return (
    <div className="space-y-2 rounded-lg border border-dashed border-border px-3 py-2.5 text-xs text-muted" data-testid="player-unavailable">
      <p>
        The recording isn&apos;t where it was transcribed from any more
        {info.name ? <> (<span className="font-mono">{info.name}</span>)</> : null}.
      </p>
      {!found && (
        <p className="flex items-center gap-1.5" data-testid="player-searching">
          <Search className="h-3.5 w-3.5 animate-pulse" /> Looking in your Desktop, Downloads, Movies, Documents and meetings folders…
        </p>
      )}
      {matches.map((c) => (
        <div key={c.path} className="flex flex-wrap items-center gap-2 rounded-md bg-surface-2 px-2.5 py-2 text-fg" data-testid="player-candidate">
          <div className="min-w-0 flex-1">
            <div>Found in your {c.folder}: <span className="font-medium">{c.name}</span></div>
            <div className="truncate text-muted" title={c.path}>
              {c.duration != null ? fmtTime(c.duration) : "?"} · {fmtBytes(c.size)} · {c.modified}
            </div>
          </div>
          <Button size="sm" variant="primary" loading={busy === c.path} disabled={!!busy} onClick={() => void link(c.path)}
                  data-testid="player-relink">
            <Play className="h-3.5 w-3.5" /> Use this recording
          </Button>
        </div>
      ))}
      {others.map((c) => (
        <p key={c.path} className="flex items-start gap-1.5" data-testid="player-candidate-mismatch">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
          <span>
            <span className="font-mono">{c.path}</span> has the same name but is {c.duration != null ? fmtTime(c.duration) : "of unknown length"} long
            (the meeting is {fmtTime(meetingSeconds)}), so it&apos;s probably another recording.
          </span>
        </p>
      ))}
      {found && (
        <div className="flex flex-wrap items-center gap-2">
          {!matches.length && <span data-testid="player-not-found">Not found on this Mac.</span>}
          <Button size="sm" variant="secondary" loading={progress !== null} disabled={!!busy}
                  onClick={() => fileRef.current?.click()} data-testid="player-upload">
            <Upload className="h-3.5 w-3.5" />
            {progress !== null ? `Uploading… ${Math.round(progress)}%` : matches.length ? "Choose another file" : "Upload the recording"}
          </Button>
          <input ref={fileRef} type="file" accept="audio/*,video/*,.mov,.m4a,.mp4,.mkv,.webm,.wav,.mp3" hidden
                 data-testid="player-upload-input"
                 onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) void uploadFile(f); }} />
        </div>
      )}
      {error && <p className="text-danger" data-testid="player-link-error">{error}</p>}
    </div>
  );
}
