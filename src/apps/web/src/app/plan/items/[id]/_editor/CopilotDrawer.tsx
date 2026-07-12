"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type {
  CopilotMessage,
  QueuedCopilotMessage,
} from "@/lib/edit-copilot/useEditCopilot";
import type { EditorLayoutMode } from "./useEditorLayoutMode";

const STARTERS = [
  "Make the hook punchier",
  "Smaller, more elegant text",
  "Tighten the cuts",
];

const MAX_CHARS = 500;

function useElapsed(active: boolean): number {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!active) {
      setElapsed(0);
      return;
    }
    const started = Date.now();
    const id = window.setInterval(() => setElapsed(Date.now() - started), 250);
    return () => window.clearInterval(id);
  }, [active]);
  return elapsed;
}

function useKeyboardOffset(active: boolean): number {
  const [offset, setOffset] = useState(0);
  useEffect(() => {
    if (!active || typeof window === "undefined" || !window.visualViewport) return;
    const viewport = window.visualViewport;
    const update = () => {
      const hidden = Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop);
      setOffset(hidden);
    };
    update();
    viewport.addEventListener("resize", update);
    viewport.addEventListener("scroll", update);
    return () => {
      viewport.removeEventListener("resize", update);
      viewport.removeEventListener("scroll", update);
    };
  }, [active]);
  return offset;
}

function parseApplied(summary: string): { label: string; value: string } {
  const [label, rest] = summary.split(/:\s*/, 2);
  return { label: label || "Change", value: rest || summary };
}

function latestAssistantWithChanges(messages: CopilotMessage[]): CopilotMessage | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const msg = messages[i];
    if (msg.role === "assistant" && (msg.applied?.length || msg.rejected?.length)) {
      return msg;
    }
  }
  return null;
}

export default function CopilotDrawer({
  layoutMode,
  open = true,
  messages,
  sending,
  queued,
  error,
  restoredInput,
  suggestions,
  historyVersion,
  canUndo,
  onSend,
  onCancelQueued,
  onEditQueued,
  onStop,
  onUndo,
  onClose,
  onClearRestoredInput,
}: {
  layoutMode: EditorLayoutMode;
  open?: boolean;
  messages: CopilotMessage[];
  sending: boolean;
  queued: QueuedCopilotMessage | null;
  error: string | null;
  restoredInput: string;
  suggestions: string[];
  historyVersion: number;
  canUndo: boolean;
  onSend: (text: string) => void;
  onCancelQueued: () => void;
  onEditQueued: (text: string) => void;
  onStop: () => void;
  onUndo: () => void;
  onClose: () => void;
  onClearRestoredInput: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const threadRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const elapsed = useElapsed(sending);
  const keyboardOffset = useKeyboardOffset(layoutMode === "light" && open);
  const latestChanged = useMemo(() => latestAssistantWithChanges(messages), [messages]);
  const starterVisible = messages.length === 0;
  const activeSuggestions = suggestions.length > 0 ? suggestions : starterVisible ? STARTERS : [];

  useEffect(() => {
    if (!restoredInput) return;
    setDraft(restoredInput.slice(0, MAX_CHARS));
    onClearRestoredInput();
  }, [onClearRestoredInput, restoredInput]);

  useEffect(() => {
    const el = threadRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages, sending, queued, error]);

  if (layoutMode === "light" && !open) return null;

  const submit = () => {
    const text = draft.trim();
    if (!text) return;
    if (sending && queued) onEditQueued(text);
    else onSend(text);
    setDraft("");
  };

  const rootClass =
    layoutMode === "full"
      ? "flex h-full w-[360px] flex-col border-r border-zinc-200 bg-white"
      : layoutMode === "overlay"
        ? "flex h-[220px] w-full flex-col rounded-xl border border-zinc-200 bg-white shadow-[0_18px_48px_rgba(12,12,14,0.18)]"
        : "fixed inset-x-0 bottom-0 z-[95] flex max-h-[74dvh] min-h-[360px] flex-col rounded-t-2xl border-t border-zinc-200 bg-white shadow-[0_-18px_48px_rgba(12,12,14,0.2)]";

  return (
    <section
      data-testid={`copilot-${layoutMode}`}
      aria-label="Nova editor copilot"
      className={rootClass}
      style={layoutMode === "light" ? { paddingBottom: keyboardOffset } : undefined}
    >
      {layoutMode === "light" && (
        <div aria-hidden className="flex justify-center py-2 touch-none">
          <span className="h-1 w-10 rounded-full bg-zinc-300" />
        </div>
      )}
      <div className="flex flex-none items-center justify-between px-5 pb-3 pt-4">
        <h2 className="font-display text-[18px] font-medium text-[#0c0c0e]">Nova</h2>
        <button
          type="button"
          aria-label="Close Nova"
          onClick={onClose}
          className="flex h-11 w-11 items-center justify-center rounded-lg text-[13px] text-[#71717a] hover:bg-zinc-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
        >
          ✕
        </button>
      </div>

      <div
        ref={threadRef}
        className="min-h-0 flex-1 space-y-3 overflow-y-auto px-5 pb-3"
      >
        {starterVisible && (
          <div className="mr-auto max-w-[85%] rounded-[18px] rounded-bl-md bg-zinc-100 px-3.5 py-2.5 text-[13.5px] leading-5 text-[#0c0c0e]">
            <p>What should we change?</p>
            <p className="mt-1 text-[#3f3f46]">
              I can rewrite your hook, restyle text, and tighten or reorder cuts.
              Everything previews instantly — nothing renders until you save.
            </p>
          </div>
        )}

        {messages.map((message) => {
          const isUser = message.role === "user";
          const chips = [...(message.applied ?? []), ...(message.rejected ?? [])];
          const collapsed = chips.length > 3 && !expanded[message.id];
          const shownApplied = collapsed
            ? (message.applied ?? []).slice(0, 3)
            : (message.applied ?? []);
          const remainingSlots = Math.max(0, chips.length - shownApplied.length);
          const showUndo =
            message.id === latestChanged?.id &&
            message.undoVersion === historyVersion &&
            canUndo &&
            (message.applied?.length ?? 0) > 0;
          return (
            <div key={message.id} className="space-y-1.5">
              <div
                className={[
                  "whitespace-pre-line px-3.5 py-2.5 text-[13.5px] leading-5",
                  isUser
                    ? "ml-auto max-w-[85%] rounded-[18px] rounded-br-md bg-[#0c0c0e] text-white"
                    : "mr-auto max-w-[85%] rounded-[18px] rounded-bl-md bg-zinc-100 text-[#0c0c0e]",
                ].join(" ")}
              >
                {message.text}
              </div>
              {!isUser && chips.length > 0 && (
                <div className="flex flex-wrap items-center gap-1.5">
                  {shownApplied.map((summary) => {
                    const parsed = parseApplied(summary);
                    return (
                      <span
                        key={summary}
                        className="inline-flex min-h-8 items-center rounded-full border border-lime-200 bg-lime-50 px-3 text-[12px] text-lime-800"
                      >
                        {parsed.label} <b className="ml-1 font-semibold">{parsed.value}</b>
                      </span>
                    );
                  })}
                  {!collapsed &&
                    (message.rejected ?? []).map((summary) => (
                      <span
                        key={summary}
                        className="inline-flex min-h-8 items-center rounded-full border border-dashed border-zinc-300 bg-white px-3 text-[12px] text-[#71717a]"
                      >
                        Couldn&apos;t apply: {summary}
                      </span>
                    ))}
                  {remainingSlots > 0 && (
                    <button
                      type="button"
                      onClick={() => setExpanded((cur) => ({ ...cur, [message.id]: true }))}
                      className="min-h-8 rounded-full border border-zinc-200 px-3 text-[12px] text-[#3f3f46] hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                    >
                      +{remainingSlots} more
                    </button>
                  )}
                  {showUndo && (
                    <button
                      type="button"
                      onClick={onUndo}
                      className="min-h-8 px-1 text-[12px] text-[#71717a] underline underline-offset-2 hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                    >
                      Undo
                    </button>
                  )}
                </div>
              )}
            </div>
          );
        })}

        {sending && <Thinking elapsed={elapsed} onStop={onStop} />}
        {queued && (
          <div className="ml-auto max-w-[85%] rounded-[18px] rounded-br-md border border-dashed border-zinc-300 bg-white px-3.5 py-2.5 text-[13px] text-[#3f3f46]">
            <p className="mb-1 text-[11px] uppercase tracking-wide text-[#a1a1aa]">
              Queued after current edit
            </p>
            <button
              type="button"
              onClick={() => {
                setDraft(queued.text);
                inputRef.current?.focus();
              }}
              className="block text-left text-[#0c0c0e] underline decoration-zinc-300 underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
            >
              {queued.text}
            </button>
            <button
              type="button"
              aria-label="Cancel queued message"
              onClick={onCancelQueued}
              className="mt-2 min-h-8 rounded-full px-2 text-[12px] text-[#71717a] hover:bg-zinc-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
            >
              Cancel
            </button>
          </div>
        )}
        {error && (
          <div
            role="status"
            aria-live="polite"
            className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-[12.5px] text-[#3f3f46]"
          >
            I couldn&apos;t reach Nova just now. Your edit is untouched — try again.
          </div>
        )}
      </div>

      <div aria-live="polite" className="sr-only">
        {latestChanged?.applied?.length
          ? `Applied: ${latestChanged.applied.join(", ")}`
          : ""}
      </div>

      <div className="flex flex-none flex-wrap gap-1.5 border-t border-zinc-200 px-4 pb-2 pt-3">
        {activeSuggestions.map((suggestion) => (
          <button
            key={suggestion}
            type="button"
            disabled={!!queued}
            onClick={() => onSend(suggestion)}
            className="min-h-11 rounded-full border border-zinc-200 bg-white px-3 text-[12px] text-[#3f3f46] hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {suggestion}
          </button>
        ))}
      </div>

      <form
        className="flex flex-none items-end gap-2 px-4 pb-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div className="min-w-0 flex-1">
          <input
            ref={inputRef}
            type="text"
            value={draft}
            maxLength={MAX_CHARS}
            onChange={(e) => {
              // Typing must NOT live-mutate the queued message — a half-typed
              // fragment would be dispatched if the in-flight turn resolves
              // mid-keystroke, and backspacing to empty would silently cancel
              // it (review F2). Queued edits happen only on explicit submit.
              setDraft(e.target.value.slice(0, MAX_CHARS));
            }}
            placeholder={sending ? "Add more while I work..." : "Tell me what to change..."}
            aria-label="Tell Nova what to change"
            className="min-h-11 w-full rounded-lg border border-zinc-200 bg-white px-3 text-[16px] text-[#0c0c0e] outline-none placeholder:text-[#a1a1aa] focus:border-lime-500 focus:ring-2 focus:ring-lime-500/25 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 md:text-[13px]"
          />
          {draft.length >= MAX_CHARS * 0.8 && (
            <p className="mt-1 text-right text-[11px] text-[#71717a]">
              {draft.length}/{MAX_CHARS}
            </p>
          )}
        </div>
        <button
          type="submit"
          disabled={draft.trim().length === 0}
          aria-label={sending ? "Queue message" : "Send message"}
          className="flex h-11 w-11 flex-none items-center justify-center rounded-lg bg-[#0c0c0e] text-[15px] font-semibold text-white hover:opacity-85 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-35"
        >
          ↑
        </button>
      </form>
    </section>
  );
}

function Thinking({
  elapsed,
  onStop,
}: {
  elapsed: number;
  onStop: () => void;
}) {
  const showPlanning = elapsed >= 2000;
  const showStop = elapsed >= 5000;
  const late = elapsed >= 8000;
  return (
    <div role="status" className="mr-auto max-w-[85%] space-y-2 text-[13px] text-[#71717a]">
      <div className="flex items-center gap-2">
        <span className="h-2 w-2 rounded-full bg-lime-600 motion-safe:animate-ping" />
        {showPlanning && (
          <span>{late ? "Still working — keep editing." : "Planning edits..."}</span>
        )}
        {showStop && (
          <button
            type="button"
            onClick={onStop}
            className="ml-2 min-h-8 text-[12px] text-[#71717a] underline underline-offset-2 hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
          >
            Stop
          </button>
        )}
      </div>
      {showPlanning && (
        <div className="space-y-1">
          <div className="h-2.5 w-4/5 rounded-full bg-[linear-gradient(90deg,#f4f4f5_25%,#fff_50%,#f4f4f5_75%)] bg-[length:200%_100%] motion-safe:animate-shimmer" />
          <div className="h-2.5 w-1/2 rounded-full bg-[linear-gradient(90deg,#f4f4f5_25%,#fff_50%,#f4f4f5_75%)] bg-[length:200%_100%] motion-safe:animate-shimmer" />
        </div>
      )}
    </div>
  );
}
