/**
 * The meeting transcript as text, the way the page shows it: corrected words and speaker names (display names),
 * consecutive lines of one speaker kept together. Formats: plain "Name: words", with times, Markdown, and
 * subtitles (WebVTT / SRT, also used for the player's captions).
 */
import type { Transcript } from "@/lib/api";

export type ExportFormat = "plain" | "timed" | "markdown" | "vtt" | "srt";

export interface Line {
  start: number;
  end: number;
  name: string;
  text: string;
}

/** The transcript's lines with display names (what the page shows). */
export function lines(transcript: Transcript): Line[] {
  return transcript.segments.map((s) => ({
    start: s.start, end: s.end, text: s.text.trim(),
    name: transcript.display_names?.[s.speaker] || s.speaker,
  }));
}

/** 12:03, or 1:02:03 past the hour. */
export function clock(seconds: number): string {
  const t = Math.max(0, Math.floor(seconds));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = String(t % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

/** 00:01:02.500 (VTT) or 00:01:02,500 (SRT). */
function stamp(seconds: number, comma: boolean): string {
  const ms = Math.max(0, Math.round(seconds * 1000));
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  const rest = ms % 1000;
  const pad = (n: number, w = 2) => String(n).padStart(w, "0");
  return `${pad(h)}:${pad(m)}:${pad(s)}${comma ? "," : "."}${pad(rest, 3)}`;
}

/** Consecutive lines of the same speaker joined into one paragraph. */
function paragraphs(all: Line[]): Line[] {
  const out: Line[] = [];
  for (const line of all) {
    const last = out[out.length - 1];
    if (last && last.name === line.name) {
      last.text = `${last.text} ${line.text}`.trim();
      last.end = line.end;
    } else {
      out.push({ ...line });
    }
  }
  return out;
}

export function toPlain(transcript: Transcript): string {
  return paragraphs(lines(transcript)).map((l) => `${l.name}: ${l.text}`).join("\n\n") + "\n";
}

export function toTimed(transcript: Transcript): string {
  const timed = transcript.timestamps !== false;
  return paragraphs(lines(transcript)).map((l) => (timed ? `[${clock(l.start)}] ` : "") + `${l.name}: ${l.text}`).join("\n\n") + "\n";
}

export function toMarkdown(transcript: Transcript): string {
  const timed = transcript.timestamps !== false;
  const head = `# ${transcript.title}\n\n`;
  return head + paragraphs(lines(transcript))
    .map((l) => `**${l.name}**${timed ? ` _${clock(l.start)}_` : ""}  \n${l.text}`).join("\n\n") + "\n";
}

/** One cue per line (subtitles show what is said now, not a whole paragraph). */
export function toVtt(transcript: Transcript): string {
  return "WEBVTT\n\n" + lines(transcript)
    .map((l) => `${stamp(l.start, false)} --> ${stamp(Math.max(l.end, l.start + 0.5), false)}\n<v ${l.name}>${l.name}: ${l.text}`)
    .join("\n\n") + "\n";
}

export function toSrt(transcript: Transcript): string {
  return lines(transcript)
    .map((l, i) => `${i + 1}\n${stamp(l.start, true)} --> ${stamp(Math.max(l.end, l.start + 0.5), true)}\n${l.name}: ${l.text}`)
    .join("\n\n") + "\n";
}

export const FORMATS: { format: ExportFormat; label: string; ext: string; type: string }[] = [
  { format: "plain", label: "Plain text (Name: words)", ext: "txt", type: "text/plain" },
  { format: "timed", label: "With times ([12:03] Name: words)", ext: "txt", type: "text/plain" },
  { format: "markdown", label: "Markdown", ext: "md", type: "text/markdown" },
  { format: "vtt", label: "Subtitles (.vtt)", ext: "vtt", type: "text/vtt" },
  { format: "srt", label: "Subtitles (.srt)", ext: "srt", type: "application/x-subrip" },
];

export function render(transcript: Transcript, format: ExportFormat): string {
  switch (format) {
    case "timed": return toTimed(transcript);
    case "markdown": return toMarkdown(transcript);
    case "vtt": return toVtt(transcript);
    case "srt": return toSrt(transcript);
    default: return toPlain(transcript);
  }
}

/** Saves `text` as a file through the browser (a temporary link to a Blob). */
export function download(text: string, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type: `${type};charset=utf-8` }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** A file name from the meeting title (no characters macOS or Windows refuse). */
export function fileName(title: string, ext: string): string {
  return `${(title || "Transcript").replace(/[\\/:*?"<>|]+/g, "-").trim()}.${ext}`;
}
