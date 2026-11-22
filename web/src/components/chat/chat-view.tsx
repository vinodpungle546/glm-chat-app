"use client";

/**
 * The chat screen: the message list, the answer being streamed, privacy questions (send to cloud or stay
 * local), errors with retry, and the composer at the bottom.
 */
import { ArrowRight, Check, Copy, Cpu, Cloud, Download, FileAudio, Lock, RotateCcw, Sparkles, MessageSquareText } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { Composer, type ComposerSend } from "@/components/chat/composer";
import { LogoMark } from "@/components/logo";
import { Markdown } from "@/components/markdown";
import { ModelAvailability } from "@/components/model-availability";
import { Button, Callout, Progress, Spinner, Tooltip } from "@/components/ui";
import { api, ApiError, stream, type Message, type Project, type Session, type StreamEvent } from "@/lib/api";

/**
 * The answer being written right now: the question, the streamed text, status lines (tools, search),
 * notices, progress, an error or a confirm question, and the original request so it can be sent again
 * (retry, or after the user answers a question).
 */
interface Draft {
  user: string;
  content: string;
  model: string;
  statuses: string[];
  notices: string[];
  progress: { done: number; total: number; text: string } | null;
  error: { kind: string; message: string } | null;
  confirm: Extract<StreamEvent, { type: "confirm" }> | null;
  stopped: boolean;
  request: ComposerSend;
}

/** A small label saying which model wrote an answer, e.g. 'Gemma 4 E4B · local'. */
function ModelChip({ label }: { label?: string }) {
  if (!label) return null;
  const local = label.endsWith("· local");
  return (
    <div className="mb-1.5 flex items-center gap-1.5 text-xs text-muted" data-testid="message-model">
      {local ? <Cpu className="h-3 w-3" /> : <Cloud className="h-3 w-3" />} {label}
    </div>
  );
}

/** Copies an answer to the clipboard and briefly shows a tick. */
function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <Tooltip content={done ? "Copied" : "Copy"}>
      <Button variant="ghost" size="icon-sm" aria-label="Copy answer"
              onClick={() => navigator.clipboard.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); })}>
        {done ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      </Button>
    </Tooltip>
  );
}

/** One message the user sent, right-aligned. */
function UserBubble({ content }: { content: string }) {
  return (
    <div className="flex justify-end" data-testid="user-message">
      <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-user-bubble px-4 py-2.5 text-[15px] leading-6">
        {content}
      </div>
    </div>
  );
}

/** 'Used N facts from memory' under an answer; click to see which facts. */
function MemoryUsed({ facts }: { facts?: string[] }) {
  const [open, setOpen] = useState(false);
  if (!facts?.length) return null;
  return (
    <div className="mb-1.5 text-xs text-muted" data-testid="memory-used">
      <button className="hover:text-fg" onClick={() => setOpen(!open)} aria-expanded={open}>
        Used {facts.length} fact{facts.length === 1 ? "" : "s"} from memory {open ? "▴" : "▾"}</button>
      {open && <ul className="mt-1 list-disc space-y-0.5 pl-4">{facts.map((f, i) => <li key={i}>{f}</li>)}</ul>}
    </div>
  );
}

/** One answer: the model chip, the memory facts used, the Markdown text and a copy button. */
function AssistantMessage({ message, imported }: { message: Message; imported?: boolean }) {
  return (
    <div className="group" data-testid="assistant-message">
      {imported ? <div className="mb-1.5 text-xs text-muted">Claude · claude.ai</div> : <ModelChip label={message.model} />}
      <MemoryUsed facts={message.memory_used} />
      <Markdown>{message.content}</Markdown>
      <div className="mt-1 opacity-0 transition-opacity group-hover:opacity-100"><CopyButton text={message.content} /></div>
    </div>
  );
}

/** What a new chat shows before the first message: a greeting and shortcuts to common tasks. */
function EmptyChat() {
  const router = useRouter();
  const ideas = [
    { icon: <FileAudio className="h-4 w-4" />, title: "Transcribe a recording", text: "Upload a meeting video or pick one from your watch folder.", href: "/meetings" },
    { icon: <Sparkles className="h-4 w-4" />, title: "Summarize a meeting", text: "Minutes with decisions, action items and owners.", href: "/meetings" },
    { icon: <MessageSquareText className="h-4 w-4" />, title: "Ask about a meeting", text: "Attach a transcript with the microphone button below.", href: "" },
  ];
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col items-center justify-center px-4 pb-8 text-center">
      <LogoMark className="mb-4 h-10 w-10" />
      <h1 className="text-2xl font-semibold tracking-tight">What can I help with?</h1>
      <p className="mt-1.5 text-sm text-muted">Chat, transcribe meetings and get minutes — on this Mac or in the cloud.</p>
      <div className="mt-8 grid w-full gap-2.5 sm:grid-cols-3">
        {ideas.map((idea) => (
          <button key={idea.title} onClick={() => idea.href && router.push(idea.href)}
                  className="rounded-xl border border-border bg-surface p-3.5 text-left transition-colors hover:bg-hover">
            <div className="mb-2 text-accent">{idea.icon}</div>
            <div className="text-[13px] font-medium">{idea.title}</div>
            <div className="mt-0.5 text-xs text-muted">{idea.text}</div>
          </button>
        ))}
      </div>
    </div>
  );
}

/**
 * The chat screen for one session (or a new chat when `sessionId` is missing). Sending streams the answer
 * from the API (SSE); the first message creates the session and updates the URL without reloading.
 */
export function ChatView({ sessionId }: { sessionId?: string }) {
  const { selectedModel, upsertSession, refreshSessions, models, selectModel, settingsTab, prefs } = useApp();
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(!!sessionId);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [project, setProject] = useState<Project | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const sessionRef = useRef<Session | null>(null);
  useEffect(() => { sessionRef.current = session; }, [session]);
  const router = useRouter();

  // Load the chat (or reset for a new one)
  useEffect(() => {
    if (!sessionId) {
      setSession(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    api.get<Session>(`/api/sessions/${sessionId}`)
      .then((s) => { if (!cancelled) { setSession(s); setLoadError(null); } })
      .catch((err: ApiError) => { if (!cancelled) setLoadError(err.status === 404 ? "This chat no longer exists." : err.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [sessionId]);

  useEffect(() => {
    const reset = () => {
      abortRef.current?.abort();
      setSession(null);
      setDraft(null);
      setStreaming(false);
    };
    window.addEventListener("quorum:new-chat", reset);
    return () => window.removeEventListener("quorum:new-chat", reset);
  }, []);

  // The active project's skills for the "/" menu; reloaded after Settings closes.
  const settingsOpen = settingsTab !== null;
  useEffect(() => {
    if (settingsOpen) return;
    api.get<Project[]>("/api/projects").then((list) => {
      const active = list.find((p) => p.active) ?? list[0];
      if (active) api.get<Project>(`/api/projects/${active.id}`).then(setProject).catch(() => undefined);
    }).catch(() => undefined);
  }, [settingsOpen, prefs?.active_project]);

  // Keep the newest text in view while streaming
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // Follow the streaming answer only if the user is already near the bottom (don't yank them back up).
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
    if (nearBottom || !streaming) el.scrollTo({ top: el.scrollHeight });
  }, [draft, session?.messages.length, streaming]);

  // The first message of a new chat creates the session. The URL is changed with history.replaceState (not the
  // router), so the page doesn't remount and the streaming answer isn't lost.
  const ensureSession = useCallback(async (): Promise<Session> => {
    if (sessionRef.current) return sessionRef.current;
    const created = await api.post<Session>("/api/sessions", { model_id: selectedModel?.id ?? "" });
    setSession(created);
    sessionRef.current = created;
    // Update the URL without remounting this view (Next.js syncs with the history API).
    window.history.replaceState(null, "", `/c/${created.id}`);
    return created;
  }, [selectedModel]);

  // Sends one message and streams the answer. `confirm` answers a question the API asked (send to cloud / stay
  // local / run a long job): the same request is simply sent again with the answer.
  const run = useCallback(async (request: ComposerSend, confirm?: string) => {
    let current: Session;
    try {
      current = await ensureSession();
    } catch (err) {
      toast.error("Couldn't start a chat", { description: (err as Error).message });
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    setStreaming(true);
    setDraft({ user: request.text, content: "", model: "", statuses: [], notices: [], progress: null, error: null,
               confirm: null, stopped: false, request });
    const update = (fn: (d: Draft) => Draft) => setDraft((d) => (d ? fn(d) : d));
    let userText = request.text;
    try {
      await stream<StreamEvent>(`/api/sessions/${current.id}/messages`, {
        content: request.text, model_id: selectedModel?.id ?? "", confirm: confirm ?? null,
        skill: request.skill, attached_now: request.attachedNow,
      }, (event) => {
        switch (event.type) {
          case "start":
            userText = event.user?.content ?? userText;
            update((d) => ({ ...d, model: event.model, user: userText }));
            break;
          case "delta": update((d) => ({ ...d, content: d.content + event.text, progress: null })); break;
          // 'reset': the text streamed so far was a tool request (JSON), not the answer: clear it.
          case "reset": update((d) => ({ ...d, content: "" })); break;
          case "status": update((d) => ({ ...d, statuses: [...d.statuses, event.text] })); break;
          case "notice":
            if (event.kind === "privacy") toast.info("Memory and cloud models", { description: event.text.replace(/^ℹ️\s*/, ""), duration: 15000 });
            else update((d) => ({ ...d, notices: [...d.notices, event.text] }));
            break;
          case "progress": update((d) => ({ ...d, progress: event })); break;
          case "memory": toast.success("Memory updated", { description: event.added.join(" · ") }); break;
          // The API stopped to ask the user something; the draft shows the question with its buttons.
          case "confirm": update((d) => ({ ...d, confirm: event })); break;
          case "error": update((d) => ({ ...d, error: { kind: event.kind, message: event.message } })); break;
          case "done": {
            const msg = event.message;
            setSession((s) => (s && msg ? { ...s, messages: [...s.messages, { role: "user", content: userText }, msg] } : s));
            if (event.session) upsertSession(event.session);
            setDraft(null);
            break;
          }
        }
      }, controller.signal);
    } catch (err) {
      if ((err as Error).name === "AbortError") update((d) => ({ ...d, stopped: true }));
      else update((d) => ({ ...d, error: { kind: (err as ApiError).kind ?? "network", message: (err as Error).message } }));
    } finally {
      setStreaming(false);
      abortRef.current = null;
      refreshSessions();
      // Reload so the stored version (with the exact user text) replaces optimistic state.
      api.get<Session>(`/api/sessions/${current.id}`).then((s) => {
        setSession(s);
        setDraft((d) => (d && !d.error && !d.confirm && !d.stopped ? null : d));
      }).catch(() => undefined);
    }
  }, [ensureSession, selectedModel, upsertSession, refreshSessions]);

  // A question handed over by another page (the live transcript's "Actions so far"): sent once the chat loads.
  useEffect(() => {
    if (!session || streaming) return;
    const key = `quorum:ask:${session.id}`;
    let text: string | null = null;
    try { text = window.sessionStorage.getItem(key); window.sessionStorage.removeItem(key); } catch { text = null; }
    if (text) run({ text, skill: null, attachedNow: [] });
  }, [session, streaming, run]);

  const stop = useCallback(() => abortRef.current?.abort(), []);

  const attachFile = async (file: File): Promise<string | null> => {
    try {
      const s = await ensureSession();
      const form = new FormData();
      form.append("file", file);
      const updated = await api.upload<Session>(`/api/sessions/${s.id}/attachments`, form);
      setSession((cur) => (cur ? { ...cur, attachments: updated.attachments } : updated));
      return file.name;
    } catch (err) {
      toast.error(`Couldn't attach ${file.name}`, { description: (err as Error).message });
      return null;
    }
  };

  const attachTranscript = async (id: string) => {
    try {
      const s = await ensureSession();
      const updated = await api.post<Session>(`/api/sessions/${s.id}/transcripts`, { transcript_id: id });
      setSession((cur) => (cur ? { ...cur, attachments: updated.attachments } : updated));
      toast.success("Transcript attached");
    } catch (err) {
      toast.error("Couldn't attach the transcript", { description: (err as Error).message });
    }
  };

  const detach = async (name: string) => {
    if (!session) return;
    try {
      const updated = await api.del<Session>(`/api/sessions/${session.id}/attachments/${encodeURIComponent(name)}`);
      setSession({ ...session, attachments: updated.attachments });
    } catch (err) {
      toast.error("Couldn't remove the attachment", { description: (err as Error).message });
    }
  };

  if (loadError) {
    return (
      <div className="mx-auto max-w-lg p-8">
        <Callout tone="danger" title="Couldn't open this chat" action={<Button size="sm" onClick={() => router.push("/")}>New chat</Button>}>
          {loadError}
        </Callout>
      </div>
    );
  }

  const continueChat = async () => {
    if (!session) return;
    try {
      const created = await api.post<Session>(`/api/sessions/${session.id}/continue`, { model_id: selectedModel?.id ?? "" });
      await refreshSessions();
      toast.success("Continuing in a new chat", { description: `With ${selectedModel?.label ?? "the selected model"}. The imported chat stays unchanged.` });
      router.push(`/c/${created.id}`);
    } catch (err) {
      toast.error("Couldn't continue the chat", { description: (err as Error).message });
    }
  };

  const messages = session?.messages ?? [];
  const readOnly = !!session?.read_only;
  const showEmpty = !loading && messages.length === 0 && !draft;
  const disabledReason = selectedModel && selectedModel.status !== "ready"
    ? `${selectedModel.label} is unavailable — see above` : null;
  const firstReadyLocal = models?.models.find((m) => m.group === "local" && m.status === "ready");

  return (
    <div className="flex h-full flex-col">
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto" data-testid="chat-scroll">
        {loading ? (
          <div className="mx-auto max-w-3xl space-y-4 px-4 py-8">
            <div className="skeleton ml-auto h-10 w-1/2" /><div className="skeleton h-24" /><div className="skeleton ml-auto h-10 w-1/3" />
          </div>
        ) : showEmpty ? (
          <div className="flex h-full flex-col"><EmptyChat /></div>
        ) : (
          <div className="mx-auto max-w-3xl space-y-6 px-4 py-6 sm:py-8" data-testid="messages">
            {session?.confidential && (
              <div className="flex items-center justify-center gap-1.5 text-xs text-muted">
                <Lock className="h-3 w-3" /> Confidential chat — answered by a local model on this Mac
              </div>
            )}
            {session?.imported && (
              <div className="flex flex-wrap items-center justify-center gap-1.5 text-xs text-muted" data-testid="imported-banner">
                <Download className="h-3 w-3" /> Imported from claude.ai{session.imported.imported_at ? ` on ${session.imported.imported_at.slice(0, 10)}` : ""}
                {session.created_at ? ` · started ${session.created_at.slice(0, 10)}` : ""} · read-only
              </div>
            )}
            {session?.continued_from && (
              <div className="flex items-center justify-center gap-1.5 text-xs text-muted" data-testid="continued-banner">
                Continued from {session.continued_from.source === "claude.ai" ? "an imported claude.ai chat" : "an earlier chat"}:
                <a className="text-accent hover:underline" href={`/c/${session.continued_from.id}`}>{session.continued_from.title}</a>
              </div>
            )}
            {messages.map((m, i) => (m.role === "user" ? <UserBubble key={i} content={m.content} /> : <AssistantMessage key={i} message={m} imported={!!session?.imported} />))}
            {draft && (
              <>
                <UserBubble content={draft.user} />
                <div data-testid="assistant-draft" aria-live="polite">
                  <ModelChip label={draft.model} />
                  {draft.statuses.map((s, i) => (
                    <div key={i} className="mb-1.5 flex items-center gap-1.5 text-xs text-muted">
                      {i === draft.statuses.length - 1 && streaming && !draft.content ? <Spinner className="h-3 w-3" /> : <Check className="h-3 w-3" />} {s}
                    </div>
                  ))}
                  {draft.notices.map((n, i) => <Callout key={i} tone="info" className="mb-3">{n.replace(/^ℹ️\s*/, "")}</Callout>)}
                  {draft.progress && (
                    <div className="mb-3 space-y-1.5" data-testid="long-progress">
                      <div className="text-xs text-muted">{draft.progress.text}</div>
                      <Progress value={(draft.progress.done / Math.max(1, draft.progress.total)) * 100} />
                    </div>
                  )}
                  {draft.content ? <Markdown streaming={streaming}>{draft.content}</Markdown>
                    : streaming && !draft.confirm && !draft.progress && (
                      <div className="flex items-center gap-2 text-sm text-muted"><Spinner /> {draft.model ? "Thinking…" : "Starting…"}</div>
                    )}
                  {draft.stopped && <p className="mt-2 text-xs text-muted">Stopped. This reply wasn’t saved.</p>}
                  {draft.confirm?.kind === "sensitive" && (
                    <Callout tone="warning" title="This looks confidential" className="mt-1" role="alertdialog">
                      <span>{draft.confirm.reasons.join("; ")}. Answer it on {draft.confirm.local_model ?? "a local model"} (nothing leaves this Mac), or send it to {draft.confirm.model ?? "the cloud"}{draft.confirm.provider ? ` (${draft.confirm.provider})` : ""} anyway?
                        {draft.confirm.connectors?.length ? <> Remote connectors ({draft.confirm.connectors.join(", ")}) may also receive it.</> : null}</span>
                      <div className="mt-2.5 flex flex-wrap gap-2">
                        <Button size="sm" variant="primary" data-testid="use-local" disabled={!draft.confirm.local_model}
                                onClick={() => run(draft.request, "local")}><Lock className="h-3.5 w-3.5" /> Use local model</Button>
                        <Button size="sm" variant="secondary" data-testid="send-cloud" onClick={() => run(draft.request, "cloud")}>
                          <Cloud className="h-3.5 w-3.5" /> Send to {draft.confirm.model ?? "cloud"} anyway</Button>
                        <Button size="sm" variant="ghost" onClick={() => setDraft(null)}>Cancel</Button>
                      </div>
                    </Callout>
                  )}
                  {draft.confirm?.kind === "long" && (
                    <Callout tone="info" title="This will take a while" className="mt-1" role="alertdialog">
                      <span>{draft.confirm.notice.replace(/^ℹ️\s*/, "")}</span>
                      <div className="mt-2.5 flex gap-2">
                        <Button size="sm" variant="primary" data-testid="run-long" onClick={() => run(draft.request, "run")}>Run</Button>
                        <Button size="sm" variant="ghost" onClick={() => setDraft(null)}>Cancel</Button>
                      </div>
                    </Callout>
                  )}
                  {draft.error && (
                    <Callout tone="danger" className="mt-1" data-testid="chat-error"
                      title={draft.error.kind === "local_model" ? "The local model couldn't answer"
                        : draft.error.kind === "cloud" ? "The cloud model couldn't answer"
                        : draft.error.kind === "network" ? "Connection problem" : "Something went wrong"}>
                      <span>{draft.error.message}</span>
                      <div className="mt-2.5 flex flex-wrap gap-2">
                        <Button size="sm" variant="secondary" data-testid="retry" onClick={() => run(draft.request)}>
                          <RotateCcw className="h-3.5 w-3.5" /> Retry</Button>
                        {draft.error.kind === "local_model" && (
                          selectedModel?.group === "local" && models?.models.find((m) => m.group === "cloud") ? (
                            <Button size="sm" variant="ghost" onClick={() => {
                              const cloud = models!.models.find((m) => m.group === "cloud")!;
                              selectModel(cloud.id);
                              toast(`Switched to ${cloud.label}`);
                            }}>Switch to a cloud model</Button>
                          ) : firstReadyLocal && (
                            <Button size="sm" variant="ghost" onClick={() => selectModel(firstReadyLocal.id)}>Use {firstReadyLocal.label}</Button>
                          )
                        )}
                      </div>
                    </Callout>
                  )}
                </div>
              </>
            )}
          </div>
        )}
      </div>
      {readOnly ? (
        <div className="shrink-0 border-t border-border bg-surface-2/60 px-4 py-3" data-testid="read-only-bar">
          <div className="mx-auto flex max-w-3xl flex-wrap items-center justify-between gap-3">
            <p className="text-[13px] text-muted">This chat was imported from claude.ai and can’t be changed. Continue it in a new chat with any model.</p>
            <Button variant="primary" onClick={continueChat} data-testid="continue-chat" disabled={!selectedModel}>
              Continue with {selectedModel?.label ?? "a model"} <ArrowRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      ) : (
      <div className="shrink-0 space-y-2">
        {selectedModel && selectedModel.status !== "ready" && (
          <div className="mx-auto w-full max-w-3xl px-3 sm:px-4"><ModelAvailability model={selectedModel} compact /></div>
        )}
        <Composer
          onSend={(req) => run(req)} onStop={stop} streaming={streaming} disabledReason={disabledReason}
          attachments={session?.attachments ?? []} onAttachFile={attachFile} onAttachTranscript={attachTranscript}
          onDetach={detach} skills={project?.skills ?? []} autoFocus
        />
      </div>
      )}
    </div>
  );
}
