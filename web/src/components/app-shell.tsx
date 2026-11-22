"use client";

/**
 * The frame around every page: the sidebar, the top bar (Chat / Meetings tabs, search, theme toggle),
 * keyboard shortcuts and the global dialogs (settings, shortcuts, search).
 */
import { Menu, MessageSquare, Moon, Mic, Monitor, PanelLeft, Search, Sun } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import { useEffect } from "react";
import { useApp } from "@/components/app-provider";
import { SearchDialog } from "@/components/search/search-dialog";
import { SettingsDialog } from "@/components/settings/settings-dialog";
import { ShortcutsDialog } from "@/components/shortcuts-dialog";
import { MeetingCheckRunner } from "@/components/meetings/meeting-check";
import { LivePanel, RecordingIndicator } from "@/components/meetings/recording";
import { Sidebar } from "@/components/sidebar/sidebar";
import { Button, Kbd, Tooltip } from "@/components/ui";
import { clsx, isMac, modKey } from "@/lib/format";

/** Switches between light and dark theme. */
export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const order = ["system", "light", "dark"] as const;
  const current = (order as readonly string[]).includes(theme ?? "") ? (theme as (typeof order)[number]) : "system";
  const next = order[(order.indexOf(current) + 1) % order.length];
  const Icon = current === "dark" ? Moon : current === "light" ? Sun : Monitor;
  return (
    <Tooltip content={`Theme: ${current} (switch to ${next})`}>
      <Button variant="ghost" size="icon" aria-label={`Theme: ${current}. Switch to ${next}`}
              data-testid="theme-toggle" data-theme-mode={current} onClick={() => setTheme(next)}>
        <Icon className="h-[18px] w-[18px]" />
      </Button>
    </Tooltip>
  );
}

/**
 * Global keyboard shortcuts (⌘ on a Mac, Ctrl elsewhere): ⌘⇧O new chat, ⌘K search, ⌘⇧F open the sidebar,
 * ⌘B show/hide the sidebar, ⌘, settings, ⌘⇧M meetings, ⌘⇧L light/dark theme, ⌘/ list of shortcuts.
 * Esc closes the sidebar drawer on tablets.
 */
function useShortcuts() {
  const router = useRouter();
  const { setSidebarOpen, openSettings, setShortcutsOpen, setSearchOpen } = useApp();
  const { resolvedTheme, setTheme } = useTheme();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && window.matchMedia("(max-width: 1023px)").matches
          && !document.querySelector("[role=dialog]")) {
        setSidebarOpen(false); // close the drawer on tablets
        return;
      }
      const mod = isMac() ? e.metaKey : e.ctrlKey;
      if (!mod) return;
      const key = e.key.toLowerCase();
      if (e.shiftKey && key === "o") {
        e.preventDefault();
        router.push("/");
        window.dispatchEvent(new CustomEvent("quorum:new-chat"));
      } else if (!e.shiftKey && key === "k") {
        e.preventDefault();
        setSearchOpen(true);
      } else if (e.shiftKey && key === "f") {
        e.preventDefault();
        setSidebarOpen(true);
        window.setTimeout(() => document.getElementById("session-search")?.focus(), 50);
      } else if (!e.shiftKey && key === "b") {
        e.preventDefault();
        setSidebarOpen((v) => !v);
      } else if (!e.shiftKey && key === ",") {
        e.preventDefault();
        openSettings("general");
      } else if (e.shiftKey && key === "m") {
        e.preventDefault();
        router.push("/meetings");
      } else if (e.shiftKey && key === "l") {
        e.preventDefault();
        setTheme(resolvedTheme === "dark" ? "light" : "dark");
      } else if (key === "/") {
        e.preventDefault();
        setShortcutsOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [router, setSidebarOpen, openSettings, setShortcutsOpen, setSearchOpen, resolvedTheme, setTheme]);
}

/** The 'Search everything…' button in the top bar; opens the search dialog (⌘K). */
function SearchBar() {
  const { setSearchOpen } = useApp();
  const mod = modKey();
  return (
    <button onClick={() => setSearchOpen(true)} data-testid="open-search" aria-label="Search everything"
            className="hidden h-8 w-full max-w-xs items-center gap-2 rounded-lg border border-border bg-surface-2 px-2.5 text-[13px] text-faint transition-colors hover:text-muted sm:flex">
      <Search className="h-3.5 w-3.5" /> <span className="flex-1 text-left">Search everything…</span>
      <span className="flex gap-0.5"><Kbd>{mod}</Kbd><Kbd>K</Kbd></span>
    </button>
  );
}

/** Lays out the sidebar, top bar and page content. */
export function AppShell({ children }: { children: React.ReactNode }) {
  const { sidebarOpen, setSidebarOpen, setSearchOpen, livePanelOpen } = useApp();
  const pathname = usePathname();
  useShortcuts();

  // Close the overlay sidebar after navigating on narrow screens.
  useEffect(() => {
    if (window.matchMedia("(max-width: 1023px)").matches) setSidebarOpen(false);
  }, [pathname, setSidebarOpen]);

  const onMeetings = pathname.startsWith("/meetings");
  return (
    <div className="flex h-full">
      {/* Sidebar: inline on desktop, overlay drawer on tablet/phone */}
      <div
        className={clsx("fixed inset-0 z-30 bg-black/30 transition-opacity lg:hidden",
          sidebarOpen ? "opacity-100" : "pointer-events-none opacity-0")}
        onClick={() => setSidebarOpen(false)}
        aria-hidden
      />
      <aside
        data-testid="sidebar"
        data-open={sidebarOpen}
        aria-label="Sidebar"
        className={clsx(
          "fixed inset-y-0 left-0 z-40 w-[280px] border-r border-border bg-sidebar transition-transform duration-200 lg:static lg:z-auto",
          sidebarOpen ? "translate-x-0" : "-translate-x-full lg:hidden",
        )}
      >
        <Sidebar />
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border/70 px-2 sm:px-3">
          {!sidebarOpen && (
            <Tooltip content="Open sidebar">
              <Button variant="ghost" size="icon" aria-label="Open sidebar" data-testid="open-sidebar"
                      onClick={() => setSidebarOpen(true)}>
                <PanelLeft className="hidden h-[18px] w-[18px] lg:block" />
                <Menu className="h-[18px] w-[18px] lg:hidden" />
              </Button>
            </Tooltip>
          )}
          <nav aria-label="Main" className="flex items-center gap-1 rounded-xl bg-surface-2 p-1">
            <Link href="/" data-testid="nav-chat"
                  className={clsx("flex items-center gap-1.5 rounded-lg px-3 py-1 text-[13px] font-medium transition-colors",
                    !onMeetings ? "bg-surface text-fg shadow-sm" : "text-muted hover:text-fg")}>
              <MessageSquare className="h-3.5 w-3.5" /> Chat
            </Link>
            <Link href="/meetings" data-testid="nav-meetings"
                  className={clsx("flex items-center gap-1.5 rounded-lg px-3 py-1 text-[13px] font-medium transition-colors",
                    onMeetings ? "bg-surface text-fg shadow-sm" : "text-muted hover:text-fg")}>
              <Mic className="h-3.5 w-3.5" /> Meetings
            </Link>
          </nav>
          <RecordingIndicator />
          <div className="ml-auto flex min-w-0 flex-1 items-center justify-end gap-1">
            <SearchBar />
            <Button variant="ghost" size="icon" aria-label="Search" className="sm:hidden" onClick={() => setSearchOpen(true)}>
              <Search className="h-[18px] w-[18px]" />
            </Button>
            <ThemeToggle />
          </div>
        </header>
        <div className="flex min-h-0 flex-1">
          <main className="min-h-0 min-w-0 flex-1">{children}</main>
          {livePanelOpen && <LivePanel />}
        </div>
      </div>
      <MeetingCheckRunner />
      <SettingsDialog />
      <ShortcutsDialog />
      <SearchDialog />
    </div>
  );
}
