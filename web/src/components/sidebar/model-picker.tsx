"use client";

/**
 * The model picker at the top of the sidebar: local models (on this Mac) and cloud models, with their
 * status.
 */
import * as Menu from "@radix-ui/react-dropdown-menu";
import { Check, ChevronDown, Cloud, Cpu, Download, RefreshCw } from "lucide-react";
import { useApp } from "@/components/app-provider";
import { Badge, Button } from "@/components/ui";
import type { ModelEntry, ModelStatus } from "@/lib/api";
import { clsx } from "@/lib/format";

export const STATUS_TEXT: Record<ModelStatus, string> = {
  ready: "Ready",
  not_downloaded: "Not downloaded",
  ollama_down: "Ollama not running",
  not_configured: "Not configured",
};

/** A dot and text for a model's status: Ready, Not downloaded, Ollama not running… */
export function StatusBadge({ model }: { model: ModelEntry }) {
  if (model.pull?.state === "running") {
    return <Badge tone="accent" className="tabular-nums">Downloading {Math.round(model.pull.percent)}%</Badge>;
  }
  const tone = model.status === "ready" ? "success" : model.status === "ollama_down" ? "danger" : "warning";
  return (
    <Badge tone={tone} className="shrink-0" title={STATUS_TEXT[model.status]}>
      <span className={clsx("h-1.5 w-1.5 rounded-full", tone === "success" ? "bg-success" : tone === "danger" ? "bg-danger" : "bg-warning")} />
      <span data-testid={`status-${model.id}`}>{STATUS_TEXT[model.status]}</span>
    </Badge>
  );
}

/** One model in the menu: name, where it runs, description and status. */
function Item({ model, selected, onSelect }: { model: ModelEntry; selected: boolean; onSelect: () => void }) {
  return (
    <Menu.Item
      onSelect={onSelect}
      data-testid={`model-option-${model.id}`}
      className="flex cursor-pointer items-start gap-2.5 rounded-lg px-2.5 py-2 outline-none data-[highlighted]:bg-hover"
    >
      <div className="mt-0.5 w-4 shrink-0">{selected && <Check className="h-4 w-4 text-accent" />}</div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-sm font-medium">
            {model.label}{model.group === "local" && model.label !== model.model ? " (Local)" : ""}
          </span>
          {model.group === "local" ? <StatusBadge model={model} /> : model.status !== "ready" && <StatusBadge model={model} />}
        </div>
        {model.description && <p className="mt-0.5 line-clamp-2 text-xs text-muted">{model.description}</p>}
      </div>
    </Menu.Item>
  );
}

/**
 * The dropdown. Choosing a model saves it as the selected chat model; a new chat starts on the default
 * (cloud) model again.
 */
export function ModelPicker() {
  const { models, modelsError, selectedModel, selectModel, refreshModels, openSettings } = useApp();
  const local = models?.models.filter((m) => m.group === "local") ?? [];
  const cloud = models?.models.filter((m) => m.group === "cloud") ?? [];
  const label = selectedModel
    ? `${selectedModel.label}${selectedModel.group === "local" ? " (Local)" : ""}`
    : modelsError ? "Models unavailable" : "Loading models…";

  return (
    <Menu.Root onOpenChange={(open) => open && refreshModels()}>
      <Menu.Trigger asChild>
        <button
          data-testid="model-picker"
          aria-label={`Model: ${label}`}
          className="flex w-full items-center gap-2.5 rounded-xl border border-border bg-surface px-3 py-2 text-left shadow-sm transition-colors hover:bg-hover"
        >
          {selectedModel?.group === "cloud" ? <Cloud className="h-4 w-4 shrink-0 text-muted" /> : <Cpu className="h-4 w-4 shrink-0 text-muted" />}
          <div className="min-w-0 flex-1">
            <div className="truncate text-[13px] font-semibold" data-testid="selected-model">{label}</div>
            <div className="truncate text-[11px] text-muted">
              {selectedModel ? (selectedModel.group === "local" ? `On this Mac · ${STATUS_TEXT[selectedModel.status]}`
                : `Cloud · ${selectedModel.provider === "gaia" ? "Globant GAIA" : (selectedModel.description ?? "").split(".")[0]}`) : " "}
            </div>
          </div>
          <ChevronDown className="h-4 w-4 shrink-0 text-muted" />
        </button>
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Content align="start" sideOffset={6}
          className="z-50 max-h-[70vh] w-[320px] overflow-y-auto rounded-xl border border-border bg-surface p-1.5 shadow-card">
          {modelsError && <p className="px-2.5 py-2 text-xs text-danger">{modelsError}</p>}
          <Menu.Label className="flex items-center justify-between px-2.5 pb-1 pt-1.5 text-[11px] font-semibold uppercase tracking-wide text-faint">
            <span className="flex items-center gap-1.5"><Cpu className="h-3 w-3" /> Local</span>
            {models && !models.ollama_running && <span className="normal-case tracking-normal text-danger">Ollama not running</span>}
          </Menu.Label>
          {local.map((m) => <Item key={m.id} model={m} selected={m.id === selectedModel?.id} onSelect={() => selectModel(m.id)} />)}
          <Menu.Separator className="my-1.5 h-px bg-border" />
          <Menu.Label className="flex items-center gap-1.5 px-2.5 pb-1 pt-1.5 text-[11px] font-semibold uppercase tracking-wide text-faint">
            <Cloud className="h-3 w-3" /> Cloud
          </Menu.Label>
          {cloud.map((m) => <Item key={m.id} model={m} selected={m.id === selectedModel?.id} onSelect={() => selectModel(m.id)} />)}
          <Menu.Separator className="my-1.5 h-px bg-border" />
          <div className="flex gap-1 px-1 pb-0.5">
            <Menu.Item asChild>
              <Button variant="ghost" size="sm" className="flex-1 justify-start" onClick={() => openSettings("models")}>
                <Download className="h-3.5 w-3.5" /> Local models
              </Button>
            </Menu.Item>
            <Menu.Item asChild>
              <Button variant="ghost" size="sm" className="flex-1 justify-start" onClick={() => openSettings("providers")} data-testid="manage-cloud-models">
                <Cloud className="h-3.5 w-3.5" /> Cloud models
              </Button>
            </Menu.Item>
            <Button variant="ghost" size="icon-sm" aria-label="Refresh models" onClick={(e) => { e.preventDefault(); refreshModels(); }}>
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
          </div>
        </Menu.Content>
      </Menu.Portal>
    </Menu.Root>
  );
}
