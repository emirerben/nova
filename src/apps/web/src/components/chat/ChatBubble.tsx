"use client";

import type { ReactNode } from "react";

/**
 * Shared chat bubble (stock shadcn tokens, DESIGN.md §15 — Kria Design
 * System migration, Lane K): dark right-aligned user bubble, muted
 * left-aligned assistant bubble. Used by every bubble-thread surface —
 * CopilotDrawer's inline messages, EditProposalCard's ConversationThread,
 * and AskKriaPanel.
 */
export function ChatBubble({
  role,
  pending = false,
  children,
}: {
  role: "user" | "assistant";
  /** Reduced-opacity variant for an optimistic bubble not yet confirmed by the server. */
  pending?: boolean;
  children: ReactNode;
}) {
  const isUser = role === "user";
  return (
    <div
      className={[
        "whitespace-pre-line break-words rounded-lg px-3 py-2 text-sm leading-relaxed max-w-[85%]",
        isUser
          ? "ml-auto rounded-br-sm bg-primary text-primary-foreground"
          : "mr-auto rounded-bl-sm bg-muted text-foreground",
        pending ? "opacity-60" : "",
      ].join(" ")}
    >
      {children}
    </div>
  );
}
