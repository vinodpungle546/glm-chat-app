"use client";

/**
 * Explains why the selected model can't be used right now (not downloaded, Ollama not running, Ollama too
 * old) and how to fix it.
 */
import { useApp } from "@/components/app-provider";
import { Button, Callout } from "@/components/ui";
import type { ModelEntry } from "@/lib/api";

/** Why the selected model can't be used right now, with fix-it steps (null when it's fine). */
export function ModelAvailability({ model, compact }: { model: ModelEntry | null; compact?: boolean }) {
  const { openSettings, refreshModels } = useApp();
  if (!model || model.status === "ready") return null;
  if (model.status === "ollama_down") {
    return (
      <Callout tone="danger" title="Ollama isn't running" data-testid="ollama-down"
        action={<>
          <Button size="sm" variant="secondary" onClick={() => refreshModels()}>Check again</Button>
          <Button size="sm" variant="ghost" onClick={() => openSettings("models")}>Local model settings</Button>
        </>}>
        {model.label} runs in Ollama on this Mac. Start it with <code className="font-mono">ollama serve</code> (or{" "}
        <code className="font-mono">scripts/start_local_ai.sh</code>), then check again — or pick a cloud model in the sidebar.
        {!compact && " Nothing is sent to the cloud while a local model is selected."}
      </Callout>
    );
  }
  if (model.status === "not_downloaded") {
    return (
      <Callout tone="warning" title={`${model.label} isn't downloaded`}
        action={<Button size="sm" variant="primary" onClick={() => openSettings("models")}>Download model</Button>}>
        Download it in Settings → Local models{model.size_gb ? ` (about ${model.size_gb} GB)` : ""}, or pick another model in the sidebar.
      </Callout>
    );
  }
  return (
    <Callout tone="warning" title="Cloud model not configured">
      Set GAIA_API_KEY in .env and restart the API, or pick a local model.
    </Callout>
  );
}
