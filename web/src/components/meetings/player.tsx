"use client";

/**
 * MeetingPlayer: plays a meeting's recording on its page, from this Mac (served by the API from the transcriber).
 *
 * - A call Quorum recorded has the call and your microphone on separate tracks: they play as two sounds with their
 *   own volume ("Call", "You"), next to the (muted) video. Other recordings play their own sound (several tracks are
 *   mixed once by the transcriber).
 * - `quorum:play` {seconds} (a transcript line's time) seeks there and plays; while playing, `quorum:playing`
 *   {seconds} tells the transcript which line is being heard.
 */
import { Gauge, Headphones, Mic, Pause, Play, RotateCcw, RotateCw, Video, VideoOff } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui";
import { api, API_URL } from "@/lib/api";
import { clsx, fmtTime } from "@/lib/format";

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

export function MeetingPlayer({ transcriptId, fallbackDuration }: { transcriptId: string; fallbackDuration: number }) {
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
    return (
      <p className="rounded-lg border border-dashed border-border px-3 py-2 text-xs text-muted" data-testid="player-unavailable">
        Can&apos;t play this meeting: {info.message}{info.path ? <> (<span className="font-mono">{info.path}</span>)</> : null}
      </p>
    );
  }

  const volumeRows = parts.length ? parts.map((p) => ({ key: p.part, label: p.label })) : [{ key: "video", label: "Sound" }];
  const icon = (key: string) => (key === "mic" ? <Mic className="h-3.5 w-3.5" /> : <Headphones className="h-3.5 w-3.5" />);
  return (
    <div className="sticky top-0 z-10 space-y-2 rounded-xl border border-border bg-surface p-3 shadow-card" data-testid="player"
         data-time={time.toFixed(1)} data-playing={playing || undefined}>
      {info.video && (
        <video ref={videoRef} src={src("video")} muted={parts.length > 0} playsInline preload="metadata"
               className={clsx("max-h-48 w-full rounded-lg bg-black object-contain", !showVideo && "hidden")}
               data-testid="player-video" onClick={() => (playing ? pause() : play())} />
      )}
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
        {info.video && (
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
