"use client";

/** The left sidebar: model picker, New chat, chat search and the chats grouped by date. */
import * as Menu from "@radix-ui/react-dropdown-menu";
import { Download, Eye, EyeOff, Lock, MoreHorizontal, PanelLeftClose, Pencil, Search, Settings, SquarePen, Trash2, X } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { LogoMark, PRODUCT } from "@/components/logo";
import { ModelPicker } from "@/components/sidebar/model-picker";
import { Button, ConfirmDialog, Input, Kbd, Tooltip } from "@/components/ui";
import { api, type SessionSummary } from "@/lib/api";
import { clsx, modKey, relativeDay } from "@/lib/format";

/** One chat in the list: open, rename (inline) or delete (with confirmation). */
function SessionItem({ session, active }: { session: SessionSummary; active: boolean }) {
  const { refreshSessions, upsertSession } = useApp();
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(session.title);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  const save = async () => {
    setEditing(false);
    const next = title.trim();
    if (!next || next === session.title) return setTitle(session.title);
    try {
      await api.patch(`/api/sessions/${session.id}`, { title: next });
      upsertSession({ ...session, title: next });
      refreshSessions();
    } catch (err) {
      setTitle(session.title);
      toast.error("Couldn't rename the chat", { description: (err as Error).message });
    }
  };

  const remove = async () => {
    try {
      await api.del(`/api/sessions/${session.id}`);
      toast.success("Chat deleted");
      await refreshSessions();
      if (active) router.push("/");
    } catch (err) {
      toast.error("Couldn't delete the chat", { description: (err as Error).message });
    }
  };

  if (editing) {
    return (
      <div className="px-1 py-0.5">
        <Input ref={inputRef} value={title} aria-label="Chat title" data-testid="rename-input"
               className="h-8" onChange={(e) => setTitle(e.target.value)} onBlur={save}
               onKeyDown={(e) => {
                 if (e.key === "Enter") save();
                 if (e.key === "Escape") { setTitle(session.title); setEditing(false); }
               }} />
      </div>
    );
  }
  return (
    <div className={clsx("group relative flex items-center rounded-lg", active ? "bg-hover" : "hover:bg-hover/70")}
         data-testid="session-item" data-session-id={session.id}>
      <Link href={`/c/${session.id}`} className="flex min-w-0 flex-1 items-center gap-1.5 py-1.5 pl-2.5 pr-8 text-[13px]"
            title={session.title} aria-current={active ? "page" : undefined}>
        {session.confidential && <Lock className="h-3 w-3 shrink-0 text-muted" aria-label="Confidential chat" />}
        <span className="truncate">{session.title}</span>
        {session.imported && (
          <span className="ml-auto shrink-0 rounded-full border border-accent/25 bg-accent-soft px-1.5 text-[10px] font-medium leading-4 text-accent"
                data-testid="imported-tag" title={`Imported from claude.ai${session.imported_at ? ` on ${session.imported_at.slice(0, 10)}` : ""}`}>
            Imported
          </span>
        )}
      </Link>
      <Menu.Root>
        <Menu.Trigger asChild>
          <button aria-label={`Options for ${session.title}`} data-testid="session-menu"
                  className="absolute right-1 rounded-md p-1 text-muted opacity-0 hover:bg-border hover:text-fg focus:opacity-100 group-hover:opacity-100 data-[state=open]:opacity-100">
            <MoreHorizontal className="h-4 w-4" />
          </button>
        </Menu.Trigger>
        <Menu.Portal>
          <Menu.Content align="start" className="z-50 min-w-36 rounded-xl border border-border bg-surface p-1 shadow-card">
            <Menu.Item onSelect={() => setEditing(true)}
                       className="flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-[13px] outline-none data-[highlighted]:bg-hover">
              <Pencil className="h-3.5 w-3.5" /> Rename
            </Menu.Item>
            <Menu.Item onSelect={() => setConfirmDelete(true)}
                       className="flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-[13px] text-danger outline-none data-[highlighted]:bg-danger-soft">
              <Trash2 className="h-3.5 w-3.5" /> Delete
            </Menu.Item>
          </Menu.Content>
        </Menu.Portal>
      </Menu.Root>
      <ConfirmDialog open={confirmDelete} onOpenChange={setConfirmDelete} title="Delete chat?"
                     description={`“${session.title}” will be permanently deleted.`} onConfirm={remove} />
    </div>
  );
}

/** The sidebar; on tablets it's a drawer over the page. */
export function Sidebar() {
  const { sessions, sessionsLoading, sessionQuery, setSessionQuery, setSidebarOpen, openSettings, showImported,
    setShowImported } = useApp();
  const anyImported = sessions.some((s) => s.imported);
  const pathname = usePathname();
  const router = useRouter();
  const activeId = pathname.startsWith("/c/") ? pathname.slice(3) : "";
  const groups: [string, SessionSummary[]][] = [];
  for (const s of sessions) {
    const day = relativeDay(s.updated_at);
    const last = groups[groups.length - 1];
    if (last && last[0] === day) last[1].push(s);
    else groups.push([day, [s]]);
  }
  const mod = modKey();

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-12 items-center justify-between px-3">
        <Link href="/" className="flex items-center gap-2 rounded-lg px-1 py-1" aria-label={`${PRODUCT} home`}>
          <LogoMark />
          <span className="text-[15px] font-semibold tracking-tight">{PRODUCT}</span>
        </Link>
        <Tooltip content={<span>Close sidebar <Kbd>{mod}</Kbd><Kbd>B</Kbd></span>}>
          <Button variant="ghost" size="icon-sm" aria-label="Close sidebar" data-testid="close-sidebar"
                  onClick={() => setSidebarOpen(false)}>
            <PanelLeftClose className="h-4 w-4" />
          </Button>
        </Tooltip>
      </div>

      <div className="space-y-2 px-3 pb-2">
        <ModelPicker />
        <Button variant="outline" className="w-full justify-between" data-testid="new-chat"
                onClick={() => { router.push("/"); window.dispatchEvent(new CustomEvent("quorum:new-chat")); }}>
          <span className="flex items-center gap-2"><SquarePen className="h-4 w-4" /> New chat</span>
          <span className="flex gap-0.5"><Kbd>{mod}</Kbd><Kbd>⇧</Kbd><Kbd>O</Kbd></span>
        </Button>
        <div className="relative flex items-center gap-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-faint" />
          <Input id="session-search" value={sessionQuery} onChange={(e) => setSessionQuery(e.target.value)}
                 placeholder="Search chats" aria-label="Search chats" className="h-8 pl-8 pr-8 text-[13px]" />
          {sessionQuery && (
            <button aria-label="Clear search" onClick={() => setSessionQuery("")}
                    className="absolute right-11 top-1/2 -translate-y-1/2 text-faint hover:text-fg">
              <X className="h-3.5 w-3.5" />
            </button>
          )}
          <Tooltip content={showImported ? "Hide chats imported from claude.ai" : "Show chats imported from claude.ai"}>
            <Button variant="ghost" size="icon-sm" data-testid="toggle-imported" aria-pressed={showImported}
                    aria-label={showImported ? "Hide imported chats" : "Show imported chats"}
                    onClick={() => setShowImported(!showImported)} className="h-8 w-8 shrink-0">
              {showImported ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
            </Button>
          </Tooltip>
        </div>
        {!showImported && (
          <p className="flex items-center gap-1 px-1 text-[11px] text-faint" data-testid="imported-hidden">
            <Download className="h-3 w-3" /> Imported claude.ai chats are hidden
          </p>
        )}
      </div>

      <nav aria-label="Chats" className="min-h-0 flex-1 overflow-y-auto px-2 pb-3" data-testid="session-list">
        {sessionsLoading ? (
          <div className="space-y-2 px-1 pt-2">{[0, 1, 2, 3].map((i) => <div key={i} className="skeleton h-7" />)}</div>
        ) : sessions.length === 0 ? (
          <p className="px-3 py-6 text-center text-[13px] text-muted">
            {sessionQuery ? `No chats match “${sessionQuery}”.` : anyImported || !showImported ? "No chats to show."
              : "No chats yet. Start one, open Meetings to transcribe a recording, or import your claude.ai chats in Settings."}
          </p>
        ) : (
          groups.map(([day, list]) => (
            <div key={day} className="mt-3 first:mt-1">
              <div className="px-2.5 pb-1 text-[11px] font-semibold uppercase tracking-wide text-faint">{day}</div>
              {list.map((s) => <SessionItem key={s.id} session={s} active={s.id === activeId} />)}
            </div>
          ))
        )}
      </nav>

      <div className="border-t border-border p-2">
        <Button variant="ghost" className="w-full justify-between" data-testid="open-settings"
                onClick={() => openSettings("general")}>
          <span className="flex items-center gap-2"><Settings className="h-4 w-4" /> Settings</span>
          <span className="flex gap-0.5"><Kbd>{mod}</Kbd><Kbd>,</Kbd></span>
        </Button>
      </div>
    </div>
  );
}
