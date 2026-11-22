"use client";

/**
 * Settings → Models: add cloud providers (Claude, OpenAI, OpenAI-compatible), their API keys (kept in the
 * Keychain) and which of their models appear in the picker.
 */
import { Cloud, KeyRound, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { SectionTitle } from "@/components/settings/settings-dialog";
import { Badge, Button, Callout, ConfirmDialog, Input, Label } from "@/components/ui";
import { api, type CloudProvider, type ProvidersResponse } from "@/lib/api";

const ERRORS: Record<string, string> = {
  missing_key: "No API key yet.",
  invalid_key: "The API key was rejected.",
  rate_limited: "Rate limited. Try again in a minute.",
  offline: "Can't reach the provider (offline?).",
  unknown_model: "Model not found.",
  overloaded: "The provider is overloaded right now.",
  error: "The provider answered with an error.",
};

/** The provider types that can be added. */
type Kind = CloudProvider["kind"];

/** The Models tab: GAIA's status, the added providers and the 'Add provider' form. */
export function CloudModelsTab() {
  const { refreshModels } = useApp();
  const [data, setData] = useState<ProvidersResponse | null>(null);
  const [draft, setDraft] = useState<{ kind: Kind; label: string; base_url: string; key: string; auth_header: "bearer" | "api-key" }>(
    { kind: "anthropic", label: "", base_url: "", key: "", auth_header: "bearer" });
  const [busy, setBusy] = useState(false);
  const load = () => api.get<ProvidersResponse>("/api/providers").then(setData).catch(() => setData(null));
  useEffect(() => { load(); }, []);

  const changed = async () => { await load(); await refreshModels(); };

  const add = async () => {
    setBusy(true);
    try {
      const added = await api.post<CloudProvider>("/api/providers", draft);
      setDraft({ kind: "anthropic", label: "", base_url: "", key: "", auth_header: "bearer" });
      if (added.has_key) {
        const result = await api.post<{ ok: boolean; message?: string; count?: number }>(`/api/providers/${added.id}/test`);
        if (result.ok) toast.success(`${added.label}: connected, ${result.count} models found`);
        else toast.error(`${added.label}: ${result.message}`);
      } else {
        toast.success(`${added.label} added. Add its API key to use it.`);
      }
      await changed();
    } catch (err) {
      toast.error("Couldn't add the provider", { description: (err as Error).message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <SectionTitle title="Models" description={<>Cloud chat models. Their models appear in the model picker next to the local ones.
        Keys are stored in the macOS Keychain, never in settings files. Transcription, speaker identification
        on this Mac, on-screen names and the vision fallback always stay local.</>} />

      <div className="rounded-xl border border-border p-3.5" data-testid="provider-gaia">
        <div className="flex items-center gap-2 text-sm font-medium"><Cloud className="h-4 w-4" /> Globant GAIA
          <Badge tone="accent">Built in</Badge>
          <Badge tone={data?.gaia.configured ? "success" : "warning"}>{data?.gaia.configured ? "Ready" : "No key in .env"}</Badge></div>
        <p className="mt-1 text-xs text-muted">{data?.gaia.model ?? "GLM-5.3 Flash"} · configured in .env (GAIA_*); the default cloud model.</p>
      </div>

      {data === null ? <div className="skeleton h-20" /> : data.providers.map((p) => (
        <ProviderCard key={p.id} provider={p} onChange={changed} />
      ))}

      <div className="space-y-3 rounded-xl border border-border p-4" data-testid="add-provider">
        <div className="text-sm font-medium">Add a cloud provider</div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <Label htmlFor="p-kind">Provider</Label>
            <select id="p-kind" value={draft.kind} onChange={(e) => setDraft({ ...draft, kind: e.target.value as Kind })}
                    className="h-9 w-full rounded-lg border border-border bg-surface px-2.5 text-sm">
              <option value="anthropic">Anthropic (Claude)</option>
              <option value="openai">OpenAI (ChatGPT models)</option>
              <option value="openai_compatible">OpenAI-compatible endpoint (Azure, OpenRouter, Groq…)</option>
            </select>
          </div>
          <div>
            <Label htmlFor="p-key" hint="Stored in the Keychain">API key</Label>
            <Input id="p-key" type="password" autoComplete="off" value={draft.key}
                   onChange={(e) => setDraft({ ...draft, key: e.target.value })} />
          </div>
          {draft.kind === "openai_compatible" && <>
            <div><Label htmlFor="p-label">Name</Label>
              <Input id="p-label" value={draft.label} placeholder="OpenRouter" onChange={(e) => setDraft({ ...draft, label: e.target.value })} /></div>
            <div><Label htmlFor="p-url">Base URL</Label>
              <Input id="p-url" value={draft.base_url} placeholder="https://openrouter.ai/api/v1"
                     onChange={(e) => setDraft({ ...draft, base_url: e.target.value })} /></div>
            <div><Label htmlFor="p-auth">Key header</Label>
              <select id="p-auth" value={draft.auth_header} onChange={(e) => setDraft({ ...draft, auth_header: e.target.value as "bearer" | "api-key" })}
                      className="h-9 w-full rounded-lg border border-border bg-surface px-2.5 text-sm">
                <option value="bearer">Authorization: Bearer (most services)</option>
                <option value="api-key">api-key (Azure OpenAI)</option>
              </select></div>
          </>}
        </div>
        <Button variant="primary" onClick={add} loading={busy}
                disabled={busy || (draft.kind === "openai_compatible" && !draft.base_url.trim())}>
          <Plus className="h-4 w-4" /> Add provider
        </Button>
      </div>
    </div>
  );
}

/** One provider: key (masked), Test connection, its models with on/off checkboxes, and Remove. */
function ProviderCard({ provider, onChange }: { provider: CloudProvider; onChange: () => Promise<void> }) {
  const [key, setKey] = useState("");
  const [manual, setManual] = useState("");
  const [testing, setTesting] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; message?: string; kind?: string } | null>(null);
  const [enabled, setEnabled] = useState(provider.enabled);
  useEffect(() => { setEnabled(provider.enabled); }, [provider.enabled]);

  const test = async () => {
    setTesting(true);
    try {
      const r = await api.post<{ ok: boolean; message?: string; kind?: string; count?: number }>(`/api/providers/${provider.id}/test`);
      setResult(r.ok ? { ok: true, message: `Connected · ${r.count} models` } : { ok: false, kind: r.kind, message: r.message });
      await onChange();
    } finally {
      setTesting(false);
    }
  };
  const patch = async (fields: Record<string, unknown>) => {
    try {
      await api.patch(`/api/providers/${provider.id}`, fields);
      await onChange();
    } catch (err) {
      toast.error((err as Error).message);
    }
  };
  const toggle = (model: string, on: boolean) => {
    const next = on ? [...enabled, model] : enabled.filter((m) => m !== model);
    setEnabled(next);  // show the change right away; the picker updates once it's saved
    patch({ enabled: next });
  };

  return (
    <div className="space-y-3 rounded-xl border border-border p-3.5" data-testid={`provider-${provider.id}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium"><Cloud className="h-4 w-4" /> {provider.label}
          <Badge tone={provider.has_key ? "success" : "warning"}>{provider.has_key ? `Key ${provider.key_hint}` : "No key"}</Badge>
          {provider.last_error && <Badge tone="danger">{ERRORS[provider.last_error] ?? provider.last_error}</Badge>}
        </div>
        <div className="flex gap-1.5">
          <Button size="sm" onClick={test} loading={testing} disabled={!provider.has_key || testing}>
            <RefreshCw className="h-3.5 w-3.5" /> Test connection</Button>
          <Button size="icon-sm" variant="ghost" aria-label={`Remove ${provider.label}`} onClick={() => setRemoving(true)}>
            <Trash2 className="h-3.5 w-3.5" /></Button>
        </div>
      </div>
      {provider.kind === "openai_compatible" && <p className="font-mono text-xs text-muted">{provider.base_url}</p>}
      {result && (
        <Callout tone={result.ok ? "success" : "danger"} data-testid="provider-test-result">
          {result.ok ? result.message : `${ERRORS[result.kind ?? "error"] ?? ""} ${result.message ?? ""}`}
        </Callout>
      )}
      <div className="flex gap-2">
        <Input aria-label={`New API key for ${provider.label}`} type="password" autoComplete="off" placeholder={provider.has_key ? "Replace the API key" : "API key"}
               value={key} onChange={(e) => setKey(e.target.value)} />
        <Button size="md" disabled={!key.trim()} onClick={async () => { await patch({ key }); setKey(""); toast.success("Key saved in the Keychain"); }}>
          <KeyRound className="h-4 w-4" /> Save key</Button>
      </div>
      <div>
        <div className="mb-1.5 text-xs font-medium text-muted">Models shown in the picker</div>
        {provider.models.length === 0 ? (
          <p className="text-xs text-muted">Test the connection to list models, or add one by name below.</p>
        ) : (
          <div className="grid max-h-56 gap-1 overflow-y-auto sm:grid-cols-2" data-testid="provider-models">
            {provider.models.map((m) => (
              <label key={m.id} className="flex items-center gap-2 rounded-md px-1.5 py-1 text-[13px] hover:bg-hover">
                <input type="checkbox" checked={enabled.includes(m.id)} onChange={(e) => toggle(m.id, e.target.checked)} />
                <span className="truncate">{m.label}</span>
                {m.label !== m.id && <span className="truncate font-mono text-[11px] text-muted">{m.id}</span>}
              </label>
            ))}
          </div>
        )}
        <div className="mt-2 flex gap-2">
          <Input aria-label="Model name" placeholder="Add a model by name, e.g. claude-opus-5-5" value={manual}
                 onChange={(e) => setManual(e.target.value)} />
          <Button disabled={!manual.trim()} onClick={async () => { await patch({ manual_model: manual.trim() }); setManual(""); }}>
            <Plus className="h-4 w-4" /> Add</Button>
        </div>
      </div>
      <ConfirmDialog open={removing} onOpenChange={setRemoving} title={`Remove ${provider.label}?`}
        description="Its API key is deleted from the Keychain and its models leave the picker. Chats keep their history."
        confirmLabel="Remove" onConfirm={async () => { await api.del(`/api/providers/${provider.id}`); await onChange(); }} />
    </div>
  );
}
