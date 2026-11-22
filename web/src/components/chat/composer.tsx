"use client";

/**
 * The message box at the bottom of a chat: text input, skill picker ('/'), file and meeting attachments,
 * send and stop.
 */
import * as Popover from "@radix-ui/react-popover";
import { ArrowUp, FileText, Mic, Paperclip, Puzzle, Square, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Badge, Button, Spinner, Tooltip } from "@/components/ui";
import { api, type Attachment, type Skill, type TranscriptMeta } from "@/lib/api";
import { clsx, DOCUMENT_EXTENSIONS, extension, fmtDuration, MEDIA_EXTENSIONS } from "@/lib/format";

/**
 * What the composer sends: the text, a skill chosen for this message, and the names of files attached with
 * this message.
 */
export interface ComposerSend {
  text: string;
  skill: string | null;
  attachedNow: string[];
}

/**
 * The message input. Enter sends, Shift+Enter adds a line; while an answer is streaming the send button
 * becomes Stop. `disabledReason` explains why sending is off (e.g. a read-only imported chat).
 */
export function Composer({ onSend, onStop, streaming, disabledReason, attachments, onAttachFile, onAttachTranscript,
  onDetach, skills, autoFocus }: {
  onSend: (msg: ComposerSend) => void;
  onStop: () => void;
  streaming: boolean;
  disabledReason: string | null;
  attachments: Attachment[];
  onAttachFile: (file: File) => Promise<string | null>;
  onAttachTranscript: (id: string) => Promise<void>;
  onDetach: (name: string) => void;
  skills: Skill[];
  autoFocus?: boolean;
}) {
  const [text, setText] = useState("");
  const [skill, setSkill] = useState<string | null>(null);
  const [attachedNow, setAttachedNow] = useState<string[]>([]);
  const [uploading, setUploading] = useState(false);
  const [transcripts, setTranscripts] = useState<TranscriptMeta[] | null>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const showSkills = text.startsWith("/") && !text.includes(" ") && skills.length > 0;
  const matching = skills.filter((s) => s.name.toLowerCase().includes(text.slice(1).toLowerCase()));

  useEffect(() => {
    if (autoFocus) textRef.current?.focus();
  }, [autoFocus]);

  useEffect(() => {
    const el = textRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [text]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && streaming) onStop();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [streaming, onStop]);

  const submit = () => {
    if (streaming || disabledReason) return;
    const value = text.trim();
    if (!value && !attachedNow.length) return;
    onSend({ text: value || "Please analyse the attached file(s) and summarise them.", skill, attachedNow });
    setText("");
    setSkill(null);
    setAttachedNow([]);
  };

  const pickFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    setUploading(true);
    for (const file of Array.from(files)) {
      const ext = extension(file.name);
      if (MEDIA_EXTENSIONS.includes(ext)) {
        toast.error(`${file.name} is a recording`, { description: "Transcribe recordings in Meetings, then attach the transcript here." });
        continue;
      }
      if (!DOCUMENT_EXTENSIONS.includes(ext)) {
        toast.error(`Unsupported file type: .${ext || "?"}`, { description: `Attach ${DOCUMENT_EXTENSIONS.join(", ")} files.` });
        continue;
      }
      const name = await onAttachFile(file);
      if (name) setAttachedNow((list) => [...list.filter((n) => n !== name), name]);
    }
    setUploading(false);
    if (fileRef.current) fileRef.current.value = "";
  };

  return (
    <div className="mx-auto w-full max-w-3xl px-3 pb-3 sm:px-4 sm:pb-4">
      <div
        className={clsx("relative rounded-2xl border border-border-strong bg-surface shadow-card transition-colors",
          "focus-within:border-accent/60")}
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => { e.preventDefault(); pickFiles(e.dataTransfer.files); }}
      >
        {showSkills && (
          <div className="absolute bottom-full left-0 mb-2 w-72 rounded-xl border border-border bg-surface p-1 shadow-card" role="listbox" aria-label="Skills">
            {matching.length === 0 ? <p className="px-2.5 py-2 text-xs text-muted">No skill matches.</p> : matching.map((s) => (
              <button key={s.name} role="option" aria-selected={false}
                onClick={() => { setSkill(s.name); setText(""); textRef.current?.focus(); }}
                className="block w-full rounded-lg px-2.5 py-1.5 text-left hover:bg-hover">
                <div className="text-[13px] font-medium">/{s.name}</div>
                {s.description && <div className="truncate text-xs text-muted">{s.description}</div>}
              </button>
            ))}
          </div>
        )}
        {(attachments.length > 0 || skill) && (
          <div className="flex flex-wrap gap-1.5 px-3 pt-3" data-testid="composer-chips">
            {skill && (
              <Badge tone="accent" className="py-1">
                <Puzzle className="h-3 w-3" /> {skill}
                <button aria-label="Remove skill" onClick={() => setSkill(null)}><X className="h-3 w-3" /></button>
              </Badge>
            )}
            {attachments.map((a) => (
              <Badge key={a.name} className="max-w-full py-1">
                {a.kind === "transcript" ? <Mic className="h-3 w-3" /> : <FileText className="h-3 w-3" />}
                {a.live && <span className="rounded bg-danger/15 px-1 text-[10px] font-semibold uppercase text-danger" title="Updated with the meeting so far before each message" data-testid="live-attachment">live</span>}
                <span className="max-w-56 truncate">{a.name}</span>
                <button aria-label={`Remove ${a.name}`} onClick={() => onDetach(a.name)}><X className="h-3 w-3" /></button>
              </Badge>
            ))}
          </div>
        )}
        <textarea
          ref={textRef}
          value={text}
          rows={1}
          data-testid="composer-input"
          aria-label="Message"
          placeholder={disabledReason ?? (skills.length ? "Message Quorum… (type / for skills)" : "Message Quorum…")}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              if (showSkills && matching[0]) { setSkill(matching[0].name); setText(""); return; }
              submit();
            }
          }}
          className="block max-h-60 w-full resize-none bg-transparent px-4 pb-1 pt-3.5 text-[15px] leading-6 text-fg placeholder:text-faint focus:outline-none focus-visible:outline-none"
        />
        <div className="flex items-center justify-between px-2.5 pb-2.5 pt-1">
          <div className="flex items-center gap-0.5">
            <input ref={fileRef} type="file" multiple hidden data-testid="composer-file"
                   accept={DOCUMENT_EXTENSIONS.map((e) => `.${e}`).join(",")} onChange={(e) => pickFiles(e.target.files)} />
            <Tooltip content="Attach documents">
              <Button variant="ghost" size="icon" aria-label="Attach documents" onClick={() => fileRef.current?.click()} disabled={uploading}>
                {uploading ? <Spinner /> : <Paperclip className="h-[18px] w-[18px]" />}
              </Button>
            </Tooltip>
            <Popover.Root onOpenChange={(open) => {
              if (open) api.get<TranscriptMeta[]>("/api/meetings/transcripts").then(setTranscripts).catch((err) => {
                setTranscripts([]);
                toast.error("Meetings unavailable", { description: (err as Error).message });
              });
            }}>
              <Tooltip content="Attach a meeting transcript">
                <Popover.Trigger asChild>
                  <Button variant="ghost" size="icon" aria-label="Attach a meeting transcript"><Mic className="h-[18px] w-[18px]" /></Button>
                </Popover.Trigger>
              </Tooltip>
              <Popover.Portal>
                <Popover.Content side="top" align="start" sideOffset={8}
                  className="z-50 max-h-80 w-80 overflow-y-auto rounded-xl border border-border bg-surface p-1 shadow-card">
                  <p className="px-2.5 pb-1 pt-1.5 text-[11px] font-semibold uppercase tracking-wide text-faint">Meeting transcripts</p>
                  {transcripts === null ? <div className="p-3"><Spinner /></div> : transcripts.length === 0 ? (
                    <p className="px-2.5 py-2 text-[13px] text-muted">No transcripts yet. Transcribe one in Meetings.</p>
                  ) : transcripts.map((t) => (
                    <Popover.Close key={t.id} asChild>
                      <button onClick={() => onAttachTranscript(t.id)} className="block w-full rounded-lg px-2.5 py-1.5 text-left hover:bg-hover">
                        <div className="truncate text-[13px] font-medium">{t.title}</div>
                        <div className="text-xs text-muted">{fmtDuration(t.duration)} · {t.speakers.length} speaker{t.speakers.length === 1 ? "" : "s"}</div>
                      </button>
                    </Popover.Close>
                  ))}
                </Popover.Content>
              </Popover.Portal>
            </Popover.Root>
          </div>
          {streaming ? (
            <Tooltip content="Stop (Esc)">
              <Button variant="primary" size="icon" aria-label="Stop generating" data-testid="stop" onClick={onStop} className="rounded-full">
                <Square className="h-3.5 w-3.5 fill-current" />
              </Button>
            </Tooltip>
          ) : (
            <Button variant="primary" size="icon" aria-label="Send message" data-testid="send" onClick={submit}
                    disabled={!!disabledReason || (!text.trim() && !attachedNow.length)} className="rounded-full">
              <ArrowUp className="h-[18px] w-[18px]" />
            </Button>
          )}
        </div>
      </div>
      <p className="mt-2 text-center text-[11px] text-faint">
        Local models keep everything on this Mac. Answers can be wrong — check important details.
      </p>
    </div>
  );
}
