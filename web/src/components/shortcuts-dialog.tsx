"use client";

/** The keyboard shortcuts list (⌘/). */
import { useApp } from "@/components/app-provider";
import { Dialog, Kbd } from "@/components/ui";
import { modKey } from "@/lib/format";

/** A dialog listing every keyboard shortcut. */
export function ShortcutsDialog() {
  const { shortcutsOpen, setShortcutsOpen } = useApp();
  const mod = modKey();
  const rows: [string, string[]][] = [
    ["New chat", [mod, "Shift", "O"]],
    ["Search everything", [mod, "K"]],
    ["Filter chats in the sidebar", [mod, "Shift", "F"]],
    ["Toggle sidebar", [mod, "B"]],
    ["Meetings", [mod, "Shift", "M"]],
    ["Settings", [mod, ","]],
    ["Toggle light / dark", [mod, "Shift", "L"]],
    ["Keyboard shortcuts", [mod, "/"]],
    ["Send message", ["Enter"]],
    ["New line", ["Shift", "Enter"]],
    ["Stop generating", ["Esc"]],
  ];
  return (
    <Dialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} title="Keyboard shortcuts">
      <ul className="divide-y divide-border">
        {rows.map(([label, keys]) => (
          <li key={label} className="flex items-center justify-between py-2 text-sm">
            <span>{label}</span>
            <span className="flex gap-1">{keys.map((k) => <Kbd key={k}>{k}</Kbd>)}</span>
          </li>
        ))}
      </ul>
    </Dialog>
  );
}
