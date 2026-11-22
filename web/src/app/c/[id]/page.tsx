"use client";

/** Route '/c/<id>': an existing chat, opened from the sidebar. */
import { useParams } from "next/navigation";
import { ChatView } from "@/components/chat/chat-view";

/** Reads the chat id from the URL and shows that chat. */
export default function ChatPage() {
  const { id } = useParams<{ id: string }>();
  return <ChatView key={id} sessionId={id} />;
}
