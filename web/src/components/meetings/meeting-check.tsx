"use client";

/**
 * The meeting check in the UI:
 *  - AlertsView: "Check now", the privacy question for a cloud check model, and the alerts (conflict, to raise
 *    with everyone, still pending, my action) with their line and source, and Dismiss / Track / Ask in chat.
 *  - MeetingCheckRunner: runs on every page while a call with a live transcript is recorded: keeps the alerts
 *    loaded, checks every N minutes when that's on, and shows the pop-up (count only) when that's on.
 *  - MyActions: the tracked actions of the active project, on the Meetings page.
 */
import { AlertTriangle, Check, Clock, Eye, ListChecks, Megaphone, MessageSquare, RefreshCw, Trash2, UserCheck, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { Badge, Button, Callout, Spinner } from "@/components/ui";
import { api, type AlertType, type MeetingAlert, type MyAction, type Session } from "@/lib/api";
import { clsx } from "@/lib/format";
import { actOn, askNotificationPermission, loadAlerts, notify, runCheck, useCheck, type CheckTarget } from "@/lib/meeting-check";

const KINDS: Record<AlertType, { label: string; tone: "danger" | "warning" | "neutral" | "success"; icon: React.ReactNode }> = {
  conflict: { label: "Conflict", tone: "danger", icon: <AlertTriangle className="h-3 w-3" /> },
  raise: { label: "To raise with everyone", tone: "warning", icon: <Megaphone className="h-3 w-3" /> },
  pending: { label: "Still pending", tone: "neutral", icon: <Clock className="h-3 w-3" /> },
  my_action: { label: "My action", tone: "success", icon: <UserCheck className="h-3 w-3" /> },
};
const ORDER: AlertType[] = ["conflict", "raise", "pending", "my_action"];

/** Where an alert comes from, in a few words. */
function sourceText(alert: MeetingAlert) {
  const s = alert.sources[0];
  if (!s) return "";
  if (s.kind === "memory") return `Memory: ${s.text}`;
  if (s.kind === "tracked") return `My actions: ${s.text}`;
  const what = s.kind === "passage" ? "said" : s.kind;
  return `${s.meeting ?? "Earlier meeting"}${s.date ? ` (${s.date})` : ""}${s.time ? ` at ${s.time}` : ""}, ${what}: ${s.text}`
    + (s.owner ? ` — ${s.owner}` : "") + (s.due ? `, due ${s.due}` : "");
}

/** Opens a chat about one alert, with the meeting (live draft or transcript) attached and the question sent. */
function useAskAboutAlert(target: CheckTarget) {
  const { selectedModel, refreshSessions } = useApp();
  const router = useRouter();
  return async (alert: MeetingAlert) => {
    try {
      const s = await api.post<Session>("/api/sessions", { model_id: selectedModel?.id ?? "" });
      if (target.transcript_id) await api.post(`/api/sessions/${s.id}/transcripts`, { transcript_id: target.transcript_id });
      else await api.post(`/api/sessions/${s.id}/attachments/live`, { recording_id: target.recording_id ?? null });
      const question = `The meeting check found: “${alert.text}”${alert.line ? ` (at ${alert.line})` : ""}. `
        + `${sourceText(alert) ? `Source: ${sourceText(alert)}. ` : ""}What should I say or do about it?`;
      try { window.sessionStorage.setItem(`quorum:ask:${s.id}`, question); } catch { /* private mode */ }
      await refreshSessions();
      router.push(`/c/${s.id}`);
    } catch (err) {
      toast.error("Couldn't start a chat", { description: (err as Error).message });
    }
  };
}

/** Check now + the alerts of one meeting. `onLine` jumps to a line of the meeting (transcript or live panel). */
export function AlertsView({ target, onLine, compact = false }: { target: CheckTarget; onLine?: (seconds: number) => void; compact?: boolean }) {
  const { prefs } = useApp();
  const state = useCheck(target);
  const [showDismissed, setShowDismissed] = useState(false);
  const ask = useAskAboutAlert(target);
  const targetId = target.transcript_id ?? target.recording_id;
  useEffect(() => { if (targetId) loadAlerts(target); }, [targetId]); // eslint-disable-line react-hooks/exhaustive-deps

  const check = async (confirm?: "local" | "cloud") => {
    if (prefs?.check_popups) await askNotificationPermission();
    const { fresh, summary } = await runCheck(target, confirm);
    if (fresh.length) {
      toast(summary || `${fresh.length} new alert(s)`);
      if (prefs?.check_popups && document.hidden) notify(summary, () => undefined);
    }
  };
  const act = async (alert: MeetingAlert, action: "dismiss" | "track" | "restore") => {
    try {
      await actOn(target, alert, action);
      if (action === "track") toast.success("Added to My actions");
    } catch (err) {
      toast.error("Couldn't update the alert", { description: (err as Error).message });
    }
  };
  const visible = state.alerts.filter((a) => a.state !== "dismissed")
    .sort((a, b) => ORDER.indexOf(a.type) - ORDER.indexOf(b.type));
  const dismissed = state.alerts.filter((a) => a.state === "dismissed");
  return (
    <div className="space-y-2.5" data-testid="meeting-check">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant={compact ? "outline" : "primary"} onClick={() => check()} loading={state.running} data-testid="check-now">
          <RefreshCw className="h-3.5 w-3.5" /> Check now
        </Button>
        <span className="text-xs text-muted" data-testid="check-status">
          {state.running ? state.status ?? "Checking…" : state.last ? `Checked ${new Date(state.last).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
            : "Compares with earlier meetings and memory"}
        </span>
      </div>
      {state.confirm && (
        <Callout tone="warning" title="Send this meeting to a cloud model?" data-testid="check-confirm">
          <p className="text-[13px]">{state.confirm.reasons.join(" ")} to {state.confirm.model ?? "the cloud check model"}.</p>
          <div className="mt-2 flex flex-wrap gap-2">
            <Button size="sm" variant="primary" disabled={!state.confirm.local_model} onClick={() => check("local")} data-testid="check-local">
              Use local model{state.confirm.local_model ? ` (${state.confirm.local_model})` : ""}
            </Button>
            <Button size="sm" variant="outline" onClick={() => check("cloud")} data-testid="check-cloud">Send to cloud</Button>
          </div>
        </Callout>
      )}
      {state.error && <p className="text-xs text-danger" data-testid="check-error">{state.error}</p>}
      {visible.length === 0 && !state.running && state.last && (
        <p className="text-[13px] text-muted" data-testid="check-none">No alerts: nothing conflicts, and nothing is left pending.</p>
      )}
      <ul className="space-y-2" data-testid="alerts">
        {visible.map((a) => (
          <li key={a.id} data-testid={`alert-${a.type}`} data-state={a.state}
              className={clsx("rounded-lg border p-2.5 text-[13px]", a.state === "tracked" ? "border-border bg-surface-2/60" : "border-border")}>
            <div className="mb-1 flex flex-wrap items-center gap-1.5">
              <Badge tone={KINDS[a.type].tone}>{KINDS[a.type].icon} {KINDS[a.type].label}</Badge>
              {a.line && (
                <button className="text-xs tabular-nums text-accent hover:underline" onClick={() => a.seconds != null && onLine?.(a.seconds)}
                        title={a.quote}>{a.line}</button>
              )}
              {a.state === "tracked" && <span className="text-xs text-success">Tracked</span>}
            </div>
            <p className="font-medium">{a.text}</p>
            {sourceText(a) && <p className="mt-1 text-xs text-muted" data-testid="alert-source">{sourceText(a)}</p>}
            <div className="mt-2 flex flex-wrap gap-1.5">
              <Button size="sm" variant="ghost" onClick={() => act(a, "dismiss")} data-testid="alert-dismiss"><X className="h-3.5 w-3.5" /> Dismiss</Button>
              {a.state !== "tracked" && (
                <Button size="sm" variant="ghost" onClick={() => act(a, "track")} data-testid="alert-track"><ListChecks className="h-3.5 w-3.5" /> Track</Button>
              )}
              <Button size="sm" variant="ghost" onClick={() => ask(a)} data-testid="alert-ask"><MessageSquare className="h-3.5 w-3.5" /> Ask in chat</Button>
            </div>
          </li>
        ))}
      </ul>
      {dismissed.length > 0 && (
        <div className="text-xs text-muted">
          <button className="flex items-center gap-1 hover:text-fg" onClick={() => setShowDismissed((v) => !v)}>
            <Eye className="h-3 w-3" /> {dismissed.length} dismissed
          </button>
          {showDismissed && (
            <ul className="mt-1 space-y-1">
              {dismissed.map((a) => (
                <li key={a.id} className="flex items-center gap-2">
                  <span className="line-through">{a.text}</span>
                  <button className="text-accent hover:underline" onClick={() => act(a, "restore")}>Restore</button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

/** While a call with a live transcript is recorded (on every page): keeps its alerts loaded, runs the automatic
 *  check every N minutes when that's switched on, and shows the pop-up (count only) for new alerts. */
export function MeetingCheckRunner() {
  const { recording, prefs, setLivePanelOpen } = useApp();
  const recId = recording?.state === "recording" && recording.live ? recording.id ?? null : null;
  const minutes = prefs?.check_auto_minutes ?? 0;
  const popups = prefs?.check_popups ?? false;

  useEffect(() => {
    if (!recId) return;
    const target = { recording_id: recId };
    loadAlerts(target);
    const refresh = window.setInterval(() => loadAlerts(target), 20_000);
    let auto: number | undefined;
    if (minutes > 0) {
      auto = window.setInterval(async () => {
        const { fresh, summary } = await runCheck(target);
        if (!fresh.length) return;
        if (popups) notify(summary, () => setLivePanelOpen(true));
        toast(summary, { action: { label: "Show", onClick: () => setLivePanelOpen(true) } });
      }, minutes * 60_000);
    }
    return () => { window.clearInterval(refresh); if (auto) window.clearInterval(auto); };
  }, [recId, minutes, popups, setLivePanelOpen]);
  return null;
}

/** The number of open (not dismissed, not tracked) alerts of the call being recorded, for the badges. */
export function useOpenAlerts(recordingId: string | null | undefined): number {
  const state = useCheck({ recording_id: recordingId ?? null });
  return recordingId ? state.alerts.filter((a) => a.state === "new").length : 0;
}

/** My actions (tracked from alerts) for the active project, with done / not done. */
export function MyActions() {
  const [items, setItems] = useState<MyAction[] | null>(null);
  const load = useCallback(() => api.get<MyAction[]>("/api/my-actions").then(setItems).catch(() => setItems([])), []);
  useEffect(() => {
    load();
    window.addEventListener("quorum:my-actions", load);
    return () => window.removeEventListener("quorum:my-actions", load);
  }, [load]);
  if (!items) return <div className="flex items-center gap-2 text-[13px] text-muted"><Spinner /> Loading…</div>;
  if (items.length === 0) return null;
  const toggle = async (item: MyAction) => {
    await api.patch(`/api/my-actions/${item.id}`, { done: !item.done }).catch(() => undefined);
    load();
  };
  const remove = async (item: MyAction) => {
    await api.del(`/api/my-actions/${item.id}`).catch(() => undefined);
    load();
  };
  return (
    <section aria-label="My actions" data-testid="my-actions">
      <h2 className="mb-3 flex items-center gap-2 text-[15px] font-semibold"><ListChecks className="h-4 w-4" /> My actions
        <span className="text-xs font-normal text-muted">{items.filter((i) => !i.done).length} open</span></h2>
      <ul className="divide-y divide-border overflow-hidden rounded-2xl border border-border bg-surface">
        {items.map((item) => (
          <li key={item.id} className="group flex items-start gap-3 px-4 py-2.5 text-[13px]" data-testid="my-action" data-done={item.done}>
            <button role="checkbox" aria-checked={item.done} aria-label={item.done ? "Mark as not done" : "Mark as done"} onClick={() => toggle(item)}
                    className={clsx("mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border",
                      item.done ? "border-success bg-success text-white" : "border-border-strong")}>
              {item.done && <Check className="h-3 w-3" />}
            </button>
            <span className="min-w-0 flex-1">
              <span className={clsx(item.done && "text-muted line-through")}>{item.text}</span>
              <span className="block text-xs text-muted">{KINDS[item.type]?.label ?? item.type}{item.sources[0]?.meeting ? ` · from ${item.sources[0].meeting}` : ""} · {item.created.slice(0, 10)}</span>
            </span>
            <Button size="icon-sm" variant="ghost" aria-label="Remove" className="opacity-60 group-hover:opacity-100" onClick={() => remove(item)}>
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </li>
        ))}
      </ul>
    </section>
  );
}
