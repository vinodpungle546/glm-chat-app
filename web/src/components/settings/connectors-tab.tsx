"use client";

/**
 * Settings → Connectors: MCP servers the assistant can use. Find them in the official MCP Registry or add
 * one by URL; sign in, test, update or remove them.
 */
import { BadgeCheck, ExternalLink, Globe, KeyRound, LogIn, Package, Plug, Plus, RefreshCw, Search, ShieldAlert, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { SectionTitle } from "@/components/settings/settings-dialog";
import { Badge, Button, Callout, ConfirmDialog, Dialog, EmptyState, Input, Label, Switch } from "@/components/ui";
import { ApiError, api, type Connector, type DirectoryResult } from "@/lib/api";

/** True for a server address on this Mac (allowed in local and confidential chats). */
const isLocal = (url: string) => /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\]|host\.docker\.internal)(:|\/|$)/.test(url);
/** Where a connector runs: 'Local package', 'On this Mac' or 'Remote'. */
const where = (c: Connector) => c.transport === "stdio" ? "Local package" : isLocal(c.url) ? "On this Mac" : "Remote";

/** One connector: status, tools, on/off, sign in, test, update and remove. */
function ConnectorRow({ connector, onChange }: { connector: Connector; onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  const [remove, setRemove] = useState(false);
  const [update, setUpdate] = useState<{ current: string; latest: string } | null>(null);
  const [key, setKey] = useState("");
  const managed = Boolean(connector.transport);

  const test = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ ok: boolean; error: string; needs_sign_in: boolean; tools: unknown[] }>(`/api/connectors/${connector.id}/test`);
      if (r.ok) toast.success(`${connector.name}: ${r.tools.length} tool${r.tools.length === 1 ? "" : "s"}`);
      else toast.error(r.needs_sign_in ? `${connector.name} needs you to sign in` : `${connector.name}: ${r.error}`);
      onChange();
    } finally {
      setBusy(false);
    }
  };
  const signIn = async () => {
    try {
      const { auth_url } = await api.post<{ auth_url: string }>(`/api/connectors/${connector.id}/oauth/start`);
      window.open(auth_url, "_blank", "noopener");
      toast.info("Finish signing in in the new tab, then press Re-test.");
    } catch (err) {
      toast.error("Couldn't start sign-in", { description: (err as Error).message });
    }
  };
  const checkUpdate = async () => {
    const r = await api.get<{ available: boolean; current: string; latest: string }>(`/api/connectors/${connector.id}/updates`);
    if (r.available) setUpdate(r);
    else toast.info(r.latest ? `Up to date (${r.current})` : "No newer version listed.");
  };

  return (
    <div className="space-y-2 rounded-xl border border-border px-3.5 py-2.5" data-testid="connector-row">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-sm font-medium">{connector.name}
            <Badge tone={where(connector) === "Remote" ? "neutral" : "success"}>{where(connector)}</Badge>
            {connector.enabled === false && <Badge tone="warning">Off</Badge>}
            {connector.source?.type === "directory" && <Badge tone="accent" title={connector.source.server}>MCP Registry · {connector.source.version}</Badge>}
            {connector.running && <Badge tone="success">Running</Badge>}
          </div>
          <div className="truncate font-mono text-xs text-muted">
            {connector.transport === "stdio" ? connector.stdio?.command.join(" ") : connector.url}
          </div>
          {connector.description && <p className="text-xs text-muted">{connector.description}</p>}
          {connector.tools && connector.tools.length > 0 && (
            <p className="mt-0.5 text-xs text-muted" data-testid="connector-tools">Tools: {connector.tools.map((t) => t.name).join(", ")}</p>
          )}
          {connector.last_error && connector.last_error !== "sign_in" && <p className="mt-0.5 text-xs text-danger">{connector.last_error}</p>}
          {connector.transport === "stdio" && <p className="mt-0.5 text-[11px] text-muted">Runs on this Mac with your user&apos;s permissions; it may access the network.</p>}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {managed && <Switch id={`c-on-${connector.id}`} label="Enabled" checked={connector.enabled !== false}
                              onChange={async (v) => { await api.patch(`/api/connectors/${connector.id}`, { enabled: v }); onChange(); }} />}
          <Button size="icon-sm" variant="ghost" aria-label={`Remove ${connector.name}`} onClick={() => setRemove(true)}><Trash2 className="h-3.5 w-3.5" /></Button>
        </div>
      </div>
      {managed && (
        <div className="flex flex-wrap gap-1.5">
          <Button size="sm" onClick={test} loading={busy}><RefreshCw className="h-3.5 w-3.5" /> Re-test</Button>
          {connector.auth?.type === "oauth" && (
            <Button size="sm" onClick={signIn}><LogIn className="h-3.5 w-3.5" /> {connector.signed_in ? "Sign in again" : "Sign in"}</Button>
          )}
          {connector.transport === "stdio" && connector.source?.type === "directory" && (
            <Button size="sm" variant="ghost" onClick={checkUpdate}>Check for update</Button>
          )}
          {connector.auth?.type === "header" && (
            <div className="flex gap-1.5">
              <Input aria-label={`New key for ${connector.name}`} type="password" className="h-8 w-48" placeholder="Replace key" value={key} onChange={(e) => setKey(e.target.value)} />
              <Button size="sm" disabled={!key.trim()} onClick={async () => { await api.patch(`/api/connectors/${connector.id}`, { header_value: key }); setKey(""); toast.success("Key saved in the Keychain"); }}>
                <KeyRound className="h-3.5 w-3.5" /> Save</Button>
            </div>
          )}
        </div>
      )}
      <ConfirmDialog open={remove} onOpenChange={setRemove} title={`Remove ${connector.name}?`} confirmLabel="Remove"
        description="The connector is removed, a running package is stopped and its stored keys are deleted from the Keychain."
        onConfirm={async () => { await api.del(`/api/connectors/${connector.id}`); onChange(); }} />
      <ConfirmDialog open={update !== null} onOpenChange={(o) => !o && setUpdate(null)} danger={false} confirmLabel="Update"
        title={`Update ${connector.name}?`} description={`From ${update?.current} to ${update?.latest}. The new version is downloaded and run with your user's permissions.`}
        onConfirm={async () => {
          try {
            await api.post(`/api/connectors/${connector.id}/update`, { version: update?.latest, confirm: true });
            toast.success(`Updated to ${update?.latest}`);
          } catch (err) {
            toast.error("Update failed", { description: (err as Error).message });
          }
          onChange();
        }} />
    </div>
  );
}

/**
 * Adds a connector found in the directory: a remote server (key or sign-in), or a local package (exact
 * version, its settings, and an explicit 'run this on my Mac' confirmation).
 */
function AddFromDirectory({ result, onClose, onAdded }: { result: DirectoryResult; onClose: () => void; onAdded: () => void }) {
  const remote = result.remotes[0];
  const pkg = result.packages.find((p) => p.supported);
  const [mode, setMode] = useState<"remote" | "package">(remote ? "remote" : "package");
  const [values, setValues] = useState<Record<string, string>>({});
  const [auth, setAuth] = useState<"none" | "header" | "oauth">(remote?.headers.some((h) => h.required || h.secret) ? "header" : "none");
  const [probe, setProbe] = useState<{ tools: { name: string }[]; needs_sign_in: boolean; error: string } | null>(null);
  const [command, setCommand] = useState<string[] | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (mode === "remote" && remote) {
      api.post<{ tools: { name: string }[]; needs_sign_in: boolean; error: string }>("/api/connector-directory/probe",
        { url: remote.url, transport: remote.type }).then((r) => {
        setProbe(r);
        if (r.needs_sign_in && !remote.headers.length) setAuth("oauth");
      }).catch(() => setProbe(null));
    }
    if (mode === "package" && pkg) {
      api.post<{ command: string[] }>("/api/connectors/preview-command", { name: result.title, registry_type: pkg.registry_type,
        identifier: pkg.identifier, version: pkg.version, args: pkg.args }).then((r) => setCommand(r.command))
        .catch((err) => { setCommand(null); toast.error((err as Error).message); });
    }
  }, [mode, remote, pkg, result.title]);

  const source = { type: "directory", directory: "mcp-registry", server: result.name, version: result.version,
                   repository: result.repository, publisher: result.publisher };
  const add = async () => {
    setBusy(true);
    try {
      if (mode === "remote" && remote) {
        const header = remote.headers.find((h) => h.secret || h.name === "Authorization") ?? remote.headers[0];
        const extra = Object.fromEntries(remote.headers.filter((h) => h !== header).map((h) => [h.name, values[h.name] ?? ""]));
        const added = await api.post<Connector>("/api/connectors", { kind: "mcp", name: result.title, description: result.description,
          url: remote.url, transport: remote.type, auth_type: auth, header: header?.name ?? "Authorization",
          header_value: header ? values[header.name] ?? "" : "", extra_headers: extra, source });
        if (auth === "oauth") {
          const { auth_url } = await api.post<{ auth_url: string }>(`/api/connectors/${added.id}/oauth/start`);
          window.open(auth_url, "_blank", "noopener");
          toast.info("Finish signing in in the new tab, then press Re-test.");
        } else {
          toast.success(`${result.title} added${added.tools?.length ? ` · ${added.tools.length} tools` : ""}`);
        }
      } else if (pkg) {
        const env = Object.fromEntries(pkg.env.filter((e) => !e.secret).map((e) => [e.name, values[e.name] ?? e.default ?? ""]));
        const secret = Object.fromEntries(pkg.env.filter((e) => e.secret).map((e) => [e.name, values[e.name] ?? ""]));
        const added = await api.post<Connector & { test: { ok: boolean; error: string } }>("/api/connectors/stdio", {
          name: result.title, description: result.description, registry_type: pkg.registry_type, identifier: pkg.identifier,
          version: pkg.version, args: pkg.args, env, secret_env: secret, source, confirm: confirmed });
        if (added.test.ok) toast.success(`${result.title} installed · ${added.tools?.length ?? 0} tools`);
        else toast.error(`${result.title} was added but didn't start: ${added.test.error}`);
      }
      onAdded();
      onClose();
    } catch (err) {
      toast.error("Couldn't add the connector", { description: err instanceof ApiError ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };
  const missing = mode === "remote"
    ? (auth === "header" && (remote?.headers ?? []).some((h) => h.required && !values[h.name]?.trim()))
    : (pkg?.env ?? []).some((e) => e.required && !(values[e.name]?.trim() || e.default));

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} title={`Add ${result.title}`} className="max-w-xl"
      description={<>{result.publisher}{result.official ? " · listed in the official MCP Registry" : ""} · version {result.version}</>}
      footer={<>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" onClick={add} loading={busy} disabled={busy || missing || (mode === "package" && (!confirmed || !command))}>
          {mode === "package" ? "Install and add" : auth === "oauth" ? "Add and sign in" : "Add connector"}</Button>
      </>}>
      <div className="space-y-3 text-[13px]" data-testid="connector-details">
        <p className="text-muted">{result.description}</p>
        {result.repository && <a className="inline-flex items-center gap-1 text-accent hover:underline" href={result.repository} target="_blank" rel="noreferrer">
          Source code <ExternalLink className="h-3 w-3" /></a>}
        {remote && pkg && (
          <div className="flex gap-1.5">
            <Button size="sm" variant={mode === "remote" ? "primary" : "secondary"} onClick={() => setMode("remote")}><Globe className="h-3.5 w-3.5" /> Remote server</Button>
            <Button size="sm" variant={mode === "package" ? "primary" : "secondary"} onClick={() => setMode("package")}><Package className="h-3.5 w-3.5" /> Local package</Button>
          </div>
        )}
        {mode === "remote" && remote && <>
          <div><span className="font-medium">URL</span> <span className="font-mono text-xs">{remote.url}</span> <Badge>{remote.type === "http" ? "Streamable HTTP" : "SSE"}</Badge></div>
          {probe && (probe.tools.length > 0 ? <p data-testid="probe-tools">Tools: {probe.tools.map((t) => t.name).join(", ")}</p>
            : probe.needs_sign_in ? <p className="text-muted">The tools are listed after you sign in.</p>
              : probe.error ? <p className="text-muted">Couldn&apos;t list the tools yet ({probe.error}).</p> : null)}
          <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Sign-in">
            {(["none", "header", "oauth"] as const).map((a) => (
              <Button key={a} size="sm" variant={auth === a ? "primary" : "secondary"} onClick={() => setAuth(a)}>
                {a === "none" ? "No sign-in" : a === "header" ? "API key / header" : "OAuth sign-in"}</Button>
            ))}
          </div>
          {auth === "header" && (remote.headers.length ? remote.headers : [{ name: "Authorization", description: "API key", required: true, secret: true, template: "" }]).map((h) => (
            <div key={h.name}><Label htmlFor={`h-${h.name}`} hint={h.secret ? "Stored in the Keychain" : undefined}>{h.name}{h.required ? " *" : ""}</Label>
              <Input id={`h-${h.name}`} type={h.secret ? "password" : "text"} placeholder={h.template || h.description}
                     value={values[h.name] ?? ""} onChange={(e) => setValues({ ...values, [h.name]: e.target.value })} />
              {h.description && <p className="mt-0.5 text-[11px] text-muted">{h.description}</p>}</div>
          ))}
        </>}
        {mode === "package" && pkg && <>
          <Callout tone="warning" title="This runs third-party code on your Mac">
            {result.publisher} publishes this {pkg.registry_type} package. It will be downloaded and run with your user&apos;s
            permissions, and it may access your files and the network.
          </Callout>
          <div><div className="font-medium">Command</div>
            <code className="mt-1 block rounded-lg bg-surface-2 px-2.5 py-1.5 font-mono text-xs" data-testid="stdio-command">{command ? command.join(" ") : "…"}</code></div>
          {pkg.env.map((e) => (
            <div key={e.name}><Label htmlFor={`e-${e.name}`} hint={e.secret ? "Stored in the Keychain" : undefined}>{e.name}{e.required ? " *" : ""}</Label>
              <Input id={`e-${e.name}`} type={e.secret ? "password" : "text"} placeholder={e.default || e.description}
                     value={values[e.name] ?? ""} onChange={(ev) => setValues({ ...values, [e.name]: ev.target.value })} />
              {e.description && <p className="mt-0.5 text-[11px] text-muted">{e.description}</p>}</div>
          ))}
          <label className="flex items-start gap-2">
            <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} className="mt-0.5" aria-label="Confirm running this package" />
            <span>I trust {result.publisher} and want to install version {pkg.version} and run it with my user&apos;s permissions.</span>
          </label>
        </>}
        {!remote && !pkg && <Callout tone="warning">This server only offers a package type Quorum can&apos;t run yet (for example Docker).</Callout>}
      </div>
    </Dialog>
  );
}

/** Search box and results from the MCP Registry (only your search words are sent). */
function FindConnectors({ onAdded }: { onAdded: () => void }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<DirectoryResult[] | null>(null);
  const [cursor, setCursor] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [chosen, setChosen] = useState<DirectoryResult | null>(null);

  const search = async (more = false) => {
    if (!query.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.get<{ results: DirectoryResult[]; next_cursor: string }>(
        `/api/connector-directory/search?q=${encodeURIComponent(query.trim())}${more && cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
      setResults(more ? [...(results ?? []), ...r.results] : r.results);
      setCursor(r.next_cursor);
    } catch (err) {
      setError((err as Error).message);
      if (!more) setResults(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="space-y-3" data-testid="find-connectors">
      <SectionTitle title="Find connectors" description="Searches the official MCP Registry. Only the words you type are sent; nothing else from Quorum." />
      <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); search(); }}>
        <Input aria-label="Search connectors" placeholder="e.g. jira, calendar, github" value={query} onChange={(e) => setQuery(e.target.value)} />
        <Button type="submit" loading={busy} disabled={!query.trim()}><Search className="h-4 w-4" /> Search</Button>
      </form>
      {error && <Callout tone="danger" data-testid="directory-error">{error}</Callout>}
      {results && results.length === 0 && <p className="text-[13px] text-muted">No connectors found for “{query}”.</p>}
      {results && results.length > 0 && (
        <div className="space-y-2" data-testid="directory-results">
          {results.map((r) => (
            <div key={r.id} className="flex items-start justify-between gap-3 rounded-xl border border-border px-3.5 py-2.5" data-testid="directory-result">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2 text-sm font-medium">{r.title}
                  {r.official && <Badge tone="accent"><BadgeCheck className="h-3 w-3" /> Official registry</Badge>}
                  <Badge>{r.version}</Badge>
                  {r.transports.map((t) => <Badge key={t}>{t === "http" ? "Remote" : t === "sse" ? "Remote (SSE)" : "Local package"}</Badge>)}
                  {r.installed && <Badge tone="success">Added</Badge>}
                </div>
                <p className="text-xs text-muted">{r.publisher}{r.updated_at ? ` · updated ${r.updated_at.slice(0, 10)}` : ""}</p>
                <p className="mt-0.5 line-clamp-2 text-xs">{r.description}</p>
                {r.needs.length > 0 && <p className="mt-0.5 text-[11px] text-muted">Needs: {r.needs.join(", ")}</p>}
                {r.repository && <a className="text-[11px] text-accent hover:underline" href={r.repository} target="_blank" rel="noreferrer">Source</a>}
              </div>
              <Button size="sm" onClick={() => setChosen(r)} disabled={r.installed}>{r.installed ? "Added" : "Details"}</Button>
            </div>
          ))}
          {cursor && <Button size="sm" variant="ghost" onClick={() => search(true)} loading={busy}>More results</Button>}
        </div>
      )}
      {chosen && <AddFromDirectory result={chosen} onClose={() => setChosen(null)} onAdded={() => { onAdded(); search(); }} />}
    </section>
  );
}

/** The Connectors tab: your connectors, Find connectors, and 'Add by URL'. */
export function ConnectorsTab() {
  const [list, setList] = useState<Connector[] | null>(null);
  const [draft, setDraft] = useState({ name: "", url: "", description: "", transport: "http", auth_type: "none", header_value: "" });
  const load = () => api.get<Connector[]>("/api/connectors").then(setList).catch(() => setList([]));
  useEffect(() => { load(); }, []);

  const add = async () => {
    try {
      const body = draft.transport === "simple" ? { kind: "mcp", name: draft.name, url: draft.url, description: draft.description }
        : { kind: "mcp", ...draft };
      await api.post("/api/connectors", body);
      setDraft({ name: "", url: "", description: "", transport: "http", auth_type: "none", header_value: "" });
      load();
      toast.success("Connector added");
    } catch (err) {
      toast.error("Couldn't add the connector", { description: (err as Error).message });
    }
  };

  return (
    <div className="space-y-6">
      <SectionTitle title="Connectors" description={<>MCP servers the assistant can call, shared by all projects. With a local model or in a
        confidential chat, only connectors on this Mac are used. Keys and sign-ins are stored in the Keychain.</>} />
      {list === null ? <div className="skeleton h-14" /> : list.length === 0 ? (
        <EmptyState icon={<Plug className="h-5 w-5" />} title="No connectors">Find one below, or add an MCP server URL.</EmptyState>
      ) : (
        <div className="space-y-2" data-testid="connector-list">
          {list.map((c) => <ConnectorRow key={c.id} connector={c} onChange={load} />)}
        </div>
      )}
      <FindConnectors onAdded={load} />
      <div className="space-y-3 rounded-xl border border-border p-4">
        <div className="flex items-center gap-2 text-sm font-medium"><ShieldAlert className="h-4 w-4 text-muted" /> Add an MCP server by URL</div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div><Label htmlFor="c-name">Name</Label><Input id="c-name" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></div>
          <div><Label htmlFor="c-url">URL</Label><Input id="c-url" value={draft.url} placeholder="http://localhost:9000/mcp" onChange={(e) => setDraft({ ...draft, url: e.target.value })} /></div>
          <div><Label htmlFor="c-transport">Transport</Label>
            <select id="c-transport" value={draft.transport} onChange={(e) => setDraft({ ...draft, transport: e.target.value })}
                    className="h-9 w-full rounded-lg border border-border bg-surface px-2.5 text-sm">
              <option value="http">Streamable HTTP</option>
              <option value="sse">SSE</option>
              <option value="simple">Simple (one tool named like the connector)</option>
            </select></div>
          {draft.transport !== "simple" && <div><Label htmlFor="c-auth">Sign-in</Label>
            <select id="c-auth" value={draft.auth_type} onChange={(e) => setDraft({ ...draft, auth_type: e.target.value })}
                    className="h-9 w-full rounded-lg border border-border bg-surface px-2.5 text-sm">
              <option value="none">None</option>
              <option value="header">API key (Authorization header)</option>
              <option value="oauth">OAuth sign-in</option>
            </select></div>}
          {draft.auth_type === "header" && draft.transport !== "simple" && (
            <div className="sm:col-span-2"><Label htmlFor="c-key" hint="Stored in the Keychain">API key</Label>
              <Input id="c-key" type="password" value={draft.header_value} onChange={(e) => setDraft({ ...draft, header_value: e.target.value })} /></div>
          )}
        </div>
        <div><Label htmlFor="c-desc">Description</Label><Input id="c-desc" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} /></div>
        <Button variant="primary" onClick={add} disabled={!draft.name.trim() || !draft.url.trim()}><Plus className="h-4 w-4" /> Add connector</Button>
      </div>
    </div>
  );
}
