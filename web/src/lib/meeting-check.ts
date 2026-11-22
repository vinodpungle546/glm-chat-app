// The meeting check on the client: alerts per meeting (during a call: its recording; afterwards: its transcript),
// running a check (SSE, with the privacy question for a cloud check model), Dismiss / Track, and the pop-up.
// A tiny store shared by the live panel, the background runner (automatic checks) and the meeting page.

import { useSyncExternalStore } from "react";
import { api, stream, type MeetingAlert } from "@/lib/api";

export type CheckTarget = { recording_id?: string | null; transcript_id?: string | null };

export interface CheckState {
  alerts: MeetingAlert[];
  key: string | null;
  running: boolean;
  status: string | null;
  error: string | null;
  /** The privacy question when the check model is a cloud model. */
  confirm: { reasons: string[]; local_model: string | null; model?: string } | null;
  last: string | null;   // when the last check finished
}

const EMPTY: CheckState = { alerts: [], key: null, running: false, status: null, error: null, confirm: null, last: null };
const states = new Map<string, CheckState>();
const listeners = new Set<() => void>();

/** The store key for a meeting. */
export function targetKey(target: CheckTarget): string {
  return target.transcript_id ? `t:${target.transcript_id}` : `rec:${target.recording_id ?? "current"}`;
}

function set(key: string, patch: Partial<CheckState>) {
  states.set(key, { ...(states.get(key) ?? EMPTY), ...patch });
  listeners.forEach((l) => l());
}

/** The check state of one meeting (re-renders on changes). */
export function useCheck(target: CheckTarget): CheckState {
  const key = targetKey(target);
  return useSyncExternalStore(
    (l) => { listeners.add(l); return () => listeners.delete(l); },
    () => states.get(key) ?? EMPTY,
    () => EMPTY,
  );
}

function query(target: CheckTarget) {
  const q = new URLSearchParams();
  if (target.recording_id) q.set("recording_id", target.recording_id);
  if (target.transcript_id) q.set("transcript_id", target.transcript_id);
  return q.toString();
}

/** Loads the alerts already found for a meeting. */
export async function loadAlerts(target: CheckTarget) {
  if (!target.recording_id && !target.transcript_id) return;
  try {
    const data = await api.get<{ alerts: MeetingAlert[]; key: string }>(`/api/meetings/check/alerts?${query(target)}`);
    set(targetKey(target), { alerts: data.alerts, key: data.key });
  } catch {
    /* transcriber or API not reachable: keep what we have */
  }
}

/** The pop-up: only the count and kinds ("2 alerts: 1 conflict, 1 still pending"), never the content. Clicking it
 *  brings Quorum forward and opens the alerts. macOS hides notifications while you share your screen (unless
 *  you allowed them in System Settings → Notifications). */
export function notify(summary: string, onClick: () => void) {
  if (typeof window === "undefined" || !("Notification" in window) || Notification.permission !== "granted") return;
  try {
    const n = new Notification("Quorum meeting check", { body: summary, tag: "quorum-meeting-check" });
    n.onclick = () => { window.focus(); onClick(); n.close(); };
  } catch {
    /* notifications unavailable in this browser */
  }
}

/** Asks the browser for permission to show pop-ups (called when the setting is switched on or on the first check). */
export async function askNotificationPermission(): Promise<boolean> {
  if (typeof window === "undefined" || !("Notification" in window)) return false;
  if (Notification.permission === "default") {
    try { await Notification.requestPermission(); } catch { /* ignored */ }
  }
  return Notification.permission === "granted";
}

type CheckEvent = { type: string; text?: string; message?: string; alerts?: MeetingAlert[]; new?: string[];
                    summary?: string; key?: string; reasons?: string[]; local_model?: string | null; model?: string };

/** Runs a check. Returns the new alerts (empty when none, or when the privacy question is waiting for an answer). */
export async function runCheck(target: CheckTarget, confirm?: "local" | "cloud"): Promise<{ fresh: MeetingAlert[]; summary: string }> {
  const key = targetKey(target);
  if (states.get(key)?.running) return { fresh: [], summary: "" };
  set(key, { running: true, status: "Checking…", error: null, confirm: null });
  let fresh: MeetingAlert[] = [];
  let summary = "";
  try {
    await stream<CheckEvent>("/api/meetings/check", { ...target, confirm: confirm ?? null }, (e) => {
      if (e.type === "status" || e.type === "notice") set(key, { status: e.text ?? null });
      else if (e.type === "confirm") set(key, { confirm: { reasons: e.reasons ?? [], local_model: e.local_model ?? null, model: e.model } });
      else if (e.type === "error") set(key, { error: e.message ?? "The check failed." });
      else if (e.type === "alerts") {
        const all = e.alerts ?? [];
        fresh = all.filter((a) => (e.new ?? []).includes(a.id));
        summary = e.summary ?? "";
        set(key, { alerts: all, key: e.key ?? states.get(key)?.key ?? null });
      }
    });
  } catch (err) {
    set(key, { error: (err as Error).message });
  } finally {
    set(key, { running: false, status: null, last: new Date().toISOString() });
  }
  return { fresh, summary };
}

/** Dismiss, Track (→ My actions) or restore an alert. */
export async function actOn(target: CheckTarget, alert: MeetingAlert, action: "dismiss" | "track" | "restore") {
  const key = targetKey(target);
  const updated = await api.post<MeetingAlert>(`/api/meetings/check/alerts/${alert.key}/${alert.id}/${action}`);
  set(key, { alerts: (states.get(key)?.alerts ?? []).map((a) => (a.id === alert.id ? { ...updated, key: alert.key } : a)) });
  if (action === "track") window.dispatchEvent(new CustomEvent("quorum:my-actions"));  // My actions reloads
}
