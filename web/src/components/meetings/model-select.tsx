"use client";

/**
 * The model choosers used for meetings (Settings, New meeting, Speakers and Summary panels): the model
 * that reads the transcript, the one that reads video frames, and the summary model. Missing local models
 * can be downloaded right here.
 */
import { Download } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { Button, Callout, Progress } from "@/components/ui";
import { api, type LocalModelList } from "@/lib/api";
import { clsx } from "@/lib/format";

/** Which model a meeting step uses:
 *  speaker  reads the transcript (local only)
 *  frames   reads video frames for on-screen names (local models that can read images)
 *  summary  writes the meeting notes (any model; "" = ask which model, never the chat model) */
export type ModelKind = "speaker" | "frames" | "summary";

const ENDPOINT: Record<Exclude<ModelKind, "summary">, string> = { speaker: "/api/models/speaker", frames: "/api/models/vision" };

/**
 * Loads the list of local models for a chooser ('speaker' or 'frames'), and keeps refreshing it while a
 * download is running so its progress shows.
 */
export function useLocalModels(kind: ModelKind) {
  const [data, setData] = useState<LocalModelList | null>(null);
  const load = useCallback(() => {
    if (kind === "summary") return Promise.resolve();
    return api.get<LocalModelList>(ENDPOINT[kind]).then(setData).catch(() => setData(null));
  }, [kind]);
  useEffect(() => { load(); }, [load]);
  const downloading = Object.values(data?.pulls ?? {}).some((p) => p.state === "running");
  useEffect(() => {
    if (!downloading) return;
    const timer = setInterval(load, 1500);
    return () => clearInterval(timer);
  }, [downloading, load]);
  return { data, reload: load };
}

const selectClass = "h-9 w-56 max-w-full rounded-lg border border-border-strong bg-surface px-2.5 text-sm";

/**
 * A labelled <select> of models. '' means 'use the default' (`defaultLabel`). For local models it marks
 * which can read video and offers a Download button (with progress) when the chosen model is missing.
 */
export function ModelSelect({ kind, value, onChange, id, label, defaultLabel, disabled, hint }: {
  kind: ModelKind;
  value: string;
  onChange: (value: string) => void;
  id: string;
  label: string;
  /** Text for the "" option, e.g. "Settings default" or "Recommended". */
  defaultLabel: string;
  disabled?: boolean;
  hint?: React.ReactNode;
}) {
  const { models: chat } = useApp();
  const { data, reload } = useLocalModels(kind);

  if (kind === "summary") {
    const ready = (chat?.models ?? []).filter((m) => m.status === "ready");
    return (
      <div className="space-y-1" data-testid={`model-${kind}`}>
        <label htmlFor={id} className="block text-[13px] font-medium">{label}</label>
        <select id={id} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} className={selectClass}>
          <option value="">{defaultLabel}</option>
          {ready.map((m) => <option key={m.id} value={m.id}>{m.label}{m.group === "cloud" ? " · cloud" : " · on this Mac"}</option>)}
          {value && !ready.some((m) => m.id === value) && <option value={value}>{value} (not available)</option>}
        </select>
        {hint && <p className="text-xs text-muted">{hint}</p>}
      </div>
    );
  }

  const list = data?.models ?? [];
  const current = value || data?.effective || "";
  const chosen = list.find((m) => m.model === current);
  // what runs while the chosen model is missing: the recommended one if it's installed
  const fallback = value && chosen && !chosen.installed
    ? list.find((m) => m.recommended && m.installed)?.model ?? list.find((m) => m.installed)?.model ?? "" : "";
  const pull = chosen ? data?.pulls[chosen.model] : undefined;
  const download = async (model: string) => {
    try {
      await api.post("/api/local-models/pull", { model });
      reload();
    } catch (err) {
      toast.error("Couldn't start the download", { description: (err as Error).message });
    }
  };
  const effective = data?.effective ? ` (${data.effective})` : "";

  return (
    <div className="space-y-1" data-testid={`model-${kind}`}>
      <label htmlFor={id} className="block text-[13px] font-medium">{label}</label>
      <select id={id} value={value} disabled={disabled || !data} onChange={(e) => onChange(e.target.value)} className={selectClass}>
        <option value="">{defaultLabel}{effective}</option>
        {list.map((m) => (
          <option key={m.model} value={m.model}>
            {m.label}{m.recommended ? " (recommended)" : ""}{kind === "speaker" && m.vision ? " · reads video" : ""}
            {m.installed ? "" : " · not downloaded"}
          </option>
        ))}
        {value && data && !list.some((m) => m.model === value) && <option value={value}>{value} (not installed)</option>}
      </select>
      {hint && <p className="text-xs text-muted">{hint}</p>}
      {chosen && !chosen.installed && (
        <Callout tone="warning" className="mt-1" data-testid={`model-${kind}-download`}>
          {chosen.label} isn&apos;t downloaded{chosen.size_gb ? ` (${chosen.size_gb} GB)` : ""}.
          {fallback ? ` Until it is, ${fallback} is used instead.` : ""}
          <div className="mt-2">
            {pull?.state === "running"
              ? <Progress value={pull.percent} label={`Downloading ${chosen.label}: ${pull.status}`} />
              : <Button size="sm" onClick={() => download(chosen.model)} disabled={!data?.ollama_running}>
                  <Download className="h-3.5 w-3.5" /> Download {chosen.label}</Button>}
          </div>
        </Callout>
      )}
      {value && !chosen && data && (
        <p className={clsx("text-xs", data.effective ? "text-muted" : "text-danger")}>
          {value} isn&apos;t installed{list.some((m) => m.installed) ? `; ${list.find((m) => m.recommended && m.installed)?.model ?? list.find((m) => m.installed)?.model} is used instead` : ""}.
        </p>
      )}
    </div>
  );
}

/** Under the speaker-model chooser: a text-only model can't read video frames. */
export function FramesNote({ speakerModel }: { speakerModel: string }) {
  const speaker = useLocalModels("speaker").data;
  const name = speakerModel || speaker?.effective || "";
  const entry = speaker?.models.find((m) => m.model === name);
  if (!entry || entry.vision || !speaker) return null;
  return (
    <p className="text-xs text-muted" data-testid="frames-note">
      {entry.label} reads text only, so video frames go to {speaker.frames || "the vision model"} (chosen below).
    </p>
  );
}
