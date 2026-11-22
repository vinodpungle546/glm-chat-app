"use client";

/**
 * Global search (⌘K): searches chats, meeting transcripts and notes, memory, connectors and (when allowed)
 * the web, with source and date filters.
 */
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Brain, Calendar, ExternalLink, FileAudio, Globe, MessageSquare, Plug, Search, Settings2, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "@/components/app-provider";
import { Badge, Button, Callout, Input, Kbd, Spinner } from "@/components/ui";
import { api, type SearchResponse, type SearchResult, type SearchSource } from "@/lib/api";
import { clsx, modKey } from "@/lib/format";

const SOURCES: { id: SearchSource; label: string; icon: React.ReactNode }[] = [
  { id: "chats", label: "Chats", icon: <MessageSquare className="h-3.5 w-3.5" /> },
  { id: "transcripts", label: "Transcripts", icon: <FileAudio className="h-3.5 w-3.5" /> },
  { id: "memory", label: "Memory", icon: <Brain className="h-3.5 w-3.5" /> },
  { id: "connectors", label: "Connectors", icon: <Plug className="h-3.5 w-3.5" /> },
  { id: "web", label: "Web", icon: <Globe className="h-3.5 w-3.5" /> },
];
const KIND_LABEL: Record<string, string> = {
  chat: "Chat", imported_chat: "claude.ai chat", transcript: "Transcript", notes: "Meeting notes", summary: "Meeting notes",
  fact: "Memory", imported_fact: "claude.ai memory", person: "Person", connector: "Connector", web: "Web",
};
const RANGES = [
  { id: "any", label: "Any time", days: 0 },
  { id: "week", label: "Past week", days: 7 },
  { id: "month", label: "Past month", days: 31 },
  { id: "year", label: "Past year", days: 366 },
  { id: "custom", label: "Custom", days: -1 },
] as const;

/** The date `daysAgo` days ago as 'YYYY-MM-DD' (for the date filters). */
function isoDay(daysAgo: number) {
  return new Date(Date.now() - daysAgo * 86400000).toISOString().slice(0, 10);
}

/** Shows `text` with the search words (3+ letters) highlighted. */
function Highlight({ text, query }: { text: string; query: string }) {
  const terms = query.toLowerCase().split(/\W+/).filter((t) => t.length > 2);
  if (!terms.length) return <>{text}</>;
  const re = new RegExp(`(${terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "ig");
  return <>{text.split(re).map((part, i) => (i % 2 ? <mark key={i} className="rounded bg-accent-soft px-0.5 text-fg">{part}</mark> : part))}</>;
}

/** The site name of a web result, e.g. 'example.com' (without 'www.'). */
function hostname(url: string) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/** One search result: where it's from, title, date, snippet with highlights; click to open it. */
function ResultRow({ result, index, query, onOpen }: { result: SearchResult; index: number; query: string; onOpen: (r: SearchResult) => void }) {
  const web = result.source === "web";
  const stamp = typeof result.meta?.timestamp === "string" ? result.meta.timestamp : "";
  return (
    <li>
      <button onClick={() => onOpen(result)} data-testid="search-result" data-kind={result.kind}
              className="group w-full rounded-xl px-3 py-2.5 text-left transition-colors hover:bg-hover focus:bg-hover focus:outline-none">
        <div className="flex items-center gap-2">
          {web && <span className="font-mono text-[11px] text-faint">[{index}]</span>}
          <span className="truncate text-[13px] font-medium">{result.title}</span>
          <Badge tone={result.kind.startsWith("imported") ? "accent" : "neutral"}>{KIND_LABEL[result.kind] ?? result.kind}</Badge>
          {stamp && <span className="font-mono text-[11px] text-muted">{stamp}</span>}
          {result.date && <span className="ml-auto shrink-0 text-[11px] text-faint">{result.date.slice(0, 10)}</span>}
        </div>
        <p className="mt-0.5 line-clamp-2 text-xs leading-relaxed text-muted"><Highlight text={result.snippet} query={query} /></p>
        {web && (
          <span className="mt-0.5 inline-flex items-center gap-1 text-[11px] text-accent" data-testid="citation">
            {hostname(result.link)} <ExternalLink className="h-3 w-3" />
          </span>
        )}
      </button>
    </li>
  );
}

/** The search dialog: query box, source and date filters, and the results. */
export function SearchDialog() {
  const { searchOpen, setSearchOpen, openSettings, prefs } = useApp();
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [sources, setSources] = useState<SearchSource[]>(["chats", "transcripts", "memory", "connectors", "web"]);
  const [range, setRange] = useState<(typeof RANGES)[number]["id"]>("any");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const mod = modKey();

  const dates = useMemo(() => {
    const r = RANGES.find((x) => x.id === range)!;
    if (r.days === -1) return { date_from: from, date_to: to };
    return r.days ? { date_from: isoDay(r.days), date_to: "" } : { date_from: "", date_to: "" };
  }, [range, from, to]);

  useEffect(() => {
    const q = query.trim();
    if (!searchOpen || q.length < 2) {
      setResult(null);
      setLoading(false);
      return;
    }
    const id = ++seq.current;
    setLoading(true);
    const t = window.setTimeout(async () => {
      try {
        const res = await api.post<SearchResponse>("/api/search", { q, sources, ...dates, web: sources.includes("web") ? null : false });
        if (id === seq.current) { setResult(res); setError(null); }
      } catch (err) {
        if (id === seq.current) setError((err as Error).message);
      } finally {
        if (id === seq.current) setLoading(false);
      }
    }, 300);
    return () => window.clearTimeout(t);
  }, [query, sources, dates, searchOpen]);

  const toggle = (id: SearchSource) =>
    setSources((list) => (list.includes(id) ? (list.length > 1 ? list.filter((s) => s !== id) : list) : [...list, id]));

  const open = (r: SearchResult) => {
    if (r.source === "web" || r.link.startsWith("http")) {
      window.open(r.link, "_blank", "noopener,noreferrer");
      return;
    }
    setSearchOpen(false);
    if (r.link.startsWith("/settings/")) openSettings(r.link.slice("/settings/".length) as "memory");
    else router.push(r.link);
  };

  const groups = result ? SOURCES.filter((s) => sources.includes(s.id)).map((s) => ({ ...s, items: result.groups[s.id] ?? [] })) : [];
  const webNote = result && sources.includes("web") && result.web.status !== "ok" ? result.web : null;
  let webIndex = 0;

  return (
    <DialogPrimitive.Root open={searchOpen} onOpenChange={setSearchOpen}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/40 backdrop-blur-[2px]" />
        <DialogPrimitive.Content data-testid="search-dialog"
          onOpenAutoFocus={(e) => { e.preventDefault(); inputRef.current?.focus(); }}
          className="fixed left-1/2 top-[8vh] z-50 flex max-h-[84vh] w-[calc(100vw-2rem)] max-w-2xl -translate-x-1/2 flex-col overflow-hidden rounded-2xl border border-border bg-surface shadow-card focus:outline-none">
          <DialogPrimitive.Title className="sr-only">Search</DialogPrimitive.Title>
          <DialogPrimitive.Description className="sr-only">Search chats, transcripts, notes, memory, connectors and the web</DialogPrimitive.Description>
          <div className="flex items-center gap-2 border-b border-border px-4">
            <Search className="h-4 w-4 shrink-0 text-muted" />
            <input ref={inputRef} value={query} onChange={(e) => setQuery(e.target.value)} data-testid="search-input"
                   placeholder="Search chats, meetings, notes, memory and the web…" aria-label="Search everything"
                   className="h-12 min-w-0 flex-1 bg-transparent text-[15px] outline-none placeholder:text-faint" />
            {loading && <Spinner className="h-4 w-4" />}
            <DialogPrimitive.Close asChild>
              <Button variant="ghost" size="icon-sm" aria-label="Close search"><X className="h-4 w-4" /></Button>
            </DialogPrimitive.Close>
          </div>
          <div className="flex flex-wrap items-center gap-1.5 border-b border-border px-4 py-2">
            {SOURCES.map((s) => (
              <button key={s.id} onClick={() => toggle(s.id)} aria-pressed={sources.includes(s.id)} data-testid={`filter-${s.id}`}
                className={clsx("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors",
                  sources.includes(s.id) ? "border-accent/40 bg-accent-soft text-accent" : "border-border text-muted hover:text-fg")}>
                {s.icon} {s.label}
              </button>
            ))}
            <span className="ml-auto inline-flex items-center gap-1.5 text-xs text-muted">
              <Calendar className="h-3.5 w-3.5" />
              <select value={range} onChange={(e) => setRange(e.target.value as typeof range)} aria-label="Date range" data-testid="date-range"
                      className="rounded-md border border-border bg-surface px-1.5 py-1 text-xs text-fg">
                {RANGES.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
              </select>
            </span>
            {range === "custom" && (
              <span className="flex w-full items-center gap-2 pt-1 text-xs text-muted">
                From <Input type="date" value={from} onChange={(e) => setFrom(e.target.value)} aria-label="From date" className="h-7 w-36 text-xs" />
                to <Input type="date" value={to} onChange={(e) => setTo(e.target.value)} aria-label="To date" className="h-7 w-36 text-xs" />
              </span>
            )}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2" aria-live="polite">
            {error && <Callout tone="danger" className="m-2">{error}</Callout>}
            {!result && !error && (
              <p className="px-3 py-8 text-center text-[13px] text-muted">
                {query.trim().length < 2 ? "Type at least two characters. Your own data is searched on this Mac; the web only when you include it."
                  : "Searching…"}
              </p>
            )}
            {webNote && webNote.message && (
              <Callout tone="warning" className="mx-2 mb-2" data-testid="web-status"
                action={<Button size="sm" variant="secondary" onClick={() => { setSearchOpen(false); openSettings("search"); }}>
                  <Settings2 className="h-3.5 w-3.5" /> Search settings</Button>}>
                {webNote.message}
              </Callout>
            )}
            {result && result.total === 0 && <p className="px-3 py-8 text-center text-[13px] text-muted">No results for “{result.query}”.</p>}
            {groups.filter((g) => g.items.length).map((g) => (
              <section key={g.id} className="mb-2" data-testid={`search-group-${g.id}`}>
                <h3 className="flex items-center gap-1.5 px-3 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wide text-faint">
                  {g.icon} {g.label} <span className="font-normal normal-case">· {g.items.length}</span>
                </h3>
                <ul>{g.items.map((r) => <ResultRow key={`${r.kind}-${r.id}-${r.link}`} result={r} index={g.id === "web" ? ++webIndex : 0} query={query} onOpen={open} />)}</ul>
              </section>
            ))}
            {result && result.notes.length > 0 && (
              <p className="px-3 pb-2 text-[11px] text-faint">{result.notes.join(" ")}</p>
            )}
          </div>
          <div className="flex items-center justify-between gap-2 border-t border-border px-4 py-2 text-[11px] text-faint">
            <span>{result ? `Ranked by ${result.method}${result.index.state === "running" ? ` · indexing ${result.index.done}/${result.index.total}` : ""}` : "Keyword + meaning search, on this Mac"}</span>
            <span className="flex items-center gap-1">{prefs?.search.web_enabled ? "Web: on" : "Web: off"} · <Kbd>{mod}</Kbd><Kbd>K</Kbd></span>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
