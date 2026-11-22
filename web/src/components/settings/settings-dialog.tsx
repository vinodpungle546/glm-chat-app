"use client";

/**
 * The Settings dialog with its tabs (General, Projects, Memory, Meetings, Skills, Connectors, Search,
 * Models, Import…).
 */
import * as DialogPrimitive from "@radix-ui/react-dialog";
import * as Tabs from "@radix-ui/react-tabs";
import { AudioLines, Brain, Cloud, Cpu, Download, FolderKanban, Plug, Puzzle, Search, SlidersHorizontal, X } from "lucide-react";
import { useEffect, useState } from "react";
import { type SettingsTab, useApp } from "@/components/app-provider";
import { CloudModelsTab } from "@/components/settings/cloud-models-tab";
import { ConnectorsTab } from "@/components/settings/connectors-tab";
import { GeneralTab } from "@/components/settings/general-tab";
import { ImportTab } from "@/components/settings/import-tab";
import { LocalModelsTab } from "@/components/settings/local-models-tab";
import { MemoryTab } from "@/components/settings/memory-tab";
import { ProjectsTab } from "@/components/settings/projects-tab";
import { SearchTab } from "@/components/settings/search-tab";
import { SkillsTab } from "@/components/settings/skills-tab";
import { SpeakersTab } from "@/components/settings/speakers-tab";
import { Button } from "@/components/ui";
import { api, type Project } from "@/lib/api";

const TABS: { id: SettingsTab; label: string; icon: React.ReactNode }[] = [
  { id: "general", label: "General", icon: <SlidersHorizontal className="h-4 w-4" /> },
  { id: "projects", label: "Projects", icon: <FolderKanban className="h-4 w-4" /> },
  { id: "memory", label: "Memory", icon: <Brain className="h-4 w-4" /> },
  { id: "speakers", label: "Meetings", icon: <AudioLines className="h-4 w-4" /> },
  { id: "skills", label: "Skills", icon: <Puzzle className="h-4 w-4" /> },
  { id: "connectors", label: "Connectors", icon: <Plug className="h-4 w-4" /> },
  { id: "search", label: "Search", icon: <Search className="h-4 w-4" /> },
  { id: "import", label: "Import from Claude", icon: <Download className="h-4 w-4" /> },
  { id: "providers", label: "Models", icon: <Cloud className="h-4 w-4" /> },
  { id: "models", label: "Local models", icon: <Cpu className="h-4 w-4" /> },
];

/** The active project, shared by the Projects, Memory and Skills tabs. */
export function useActiveProject() {
  const [project, setProject] = useState<Project | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = async () => {
    try {
      const list = await api.get<Project[]>("/api/projects");
      const active = list.find((p) => p.active) ?? list[0];
      setProject(active ? await api.get<Project>(`/api/projects/${active.id}`) : null);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  };
  useEffect(() => { reload(); }, []);
  const save = async (fields: Partial<Project>) => {
    if (!project) return;
    const updated = await api.patch<Project>(`/api/projects/${project.id}`, fields);
    setProject(updated);
    return updated;
  };
  return { project, error, reload, save };
}

/**
 * The dialog; which tab is open comes from the app state, so any part of the app can open a specific tab.
 */
export function SettingsDialog() {
  const { settingsTab, openSettings, closeSettings } = useApp();
  const open = settingsTab !== null;
  return (
    <DialogPrimitive.Root open={open} onOpenChange={(o) => (o ? openSettings() : closeSettings())}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/40 backdrop-blur-[2px]" />
        <DialogPrimitive.Content
          data-testid="settings"
          className="fixed left-1/2 top-1/2 z-50 flex h-[min(720px,calc(100vh-2rem))] w-[calc(100vw-2rem)] max-w-4xl -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-2xl border border-border bg-surface shadow-card focus:outline-none"
        >
          <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
            <DialogPrimitive.Title className="text-base font-semibold">Settings</DialogPrimitive.Title>
            <DialogPrimitive.Description className="sr-only">Projects, memory, speakers, skills, connectors, search, import from Claude, cloud models and local models</DialogPrimitive.Description>
            <DialogPrimitive.Close asChild>
              <Button variant="ghost" size="icon-sm" aria-label="Close settings"><X className="h-4 w-4" /></Button>
            </DialogPrimitive.Close>
          </div>
          <Tabs.Root value={settingsTab ?? "general"} onValueChange={(v) => openSettings(v as SettingsTab)}
                     orientation="vertical" className="flex min-h-0 flex-1 flex-col md:flex-row">
            <Tabs.List aria-label="Settings sections"
              className="flex shrink-0 gap-1 overflow-x-auto border-b border-border p-2 md:w-52 md:flex-col md:border-b-0 md:border-r">
              {TABS.map((t) => (
                <Tabs.Trigger key={t.id} value={t.id} data-testid={`settings-tab-${t.id}`}
                  className="flex shrink-0 items-center gap-2 rounded-lg px-3 py-2 text-[13px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg data-[state=active]:bg-hover data-[state=active]:text-fg">
                  {t.icon} {t.label}
                </Tabs.Trigger>
              ))}
            </Tabs.List>
            <div className="min-h-0 flex-1 overflow-y-auto">
              <Tabs.Content value="general" className="p-5 focus:outline-none"><GeneralTab /></Tabs.Content>
              <Tabs.Content value="projects" className="p-5 focus:outline-none"><ProjectsTab /></Tabs.Content>
              <Tabs.Content value="memory" className="p-5 focus:outline-none"><MemoryTab /></Tabs.Content>
              <Tabs.Content value="speakers" className="p-5 focus:outline-none"><SpeakersTab /></Tabs.Content>
              <Tabs.Content value="skills" className="p-5 focus:outline-none"><SkillsTab /></Tabs.Content>
              <Tabs.Content value="connectors" className="p-5 focus:outline-none"><ConnectorsTab /></Tabs.Content>
              <Tabs.Content value="search" className="p-5 focus:outline-none"><SearchTab /></Tabs.Content>
              <Tabs.Content value="import" className="p-5 focus:outline-none"><ImportTab /></Tabs.Content>
              <Tabs.Content value="providers" className="p-5 focus:outline-none"><CloudModelsTab /></Tabs.Content>
              <Tabs.Content value="models" className="p-5 focus:outline-none"><LocalModelsTab /></Tabs.Content>
            </div>
          </Tabs.Root>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/** A heading for a section of a settings tab, with an optional description and button. */
export function SectionTitle({ title, description, action }: { title: string; description?: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="mb-4 flex items-start justify-between gap-4">
      <div>
        <h2 className="text-[15px] font-semibold">{title}</h2>
        {description && <p className="mt-0.5 text-[13px] text-muted">{description}</p>}
      </div>
      {action}
    </div>
  );
}
