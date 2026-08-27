"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type {
  CopilotMessage,
  QueuedCopilotMessage,
} from "@/lib/edit-copilot/useEditCopilot";
import type { EditorLayoutMode } from "./useEditorLayoutMode";
import DirectorSuggestions from "./DirectorSuggestions";
import type { UseEditDirectorResult } from "@/lib/edit-copilot/useEditDirector";
import { NovaActivityFeed, NovaStepRow } from "@/components/progress";
import type { NovaStep } from "@/lib/job-phases";
import { InfoDot } from "@/components/ui/InfoDot";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ArrowUp } from "lucide-react";
import { ChatBubble } from "@/components/chat/ChatBubble";
import { useAutoScrollToEnd } from "@/components/chat/useAutoScrollToEnd";
import { CloseIcon } from "./editor-icons";

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

/** Chat steps feed (PR4): the most recent server-render turn (today: only
 *  set_intro_layout) — used to decide which message, if any, is still
 *  associated with an active EditorShell poll (see renderTurnActive). */
function latestAssistantRenderTurn(messages: CopilotMessage[]): CopilotMessage | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const msg = messages[i];
    if (msg.role === "assistant" && msg.isRenderTurn) return msg;
  }
  return null;
}

/** Compact step-row label for an applied op — same "label value" the retired
 *  lime pill showed (`parseApplied`), just without the bold weight on the
 *  new value (NovaStepRow's `label` is a plain string). */
function appliedRowLabel(summary: string): string {
  const parsed = parseApplied(summary);
  return `${parsed.label} ${parsed.value}`;
}

function noop() {}

export default function CopilotDrawer({
  layoutMode,
  open = true,
  messages,
  sending,
  queued,
  error,
  unavailable = false,
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
  director,
  renderTurnActive = false,
  renderTurnSteps = null,
}: {
  layoutMode: EditorLayoutMode;
  open?: boolean;
  messages: CopilotMessage[];
  sending: boolean;
  queued: QueuedCopilotMessage | null;
  error: string | null;
  /** API has no copilot route: composer goes inert rather than dead-ending. */
  unavailable?: boolean;
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
  director?: UseEditDirectorResult;
  /** Chat steps feed (PR4): true while EditorShell is polling job status for
   *  the latest server-render turn in THIS mount. False (default) for a
   *  freshly (re)mounted drawer, including one reopened onto a thread whose
   *  render-turn already finished in a prior session — that historical
   *  message shows its disclosure bubble text only, no stale spinner. */
  renderTurnActive?: boolean;
  /** Last-polled `steps` for the active render turn (PR1 projection, same
   *  field the item page's ProgressTheater consumes). Null until the first
   *  poll response lands. */
  renderTurnSteps?: NovaStep[] | null;
}) {
  const [draft, setDraft] = useState("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const inputRef = useRef<HTMLInputElement>(null);
  const elapsed = useElapsed(sending);
  const keyboardOffset = useKeyboardOffset(layoutMode === "light" && open);
  const latestChanged = useMemo(() => latestAssistantWithChanges(messages), [messages]);
  const latestRenderTurn = useMemo(() => latestAssistantRenderTurn(messages), [messages]);
  const starterVisible = messages.length === 0;
  // Steps feed (NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED): retires the lime
  // ChangeChip receipt pills in favor of compact NovaStepRows, and gives
  // server-render turns their own disclosure + live NovaActivityFeed. Flag
  // off renders exactly as before (byte-identical fallback).
  const stepsFeedEnabled = process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED === "true";
  // Contextual suggestion chips (artboard 06): "Undo that" / "What else
  // changed?" replace the generic starters right after an applied,
  // locally-undoable turn — the same staleness guard as the Undo link, and
  // never shown after a (non-undoable) server-render turn.
  const showContextualChips =
    stepsFeedEnabled &&
    !!latestChanged &&
    !latestChanged.isRenderTurn &&
    latestChanged.undoVersion === historyVersion &&
    canUndo &&
    (latestChanged.applied?.length ?? 0) > 0;
  const activeSuggestions = showContextualChips
    ? []
    : suggestions.length > 0
      ? suggestions
      : starterVisible
        ? STARTERS
        : [];

  useEffect(() => {
    if (!restoredInput) return;
    setDraft(restoredInput.slice(0, MAX_CHARS));
    onClearRestoredInput();
  }, [onClearRestoredInput, restoredInput]);

  const threadRef = useAutoScrollToEnd<HTMLDivElement>([messages, sending, queued, error]);

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
      ? "flex h-full w-[360px] flex-col border-r border-border bg-background"
      : layoutMode === "overlay"
        ? "flex h-[220px] w-full flex-col rounded-xl border border-border bg-background shadow-[0_18px_48px_rgba(12,12,14,0.18)]"
        : "fixed inset-x-0 bottom-0 z-[95] flex max-h-[74dvh] min-h-[360px] flex-col rounded-t-2xl border-t border-border bg-background shadow-[0_-18px_48px_rgba(12,12,14,0.2)]";

  return (
    <section
      data-testid={`copilot-${layoutMode}`}
      aria-label="Kria editor copilot"
      className={rootClass}
      style={layoutMode === "light" ? { paddingBottom: keyboardOffset } : undefined}
    >
      {layoutMode === "light" && (
        <div aria-hidden className="flex justify-center py-2 touch-none">
          <span className="h-1 w-10 rounded-full bg-muted-foreground/30" />
        </div>
      )}
      <div className="flex h-12 flex-none items-center justify-between px-4">
        <span className="flex items-center gap-1">
          <h2 className="text-base font-semibold text-foreground">Kria</h2>
          <InfoDot label="Kria">
            Kria can rewrite your hook, restyle text, and tighten or reorder cuts. Edits are
            staged in the timeline; Save renders the new video.
          </InfoDot>
        </span>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          aria-label="Close Kria"
          onClick={onClose}
        >
          <CloseIcon className="h-4 w-4" />
        </Button>
      </div>

      <ScrollArea ref={threadRef} className="min-h-0 flex-1">
      <div className="space-y-3 px-4 py-3">
        {director && (
          <DirectorSuggestions
            suggestions={director.suggestions}
            appliedReceipts={director.appliedReceipts}
            historyVersion={historyVersion}
            loading={director.loading}
            error={director.error}
            modelUsed={director.modelUsed}
            fallbackReason={director.fallbackReason}
            generation={director.generation}
            serverRendering={director.serverRendering}
            onAccept={director.accept}
            onDismiss={director.dismiss}
            onRevealApplied={director.revealApplied}
            onRefresh={director.refresh}
            onCancelGeneration={director.cancelGeneration}
            canRestoreOriginalTiming={director.canRestoreOriginalTiming}
            onRestoreOriginalTiming={director.restoreOriginalTiming}
          />
        )}

        {messages.map((message) => {
          const isUser = message.role === "user";
          const isRenderTurnMsg = stepsFeedEnabled && !isUser && !!message.isRenderTurn;
          const chips = [...(message.applied ?? []), ...(message.rejected ?? [])];
          const collapsed = chips.length > 3 && !expanded[message.id];
          const shownApplied = collapsed
            ? (message.applied ?? []).slice(0, 3)
            : (message.applied ?? []);
          const remainingSlots = Math.max(0, chips.length - shownApplied.length);
          const showUndo =
            !isRenderTurnMsg &&
            message.id === latestChanged?.id &&
            message.undoVersion === historyVersion &&
            canUndo &&
            (message.applied?.length ?? 0) > 0;
          const isActiveRenderTurn =
            isRenderTurnMsg && renderTurnActive && message.id === latestRenderTurn?.id;
          return (
            <div key={message.id} className="space-y-1.5">
              <ChatBubble role={isUser ? "user" : "assistant"}>
                {message.text}
              </ChatBubble>

              {/* Server-render turn (artboard 03): disclosure + live compact
                  NovaActivityFeed while THIS mount is polling; a historical
                  render turn (reopened later, or superseded by a newer one)
                  shows only the bubble text above — no stale spinner, no
                  Undo (non-undoable contract). */}
              {isRenderTurnMsg && isActiveRenderTurn && (
                <div className="mr-auto max-w-[85%]">
                  {renderTurnSteps && renderTurnSteps.length > 0 ? (
                    <div className="rounded-lg bg-muted p-3">
                      <NovaActivityFeed
                        steps={renderTurnSteps}
                        tone="light"
                        size="compact"
                        isTerminal={false}
                        isSuccess={false}
                      />
                    </div>
                  ) : (
                    <div className="space-y-2 rounded-lg border border-border bg-background p-3">
                      <p className="text-sm text-muted-foreground">
                        This re-renders the video and can&apos;t be undone from
                        chat. Your current version stays in history if you
                        want it back.
                      </p>
                      <div className="space-y-1" aria-hidden="true">
                        <Skeleton className="h-3 w-4/5" />
                        <Skeleton className="h-3 w-1/2" />
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Local-op turn, steps feed on (artboard 02): compact
                  NovaStepRows replace the retired lime ChangeChip pills. */}
              {!isRenderTurnMsg && stepsFeedEnabled && !isUser && chips.length > 0 && (
                <div className="space-y-1">
                  <ul role="list" aria-label="Kria AI changes" className="space-y-0.5">
                    {shownApplied.map((summary, idx) => (
                      <NovaStepRow
                        key={`applied-${idx}`}
                        step={{
                          id: `${message.id}-applied-${idx}`,
                          ts: "",
                          kind: "decision",
                          label: appliedRowLabel(summary),
                          detail: null,
                          status: "done",
                        }}
                        tone="light"
                        size="compact"
                        expanded={false}
                        onToggle={noop}
                      />
                    ))}
                    {!collapsed &&
                      (message.rejected ?? []).map((summary, idx) => (
                        <NovaStepRow
                          key={`rejected-${idx}`}
                          step={{
                            id: `${message.id}-rejected-${idx}`,
                            ts: "",
                            kind: "decision",
                            label: `Couldn't apply: ${summary}`,
                            detail: null,
                            status: "failed",
                          }}
                          tone="light"
                          size="compact"
                          expanded={false}
                          onToggle={noop}
                        />
                      ))}
                  </ul>
                  <div className="flex items-center gap-3 pl-1">
                    {remainingSlots > 0 && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => setExpanded((cur) => ({ ...cur, [message.id]: true }))}
                      >
                        +{remainingSlots} more
                      </Button>
                    )}
                    {showUndo && (
                      <Button type="button" variant="ghost" size="sm" onClick={onUndo}>
                        Undo
                      </Button>
                    )}
                  </div>
                </div>
              )}

              {/* Flag off: today's ChangeChip pills, byte-identical count/order. */}
              {!isRenderTurnMsg && !stepsFeedEnabled && !isUser && chips.length > 0 && (
                <div className="flex flex-wrap items-center gap-1.5">
                  {shownApplied.map((summary) => {
                    const parsed = parseApplied(summary);
                    return (
                      <Badge key={summary} variant="secondary">
                        {parsed.label} <span className="ml-1 font-medium">{parsed.value}</span>
                      </Badge>
                    );
                  })}
                  {!collapsed &&
                    (message.rejected ?? []).map((summary) => (
                      <Badge key={summary} variant="outline">
                        Couldn&apos;t apply: {summary}
                      </Badge>
                    ))}
                  {remainingSlots > 0 && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => setExpanded((cur) => ({ ...cur, [message.id]: true }))}
                    >
                      +{remainingSlots} more
                    </Button>
                  )}
                  {showUndo && (
                    <Button type="button" variant="ghost" size="sm" onClick={onUndo}>
                      Undo
                    </Button>
                  )}
                </div>
              )}
            </div>
          );
        })}

        {sending && <Thinking elapsed={elapsed} onStop={onStop} />}
        {queued && (
          <div className="ml-auto max-w-[85%] rounded-lg rounded-br-sm border border-dashed border-border bg-background px-3 py-2 text-sm text-foreground">
            <p className="mb-1 text-xs text-muted-foreground">
              Queued after current edit
            </p>
            <Button
              type="button"
              variant="link"
              onClick={() => {
                setDraft(queued.text);
                inputRef.current?.focus();
              }}
              className="block h-auto w-full whitespace-normal p-0 text-left text-foreground"
            >
              {queued.text}
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              aria-label="Cancel queued message"
              onClick={onCancelQueued}
              className="mt-2"
            >
              Cancel
            </Button>
          </div>
        )}
        {error && (
          <div
            role="status"
            aria-live="polite"
            className="rounded-lg border border-border bg-background p-3 text-sm text-foreground"
          >
            {error}
          </div>
        )}
      </div>
      </ScrollArea>

      <div aria-live="polite" className="sr-only">
        {latestChanged?.applied?.length
          ? `Staged: ${latestChanged.applied.join(", ")} — Save to render the new video`
          : ""}
      </div>

      <div className="flex flex-none flex-wrap gap-2 border-t border-border px-4 py-3">
        {showContextualChips ? (
          <>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={unavailable || !!queued}
              onClick={onUndo}
            >
              Undo that
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={unavailable || !!queued}
              onClick={() => onSend("Do that again")}
            >
              Do that again
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={unavailable || !!queued}
              onClick={() => onSend("What else changed?")}
            >
              What else changed?
            </Button>
          </>
        ) : (
          activeSuggestions.map((suggestion) => (
            <Button
              key={suggestion}
              type="button"
              variant="outline"
              size="sm"
              disabled={unavailable || !!queued}
              onClick={() => onSend(suggestion)}
            >
              {suggestion}
            </Button>
          ))
        )}
      </div>

      <form
        className="flex flex-none items-end gap-2 px-4 pb-4"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div className="min-w-0 flex-1">
          <Input
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
            disabled={unavailable}
            placeholder={
              unavailable
                ? "Kria editing is unavailable"
                : sending
                  ? "Add more while I work..."
                  : "Tell me what to change..."
            }
            aria-label="Tell Kria what to change"
          />
          {draft.length >= MAX_CHARS * 0.8 && (
            <p className="mt-1.5 text-right text-xs text-muted-foreground">
              {draft.length}/{MAX_CHARS}
            </p>
          )}
        </div>
        <Button
          type="submit"
          size="icon"
          disabled={unavailable || draft.trim().length === 0}
          aria-label={sending ? "Queue message" : "Send message"}
          className="flex-none"
        >
          <ArrowUp className="h-4 w-4" />
        </Button>
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
    <ChatBubble role="assistant">
      <div role="status" className="space-y-2">
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full bg-primary motion-safe:animate-ping" />
          {showPlanning && (
            <span>{late ? "Still working — keep editing." : "Planning edits..."}</span>
          )}
          {showStop && (
            <Button type="button" variant="ghost" size="sm" onClick={onStop} className="ml-2">
              Stop
            </Button>
          )}
        </div>
        {showPlanning && (
          <div className="space-y-1">
            <Skeleton className="h-3 w-4/5" />
            <Skeleton className="h-3 w-1/2" />
          </div>
        )}
      </div>
    </ChatBubble>
  );
}
