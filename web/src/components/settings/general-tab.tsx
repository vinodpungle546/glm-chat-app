"use client";

/**
 * Settings → General: appearance, privacy (ask before sending confidential content to the cloud), the
 * meetings watch folder, and keyboard shortcuts.
 */
import { FolderOpen } from "lucide-react";
import { useTheme } from "next-themes";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { SectionTitle } from "@/components/settings/settings-dialog";
import { Button, Callout, Input, Kbd, Label, Switch } from "@/components/ui";
import { api } from "@/lib/api";
import { clsx, modKey } from "@/lib/format";

/**
 * Shows and changes the folder the transcriber watches for recordings (can create it, or go back to the
 * default).
 */
function WatchFolder() {
  const [folder, setFolder] = useState("");
  const [current, setCurrent] = useState<{ meetings_dir: string; default_meetings_dir: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [create, setCreate] = useState(false);
  useEffect(() => {
    api.get<{ meetings_dir: string; default_meetings_dir: string }>("/api/meetings/settings")
      .then((s) => { setCurrent(s); setFolder(s.meetings_dir); })
      .catch((err) => setError((err as Error).message));
  }, []);
  const save = async (value: string) => {
    try {
      const s = await api.post<{ meetings_dir: string; default_meetings_dir: string }>("/api/meetings/settings", { folder: value, create });
      setCurrent(s);
      setFolder(s.meetings_dir);
      setError(null);
      toast.success("Watch folder updated", { description: s.meetings_dir });
    } catch (err) {
      setError((err as Error).message);
    }
  };
  return (
    <div className="space-y-2">
      <Label htmlFor="watch-folder" hint="Recordings saved here appear under Meetings → Watch folder">Meeting watch folder</Label>
      <div className="flex gap-2">
        <Input id="watch-folder" value={folder} onChange={(e) => setFolder(e.target.value)} placeholder="/Users/you/Meetings" />
        <Button onClick={() => save(folder)}><FolderOpen className="h-4 w-4" /> Use</Button>
        {current && current.meetings_dir !== current.default_meetings_dir && (
          <Button variant="ghost" onClick={() => save("")}>Reset</Button>
        )}
      </div>
      <label className="flex items-center gap-2 text-[13px] text-muted">
        <input type="checkbox" checked={create} onChange={(e) => setCreate(e.target.checked)} /> Create the folder if it doesn’t exist
      </label>
      {error && <Callout tone="danger">{error}</Callout>}
    </div>
  );
}

/** The General tab. */
export function GeneralTab() {
  const { prefs, updatePrefs, health } = useApp();
  const { theme, setTheme } = useTheme();
  const mod = modKey();
  const { openSettings } = useApp();
  const toggle = (key: "confirm_cloud_sensitive" | "web_search") => async (value: boolean) => {
    try {
      if (key === "web_search") {
        await api.put("/api/search/settings", { web_enabled: value });
        await updatePrefs({ web_search: value });
      } else {
        await updatePrefs({ [key]: value });
      }
    } catch (err) {
      toast.error("Couldn't save", { description: (err as Error).message });
    }
  };
  return (
    <div className="space-y-8">
      <section>
        <SectionTitle title="Appearance" />
        <div className="inline-flex rounded-xl bg-surface-2 p-1" role="radiogroup" aria-label="Theme">
          {(["system", "light", "dark"] as const).map((t) => (
            <button key={t} role="radio" aria-checked={theme === t} data-testid={`theme-${t}`} onClick={() => setTheme(t)}
              className={clsx("rounded-lg px-3.5 py-1.5 text-[13px] font-medium capitalize",
                theme === t ? "bg-surface text-fg shadow-sm" : "text-muted hover:text-fg")}>
              {t}
            </button>
          ))}
        </div>
      </section>
      <section>
        <SectionTitle title="Privacy" description="How Quorum handles confidential content." />
        {prefs && (
          <div className="divide-y divide-border rounded-xl border border-border px-4">
            <Switch id="confirm-cloud" checked={prefs.confirm_cloud_sensitive} onChange={toggle("confirm_cloud_sensitive")}
              label="Ask before sending confidential content to the cloud"
              description="With a cloud model selected, messages that look confidential (keywords, personal data, meeting transcripts) wait for you to choose a local model or confirm." />
            <Switch id="web-search" checked={prefs.search.web_enabled} onChange={toggle("web_search")}
              label="Web search (cloud models only)"
              description={<>Lets search and the cloud model include web results. Never used with local models. Choose the provider and key in <button className="text-accent hover:underline" onClick={() => openSettings("search")}>Settings → Search</button>.</>} />
          </div>
        )}
      </section>
      <section>
        <SectionTitle title="Transcription" description={health?.transcriber.ok
          ? `Runs offline on this Mac · ${health.transcriber.whisper_model?.split("/").pop()} · speaker labels ${health.transcriber.diarization ? "on" : "off"}`
          : "The local transcriber isn’t running. Start it with scripts/start_local_ai.sh."} />
        <WatchFolder />
      </section>
      <section>
        <SectionTitle title="Keyboard shortcuts" />
        <p className="text-[13px] text-muted">Press <Kbd>{mod}</Kbd> <Kbd>/</Kbd> anywhere to see all shortcuts.</p>
      </section>
    </div>
  );
}
