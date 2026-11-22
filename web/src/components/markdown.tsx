"use client";

/**
 * Renders model answers and notes written in Markdown (tables, lists, links) safely, without raw HTML.
 */
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { clsx } from "@/lib/format";

/**
 * Markdown text as formatted HTML (GitHub-style tables and lists; links open in a new tab).
 * `streaming` shows a blinking cursor while an answer is being written.
 */
export function Markdown({ children, className, streaming }: { children: string; className?: string; streaming?: boolean }) {
  return (
    <div className={clsx("prose-chat", streaming && "caret", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}
        components={{ a: ({ ...props }) => <a {...props} target="_blank" rel="noreferrer" /> }}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
