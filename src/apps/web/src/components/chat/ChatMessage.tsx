"use client";

import type { HTMLAttributes, ReactNode } from "react";

import { cn } from "@/lib/cn";

/**
 * Creator-facing message treatment adapted from AICSS Text Response.
 * Source: https://www.aicss.dev/components/text-response (MIT, AICSS 2026).
 *
 * Assistant responses are deliberately unboxed. The editorial variant keeps
 * interview surfaces left-aligned and prominent without turning them into a
 * conventional bubble transcript.
 */
export interface ChatMessageProps extends Omit<HTMLAttributes<HTMLDivElement>, "role"> {
  role: "user" | "assistant";
  /** Reduced-opacity treatment for an optimistic message awaiting the server. */
  pending?: boolean;
  /** Disable entrance motion for historical turns restored with a transcript. */
  animate?: boolean;
  presentation?: "conversation" | "editorial";
  children: ReactNode;
}

export function ChatMessage({
  role,
  pending = false,
  animate = true,
  presentation = "conversation",
  className,
  children,
  ...props
}: ChatMessageProps) {
  const isUser = role === "user";

  if (presentation === "editorial") {
    return (
      <div
        className={cn(
          "max-w-prose whitespace-pre-line [overflow-wrap:anywhere]",
          animate && "motion-safe:animate-chat-message-in motion-reduce:animate-chat-fade-in",
          isUser
            ? "border-l-2 border-primary pl-3 text-sm italic text-muted-foreground"
            : "font-display text-xl leading-snug text-foreground",
          pending && "opacity-60",
          className,
        )}
        {...props}
      >
        {children}
      </div>
    );
  }

  return (
    <div
      className={cn(
        "whitespace-pre-line [overflow-wrap:anywhere]",
        animate && "motion-safe:animate-chat-message-in motion-reduce:animate-chat-fade-in",
        isUser
          ? "ml-auto max-w-[85%] rounded-[18px] rounded-br-[6px] bg-primary px-3 py-2 text-sm leading-relaxed text-primary-foreground"
          : "mr-auto w-full max-w-prose text-sm leading-5 text-foreground",
        pending && "opacity-60",
        className,
      )}
      {...props}
    >
      {children}
    </div>
  );
}
