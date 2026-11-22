/** Small formatting helpers shared by the components (times, sizes, dates, class names). */
/** Joins CSS class names, skipping false/null/undefined: clsx('a', ok && 'b'). */
export function clsx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/** A time position as 'mm:ss', or 'h:mm:ss' after an hour (e.g. '03:12', '1:02:03'). */
export function fmtTime(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}

/** A duration as '45 s', '3 min 20 s' (under 10 minutes), '12 min' or '1 h 5 min'. */
export function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h) return `${h} h ${m} min`;
  const sec = s % 60;
  return sec && m < 10 ? `${m} min ${sec} s` : `${m} min`;
}

/** A file size as '512 KB', '12 MB' or '1.4 GB'. */
export function fmtBytes(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(0)} MB`;
  return `${Math.max(1, Math.round(bytes / 1e3))} KB`;
}

/** The sidebar's date group: 'Today', 'Yesterday', 'Previous 7 days', 'Previous 30 days', or the month ('November 2026'). */
export function relativeDay(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const days = Math.floor((new Date(now.toDateString()).getTime() - new Date(d.toDateString()).getTime()) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "Previous 7 days";
  if (days < 30) return "Previous 30 days";
  return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

/** A short date and time in the browser's language, e.g. '21 Nov, 17:24'. */
export function shortDate(iso: string): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export const MEDIA_EXTENSIONS = ["mp4", "m4a", "mov", "mkv", "webm", "wav", "mp3", "aac", "flac", "ogg", "m4v"];
export const CONVERSATION_EXTENSIONS = ["txt", "md", "vtt", "srt", "docx", "pdf"];
export const DOCUMENT_EXTENSIONS = ["txt", "md", "csv", "log", "pdf", "docx", "xlsx", "xlsm", "pptx"];

/** A file's extension in lower case, without the dot ('' if none). */
export function extension(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
}

// Stable speaker colours (work in light and dark)
const SPEAKER_COLORS = ["#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#a855f7", "#14b8a6", "#ec4899",
  "#84cc16", "#f97316", "#06b6d4", "#8b5cf6", "#22c55e", "#e11d48", "#3b82f6"];

/** A stable colour per speaker (by their position in the speaker list). */
export function speakerColor(speaker: string, speakers: string[]): string {
  const i = Math.max(0, speakers.indexOf(speaker));
  return SPEAKER_COLORS[i % SPEAKER_COLORS.length];
}

/** Up to two initials of a name, e.g. 'Priya Sharma' -> 'PS', 'Speaker 2' -> 'S2'. */
export function initials(name: string): string {
  const words = name.replace(/^Speaker\s+/i, "S").split(/\s+/).filter(Boolean);
  return (words.length > 1 ? words[0][0] + words[1][0] : (words[0] ?? "?").slice(0, 2)).toUpperCase();
}

/** True on Apple devices (to show ⌘ instead of Ctrl). */
export const isMac = () => typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
/** The shortcut key symbol for this platform: '⌘' or 'Ctrl'. */
export const modKey = () => (isMac() ? "⌘" : "Ctrl");
