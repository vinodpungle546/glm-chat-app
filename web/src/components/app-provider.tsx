"use client";

/**
 * Global state for the whole web app (React context): the model list and the selected model, preferences,
 * the chat list, the settings dialog, the sidebar and the search dialog. Any component reads it with
 * useApp().
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { api, ApiError, type Health, type ModelEntry, type ModelsResponse, type Prefs, type RecordStatus, type SessionSummary } from "@/lib/api";

/** The tabs of the Settings dialog (the id 'speakers' is shown as 'Meetings'). */
export type SettingsTab = "general" | "projects" | "memory" | "speakers" | "skills" | "connectors" | "search" | "import"
  | "providers" | "models";

/** Everything the context provides; see AppProvider for how each value is kept up to date. */
interface AppState {
  models: ModelsResponse | null;
  modelsError: string | null;
  selectedModel: ModelEntry | null;
  selectModel: (id: string) => void;
  refreshModels: () => Promise<void>;
  sessions: SessionSummary[];
  sessionsLoading: boolean;
  sessionQuery: string;
  setSessionQuery: (q: string) => void;
  refreshSessions: () => Promise<void>;
  upsertSession: (s: SessionSummary) => void;
  prefs: Prefs | null;
  updatePrefs: (values: Partial<Prefs>) => Promise<void>;
  health: Health | null;
  refreshHealth: () => Promise<void>;
  settingsTab: SettingsTab | null;
  openSettings: (tab?: SettingsTab) => void;
  closeSettings: () => void;
  sidebarOpen: boolean;
  setSidebarOpen: (open: boolean | ((v: boolean) => boolean)) => void;
  shortcutsOpen: boolean;
  setShortcutsOpen: (open: boolean) => void;
  searchOpen: boolean;
  setSearchOpen: (open: boolean) => void;
  showImported: boolean;
  setShowImported: (show: boolean) => void;
  /** The call being recorded (polled every 2 s while recording, every 15 s otherwise); null until known. */
  recording: RecordStatus | null;
  refreshRecording: () => Promise<RecordStatus | null>;
  /** Whether the live transcript panel is open (it stays open across pages, docked on the right). */
  livePanelOpen: boolean;
  setLivePanelOpen: (open: boolean) => void;
}

const Ctx = createContext<AppState | null>(null);
const MODEL_KEY = "quorum.selectedModel";
const SIDEBAR_KEY = "quorum.sidebar";
const IMPORTED_KEY = "quorum.showImported";

/** Reads a value from the browser's localStorage; null when storage is blocked (private windows). */
function readLocal(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** Saves a value in localStorage, ignoring errors when storage is blocked. */
function writeLocal(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* private mode */
  }
}

/**
 * Holds the global state. Loads models, preferences and health on start and refreshes models and health
 * every 15 s; reloads the chat list when the search box changes.
 */
export function AppProvider({ children }: { children: React.ReactNode }) {
  const [models, setModels] = useState<ModelsResponse | null>(null);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string>("");
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [sessionQuery, setSessionQuery] = useState("");
  const [prefs, setPrefs] = useState<Prefs | null>(null);
  const [recording, setRecording] = useState<RecordStatus | null>(null);
  const [livePanelOpen, setLivePanelOpen] = useState(false);

  const refreshRecording = useCallback(async () => {
    try {
      const status = await api.get<RecordStatus>("/api/meetings/record/status");
      setRecording(status);
      return status;
    } catch {
      setRecording(null);  // transcriber not running: no indicator
      return null;
    }
  }, []);

  // Poll the recorder: often while a call is recorded (elapsed time, levels), rarely otherwise (a recording
  // started from another tab, or one that ended by itself).
  const recordingState = recording?.state;
  useEffect(() => {
    refreshRecording();
    const every = recordingState === "recording" ? 2000 : 15000;
    const timer = window.setInterval(() => { if (!document.hidden) refreshRecording(); }, every);
    return () => window.clearInterval(timer);
  }, [refreshRecording, recordingState]);
  const [health, setHealth] = useState<Health | null>(null);
  const [settingsTab, setSettingsTab] = useState<SettingsTab | null>(null);
  const [sidebarOpen, setSidebarOpenState] = useState(true);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [showImported, setShowImportedState] = useState(true);
  const queryRef = useRef(sessionQuery);
  useEffect(() => { queryRef.current = sessionQuery; }, [sessionQuery]);
  const importedRef = useRef(showImported);
  useEffect(() => { importedRef.current = showImported; }, [showImported]);

  const refreshModels = useCallback(async () => {
    try {
      const data = await api.get<ModelsResponse>("/api/models");
      setModels(data);
      setModelsError(null);
      // Keep the current selection if that model still exists; otherwise use the saved preference, then this
      // browser's last choice, then the registry default.
      setSelectedId((current) => {
        const ids = new Set(data.models.map((m) => m.id));
        if (current && ids.has(current)) return current;
        const remembered = readLocal(MODEL_KEY);
        if (data.selected && ids.has(data.selected)) return data.selected;
        if (remembered && ids.has(remembered)) return remembered;
        return data.default;
      });
    } catch (err) {
      setModelsError((err as Error).message);
    }
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      const q = queryRef.current.trim();
      const params = new URLSearchParams({ imported: importedRef.current ? "show" : "hide" });
      if (q) params.set("q", q);
      const list = await api.get<SessionSummary[]>(`/api/sessions?${params}`);
      setSessions(list);
    } catch {
      /* the sidebar shows an empty list; errors surface where the user acts */
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await api.get<Health>("/api/health"));
    } catch {
      setHealth(null);
    }
  }, []);

  useEffect(() => {
    const stored = readLocal(SIDEBAR_KEY);
    const narrow = window.matchMedia("(max-width: 1023px)").matches;
    setSidebarOpenState(narrow ? false : stored !== "closed");
    const remembered = readLocal(MODEL_KEY);
    if (remembered) setSelectedId(remembered);
    if (readLocal(IMPORTED_KEY) === "hidden") setShowImportedState(false);
    api.get<Prefs>("/api/preferences").then(setPrefs).catch(() => setPrefs(null));
    refreshModels();
    refreshHealth();
    const timer = window.setInterval(() => {
      refreshModels();
      refreshHealth();
    }, 15000);
    return () => window.clearInterval(timer);
  }, [refreshModels, refreshHealth]);

  useEffect(() => {
    const t = window.setTimeout(refreshSessions, sessionQuery ? 200 : 0);
    return () => window.clearTimeout(t);
  }, [sessionQuery, showImported, refreshSessions]);

  const setShowImported = useCallback((show: boolean) => {
    importedRef.current = show;
    setShowImportedState(show);
    writeLocal(IMPORTED_KEY, show ? "shown" : "hidden");
  }, []);

  const selectModel = useCallback((id: string) => {
    setSelectedId(id);
    writeLocal(MODEL_KEY, id);
    api.put<Prefs>("/api/preferences", { selected_model: id }).then(setPrefs).catch((err: ApiError) => {
      toast.error("Couldn't save the model choice", { description: err.message });
    });
  }, []);

  // Every new chat starts on the default chat model (models.json "default": the cloud model).
  // Picking another model in the sidebar applies to the chat you're in; the next "New chat" starts
  // on the default again. A stale browser copy of an older choice can't override it.
  const defaultRef = useRef("");
  useEffect(() => { defaultRef.current = models?.default ?? ""; }, [models?.default]);
  useEffect(() => {
    const toDefault = () => { if (defaultRef.current) selectModel(defaultRef.current); };
    window.addEventListener("quorum:new-chat", toDefault);
    return () => window.removeEventListener("quorum:new-chat", toDefault);
  }, [selectModel]);

  const updatePrefs = useCallback(async (values: Partial<Prefs>) => {
    const next = await api.put<Prefs>("/api/preferences", values);
    setPrefs(next);
  }, []);

  const upsertSession = useCallback((s: SessionSummary) => {
    setSessions((list) => [s, ...list.filter((x) => x.id !== s.id)]);
  }, []);

  const setSidebarOpen = useCallback((open: boolean | ((v: boolean) => boolean)) => {
    setSidebarOpenState((current) => {
      const next = typeof open === "function" ? open(current) : open;
      if (!window.matchMedia("(max-width: 1023px)").matches) writeLocal(SIDEBAR_KEY, next ? "open" : "closed");
      return next;
    });
  }, []);

  const selectedModel = useMemo(
    () => models?.models.find((m) => m.id === selectedId) ?? null,
    [models, selectedId],
  );

  const value: AppState = {
    models, modelsError, selectedModel, selectModel, refreshModels,
    sessions, sessionsLoading, sessionQuery, setSessionQuery, refreshSessions, upsertSession,
    prefs, updatePrefs, health, refreshHealth,
    settingsTab, openSettings: (tab = "general") => setSettingsTab(tab), closeSettings: () => setSettingsTab(null),
    sidebarOpen, setSidebarOpen, shortcutsOpen, setShortcutsOpen, searchOpen, setSearchOpen,
    showImported, setShowImported, recording, refreshRecording, livePanelOpen, setLivePanelOpen,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/** The global app state; must be used inside <AppProvider>. */
export function useApp(): AppState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useApp outside AppProvider");
  return ctx;
}
