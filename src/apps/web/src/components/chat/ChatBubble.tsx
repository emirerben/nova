"use client";

import type { ReactNode } from "react";

/**
 * Shared chat bubble, visual language copied from CopilotDrawer's inline
 * message bubbles (dark right-aligned user bubble, light left-aligned
 * assistant bubble). NOT imported by CopilotDrawer — that surface stays
 * byte-identical; this is a standalone primitive for other chat-shaped
 * surfaces (see EditProposalCard).
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
        "whitespace-pre-line break-words rounded-[18px] px-3.5 py-2.5 text-sm leading-5",
        isUser
          ? "ml-auto max-w-[85%] rounded-br-md bg-[#0c0c0e] text-white"
          : "mr-auto max-w-[85%] rounded-bl-md bg-zinc-100 text-[#0c0c0e]",
        pending ? "opacity-60" : "",
      ].join(" ")}
    >
      {children}
    </div>
  );
}
