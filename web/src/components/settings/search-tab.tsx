"use client";

/**
 * Settings → Search: the web search provider and its API key, which of your data is searched, and the
 * search index (search by meaning).
 */
import { Check, Database, ExternalLink, KeyRound, RefreshCw, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { SectionTitle } from "@/components/settings/settings-dialog";
import { Badge, Button, Callout, Input, Progress, Switch } from "@/components/ui";
import { api, type SearchPrefs, type SearchSettings } from "@/lib/api";
import { clsx } from "@/lib/format";

const SOURCE_LABELS: { key: keyof SearchPrefs["sources"]; label: string; description: string }[] = [
  { key: "chats", label: "Chats", description: "Your chats here and chats imported from claude.ai" },
  { key: "transcripts", label: "Transcripts & meeting notes", description: "Every transcript line and generated note" },
  { key: "memory", label: "Memory", description: "Facts and people, including imported claude.ai memory" },
  { key: "connectors", label: "Connectors", description: "Sends the query to your enabled MCP connectors" },
];

/** The Search tab. */
export function SearchTab() {
  const { refreshHealth, updatePrefs } = useApp();
  const [data, setData] = useState<SearchSettings | null>(null);
  const [key, setKey] = useState("");
  const [testing, setTesting] = useState(false);
  const load = useCallback(() => api.get<SearchSettings>("/api/search/settings").then(setData).catch(() => setData(null)), []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (data?.index.state !== "running" && !data?.embedding.pull) return;
    const t = window.setInterval(load, 1500);
    return () => window.clearInterval(t);
  }, [data, load]);

  const save = async (values: Partial<SearchPrefs>) => {
    try {
      setData(await api.put<SearchSettings>("/api/search/settings", values));
      if ("web_enabled" in values) await updatePrefs({});
    } catch (err) {
      toast.error("Couldn't save", { description: (err as Error).message });
    }
  };
  if (!data) return <div className="skeleton h-40" />;
  const s = data.settings;
  const provider = data.providers.find((p) => p.id === s.provider)!;

  const saveKey = async () => {
    try {
      setData(await api.put<SearchSettings>(`/api/search/keys/${provider.id}`, { key }));
      setKey("");
      toast.success(`${provider.label} key saved`, { description: data.key_storage === "keychain" ? "Stored in your macOS Keychain." : "Stored in a private file on this Mac." });
    } catch (err) {
      toast.error("Couldn't save the key", { description: (err as Error).message });
    }
  };
  const test = async () => {
    setTesting(true);
    try {
      const res = await api.post<{ ok: boolean; message: string }>("/api/search/test");
      if (res.ok) toast.success(res.message);
      else toast.error("The provider didn't accept the request", { description: res.message });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="space-y-8">
      <section>
        <SectionTitle title="Web search" description="Used by global search (⌘K) and by the assistant’s search tool in chats on cloud models. Local-model chats never use the web." />
        <div className="rounded-xl border border-border px-4">
          <Switch id="web-enabled" checked={s.web_enabled} onChange={(v) => save({ web_enabled: v })}
            label="Include web results"
            description="Only the words you search for are sent to the provider. Names from your memory and meetings are never sent unless you type them yourself." />
        </div>
        <div className="mt-4 grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="Search provider">
          {data.providers.map((p) => (
            <button key={p.id} role="radio" aria-checked={p.id === s.provider} data-testid={`provider-${p.id}`}
                    onClick={() => save({ provider: p.id })}
                    className={clsx("rounded-xl border p-3 text-left transition-colors",
                      p.id === s.provider ? "border-accent bg-accent-soft/60" : "border-border hover:bg-hover")}>
              <div className="flex items-center gap-2 text-[13px] font-medium">
                {p.label}
                {p.id === "tavily" && <Badge>Default</Badge>}
                {p.needs_key ? (p.has_key ? <Badge tone="success"><Check className="h-3 w-3" /> Key saved</Badge> : <Badge tone="warning">Needs key</Badge>)
                  : <Badge>No key</Badge>}
              </div>
              <p className="mt-1 text-xs text-muted">{p.note}</p>
            </button>
          ))}
        </div>
        {provider.needs_key && (
          <div className="mt-4 space-y-2">
            <label htmlFor="search-key" className="flex items-center gap-1.5 text-[13px] font-medium"><KeyRound className="h-3.5 w-3.5" /> {provider.label} API key</label>
            <div className="flex flex-col gap-2 sm:flex-row">
              <Input id="search-key" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} data-testid="search-key-input"
                     placeholder={provider.has_key ? `Saved (${provider.key_hint}) — paste a new key to replace it` : "Paste your API key"}
                     onKeyDown={(e) => e.key === "Enter" && key.trim() && saveKey()} />
              <Button onClick={saveKey} disabled={!key.trim()} data-testid="save-search-key">Save key</Button>
              {provider.has_key && <Button variant="ghost" onClick={async () => setData(await api.del<SearchSettings>(`/api/search/keys/${provider.id}`))} aria-label="Remove key"><Trash2 className="h-4 w-4" /></Button>}
            </div>
            <p className="flex flex-wrap items-center gap-2 text-xs text-muted">
              {data.key_storage === "keychain" ? "Stored in your macOS Keychain — never in Quorum’s files or code." : "Stored in a private file (permissions 600) in Quorum’s data folder."}
              <a href={provider.signup} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-accent hover:underline">Get a key <ExternalLink className="h-3 w-3" /></a>
              {provider.has_key && <Button size="sm" variant="ghost" onClick={test} loading={testing} data-testid="test-search">Test connection</Button>}
            </p>
          </div>
        )}
        <div className="mt-4 flex items-center gap-3">
          <label htmlFor="max-results" className="text-[13px] font-medium">Results per source</label>
          <Input id="max-results" type="number" min={1} max={20} value={s.max_results} className="h-8 w-20" data-testid="max-results"
                 onChange={(e) => save({ max_results: Number(e.target.value) || 5 })} />
        </div>
      </section>
      <section>
        <SectionTitle title="Searchable sources" description="What global search and the assistant’s search tool look through. All searched on this Mac." />
        <div className="divide-y divide-border rounded-xl border border-border px-4">
          {SOURCE_LABELS.map((src) => (
            <Switch key={src.key} id={`source-${src.key}`} checked={s.sources[src.key]} label={src.label} description={src.description}
                    onChange={(v) => save({ sources: { ...s.sources, [src.key]: v } })} />
          ))}
        </div>
      </section>
      <section>
        <SectionTitle title="Semantic index" description="Finds passages by meaning as well as keywords, with a local embedding model. Nothing leaves this Mac." />
        <div className="space-y-3 rounded-xl border border-border p-4 text-[13px]" data-testid="index-status">
          <div className="flex flex-wrap items-center gap-2">
            <Database className="h-4 w-4 text-muted" /> <span className="font-medium">{data.embedding.model}</span>
            {data.embedding.installed ? <Badge tone="success">Installed</Badge>
              : !data.embedding.ollama_running ? <Badge tone="danger">Ollama not running</Badge> : <Badge tone="warning">Not downloaded</Badge>}
            {!data.embedding.installed && data.embedding.ollama_running && !data.embedding.pull && (
              <Button size="sm" onClick={async () => { await api.post("/api/local-models/pull", { model: data.embedding.model }); load(); refreshHealth(); }}>
                Download (≈270 MB)
              </Button>
            )}
            <Button size="sm" variant="ghost" className="ml-auto" onClick={async () => { await api.post("/api/search/reindex"); load(); }}>
              <RefreshCw className="h-3.5 w-3.5" /> Rebuild index
            </Button>
          </div>
          {data.embedding.pull && data.embedding.pull.state === "running" && <Progress value={data.embedding.pull.percent} label="Downloading the embedding model" />}
          {data.index.state === "running" && (
            <div className="space-y-1"><div className="text-xs text-muted">Indexing {data.index.done} of {data.index.total} passages…</div>
              <Progress value={(data.index.done / Math.max(1, data.index.total)) * 100} /></div>
          )}
          {data.index.state === "done" && <p className="text-xs text-muted">Index up to date{data.index.finished_at ? ` · ${data.index.finished_at.replace("T", " ")}` : ""}.</p>}
          {data.index.state === "error" && <Callout tone="warning">{data.index.error}</Callout>}
          {data.index.state === "idle" && <p className="text-xs text-muted">The index builds in the background the first time you search.</p>}
        </div>
      </section>
    </div>
  );
}
