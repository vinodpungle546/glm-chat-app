"use client";

/** Settings → Local models: the Ollama models, downloads with progress, and deleting models. */
import { Download, HardDrive, RefreshCw, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { SectionTitle } from "@/components/settings/settings-dialog";
import { StatusBadge } from "@/components/sidebar/model-picker";
import { Badge, Button, Callout, ConfirmDialog, Progress } from "@/components/ui";
import { api, type ModelEntry, type PullState } from "@/lib/api";
import { fmtBytes } from "@/lib/format";

/** One local model with its status and Download / Cancel / Delete. */
function ModelRow({ model, pull, onPull, onCancel, onDelete }: {
  model: ModelEntry; pull?: PullState; onPull: () => void; onCancel: () => void; onDelete: () => void;
}) {
  const [confirm, setConfirm] = useState(false);
  const running = pull?.state === "running";
  return (
    <div className="rounded-xl border border-border p-4" data-testid={`local-model-${model.model}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold">{model.label}</span>
            <span className="font-mono text-xs text-muted">{model.model}</span>
            <StatusBadge model={{ ...model, pull }} />
          </div>
          {model.description && <p className="mt-1 text-[13px] text-muted">{model.description}</p>}
          <p className="mt-1 flex items-center gap-1 text-xs text-faint">
            <HardDrive className="h-3 w-3" />
            {model.installed_bytes ? `${fmtBytes(model.installed_bytes)} on disk` : model.size_gb ? `About ${model.size_gb} GB download` : "Size unknown"}
          </p>
          {model.ollama_too_old && (
            <p className="mt-1 text-xs text-warning">Needs Ollama {model.min_ollama} or newer — update Ollama first (brew upgrade ollama).</p>
          )}
        </div>
        <div className="flex gap-2">
          {model.status === "not_downloaded" && !running && (
            <Button variant="primary" size="sm" onClick={onPull} data-testid={`download-${model.model}`} disabled={model.ollama_too_old}>
              <Download className="h-3.5 w-3.5" /> Download model
            </Button>
          )}
          {running && (
            <Button variant="secondary" size="sm" onClick={onCancel} data-testid={`cancel-${model.model}`}>
              <X className="h-3.5 w-3.5" /> Cancel
            </Button>
          )}
          {model.status === "ready" && (
            <Button variant="ghost" size="sm" onClick={() => setConfirm(true)} aria-label={`Delete ${model.model}`}>
              <Trash2 className="h-3.5 w-3.5" /> Remove
            </Button>
          )}
        </div>
      </div>
      {model.status === "not_downloaded" && !running && model.size_gb && (
        <p className="mt-3 text-xs text-muted">This is a large download (about {model.size_gb} GB). It runs in the background — you can keep working; a partial download resumes if cancelled.</p>
      )}
      {pull && (pull.state === "running" || pull.state === "error" || pull.state === "cancelled") && (
        <div className="mt-3 space-y-1.5" data-testid={`pull-${model.model}`}>
          {pull.state === "running" && (
            <>
              <Progress value={pull.percent} label={`Downloading ${model.label}`} />
              <div className="flex justify-between text-xs text-muted tabular-nums">
                <span>{pull.status || "Starting…"}</span>
                <span>{pull.total ? `${fmtBytes(pull.completed)} / ${fmtBytes(pull.total)} · ${pull.percent.toFixed(0)}%` : ""}</span>
              </div>
            </>
          )}
          {pull.state === "cancelled" && <p className="text-xs text-muted">Download cancelled. Downloading again resumes where it stopped.</p>}
          {pull.state === "error" && <Callout tone="danger">{pull.error}</Callout>}
        </div>
      )}
      <ConfirmDialog open={confirm} onOpenChange={setConfirm} title={`Remove ${model.label}?`} confirmLabel="Remove"
        description={`Deletes ${model.model} from Ollama and frees ${model.installed_bytes ? fmtBytes(model.installed_bytes) : "its disk space"}. You can download it again later.`}
        onConfirm={onDelete} />
    </div>
  );
}

/** The Local models tab. */
export function LocalModelsTab() {
  const { models, refreshModels, health } = useApp();
  const [pulls, setPulls] = useState<Record<string, PullState>>({});
  const timer = useRef<number | null>(null);

  const poll = useCallback(async () => {
    try {
      const data = await api.get<Record<string, PullState>>("/api/local-models/pulls");
      setPulls(data);
      const finished = Object.values(data).filter((p) => p.state === "done");
      if (finished.length) refreshModels();
      return Object.values(data).some((p) => p.state === "running");
    } catch {
      return false;
    }
  }, [refreshModels]);

  const startPolling = useCallback(() => {
    if (timer.current) return;
    timer.current = window.setInterval(async () => {
      const busy = await poll();
      if (!busy && timer.current) {
        window.clearInterval(timer.current);
        timer.current = null;
      }
    }, 700);
  }, [poll]);

  useEffect(() => {
    poll().then((busy) => busy && startPolling());
    return () => { if (timer.current) window.clearInterval(timer.current); };
  }, [poll, startPolling]);

  const prevStates = useRef<Record<string, string>>({});
  useEffect(() => {
    for (const [name, p] of Object.entries(pulls)) {
      if (prevStates.current[name] === "running" && p.state === "done") toast.success(`${name} downloaded`, { description: "It's ready to use." });
      if (prevStates.current[name] === "running" && p.state === "error") toast.error(`Download of ${name} failed`, { description: p.error });
      prevStates.current[name] = p.state;
    }
  }, [pulls]);

  const pull = async (model: string) => {
    try {
      const state = await api.post<PullState>("/api/local-models/pull", { model });
      setPulls((p) => ({ ...p, [model]: state }));
      startPolling();
    } catch (err) {
      toast.error("Couldn't start the download", { description: (err as Error).message });
    }
  };
  const cancel = async (model: string) => {
    try {
      const state = await api.del<PullState>(`/api/local-models/pull/${encodeURIComponent(model)}`);
      setPulls((p) => ({ ...p, [model]: state }));
    } catch (err) {
      toast.error("Couldn't cancel", { description: (err as Error).message });
    }
  };
  const remove = async (model: string) => {
    try {
      await api.del(`/api/local-models/${encodeURIComponent(model)}`);
      toast.success(`${model} removed`);
      refreshModels();
    } catch (err) {
      toast.error("Couldn't remove the model", { description: (err as Error).message });
    }
  };

  const local = models?.models.filter((m) => m.group === "local") ?? [];
  return (
    <div>
      <SectionTitle title="Local models"
        description="Chat models that run in Ollama on this Mac — nothing leaves the laptop. Speech-to-text (Whisper) and speaker labels (pyannote) are separate and fixed."
        action={<Button size="sm" variant="ghost" onClick={() => refreshModels()}><RefreshCw className="h-3.5 w-3.5" /> Refresh</Button>} />
      {models && !models.ollama_running ? (
        <Callout tone="danger" title="Ollama isn’t running" className="mb-4" data-testid="ollama-down-settings"
          action={<Button size="sm" onClick={() => refreshModels()}>Check again</Button>}>
          <ol className="mt-1 list-decimal space-y-0.5 pl-4">
            <li>Install it once: <code className="font-mono">brew install ollama</code></li>
            <li>Start it: <code className="font-mono">ollama serve</code> or <code className="font-mono">scripts/start_local_ai.sh</code></li>
            <li>Then press “Check again”. Until then, pick a cloud model in the sidebar.</li>
          </ol>
        </Callout>
      ) : models && (
        <div className="mb-4 flex flex-wrap items-center gap-2 text-xs text-muted">
          <Badge tone="success">Ollama {models.ollama_version}</Badge>
          <span>{health?.ollama.url}</span>
        </div>
      )}
      <div className="space-y-3">
        {local.map((m) => (
          <ModelRow key={m.id} model={m} pull={pulls[m.model]} onPull={() => pull(m.model)}
                    onCancel={() => cancel(m.model)} onDelete={() => remove(m.model)} />
        ))}
      </div>
      <p className="mt-4 text-xs text-muted">
        Models you install with <code className="font-mono">ollama pull</code> appear here automatically. Add recommended ones to <code className="font-mono">models.json</code>.
      </p>
    </div>
  );
}
