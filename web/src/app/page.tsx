/**
 * Route '/': a new chat. ChatView without a session id shows the empty chat; the first message creates the
 * session.
 */
import { ChatView } from "@/components/chat/chat-view";

/** The home page: an empty chat. */
export default function Home() {
  return <ChatView key="new" />;
}
