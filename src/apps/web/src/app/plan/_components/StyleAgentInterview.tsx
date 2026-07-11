"use client";

import { useEffect, useRef, useState } from "react";
import { styleAgentStart, styleAgentTurn, NotAuthenticatedError } from "@/lib/plan-api";

type Phase = "loading" | "chat" | "thinking" | "error";

/**
 * Conversational style editor (Creator Agent M2).
 *
 * Layout rules (DESIGN.md — editorial interview, NO chat bubbles):
 * - Agent reply: LEFT-ALIGNED Playfair Display, text-2xl/3xl, #0c0c0e — floats on cream.
 * - One prior-utterance pull-quote (lime left-border). Never a scrollable thread.
 * - No bot avatar, no "AI:" label. Dialogue, not customer service chat.
 * - Suggestion chips: horizontal, 44px touch targets.
 * - Input: sticky bottom-0, keyboard-safe on iOS.
 * - applied=true: brief lime confirmation line (not a toast, not a banner).
 */
export default function StyleAgentInterview({ onDone }: { onDone?: () => void }) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [reply, setReply] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [priorUtterance, setPriorUtterance] = useState<string | null>(null);
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [retryPayload, setRetryPayload] = useState<{ answer: string; turns: unknown[] } | null>(
    null,
  );
  const [appliedMessage, setAppliedMessage] = useState<string | null>(null);
  const [priorTurns, setPriorTurns] = useState<unknown[]>([]);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    styleAgentStart()
      .then((res) => {
        setReply(res.reply);
        setSuggestions(res.suggestions);
        setPhase("chat");
      })
      .catch((err: unknown) => {
        if (err instanceof NotAuthenticatedError) {
          window.location.href = "/api/auth/signin?callbackUrl=/plan/style";
          return;
        }
        setError("Couldn't start the style editor. Try refreshing.");
        setPhase("error");
      });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (phase === "chat") inputRef.current?.focus();
  }, [reply, phase]);

  async function submit(text?: string) {
    const payload = (text ?? answer).trim();
    if (!payload || phase !== "chat") return;
    setAnswer("");
    setPriorUtterance(payload);
    setPhase("thinking");
    setError(null);
    setAppliedMessage(null);

    const currentTurns = priorTurns;

    try {
      const res = await styleAgentTurn(payload, currentTurns);
      const newTurns = [
        ...currentTurns,
        { role: "user", content: payload },
        { role: "agent", content: res.reply, intent: res.intent },
      ];
      setPriorTurns(newTurns);
      setReply(res.reply);
      setSuggestions(res.suggestions);
      if (res.applied) {
        setAppliedMessage("Done — your next render will use this style.");
      }
      setPhase("chat");
    } catch {
      setRetryPayload({ answer: payload, turns: currentTurns });
      setError("Something went wrong — let me try again.");
      setPhase("error");
    }
  }

  async function retry() {
    if (!retryPayload) return;
    setPhase("thinking");
    setError(null);
    try {
      const res = await styleAgentTurn(retryPayload.answer, retryPayload.turns);
      const newTurns = [
        ...retryPayload.turns,
        { role: "user", content: retryPayload.answer },
        { role: "agent", content: res.reply, intent: res.intent },
      ];
      setPriorTurns(newTurns);
      setRetryPayload(null);
      setReply(res.reply);
      setSuggestions(res.suggestions);
      if (res.applied) {
        setAppliedMessage("Done — your next render will use this style.");
      }
      setPhase("chat");
    } catch {
      setError("I hit a snag. Try refreshing.");
    }
  }

  if (phase === "loading") {
    return (
      <div className="flex items-center gap-2 py-10">
        <span className="h-1.5 w-1.5 motion-safe:animate-ping rounded-full bg-lime-600" />
        <span className="text-sm text-[#71717a]">Loading your style…</span>
      </div>
    );
  }

  return (
    <div className="py-2">
      {/* Eyebrow */}
      <p className="mb-3 text-xs font-medium uppercase tracking-wide text-lime-700">
        Your style
      </p>

      {/* Agent reply — LEFT-ALIGNED, Playfair Display, no card, no border */}
      <div role="status" aria-live="polite">
        <h1 className="animate-fade-up font-display text-2xl leading-snug text-[#0c0c0e] sm:text-3xl">
          {reply}
        </h1>
      </div>

      {/* Applied confirmation — brief lime line, not a banner */}
      {appliedMessage && (
        <p className="mt-3 text-sm font-medium text-lime-700">{appliedMessage}</p>
      )}

      {/* Prior-utterance pull-quote — ONE entry, lime left-border, never a thread */}
      {priorUtterance && (
        <blockquote className="mt-5 border-l-2 border-lime-600 pl-3">
          <p className="line-clamp-3 text-sm italic text-[#71717a]">{priorUtterance}</p>
        </blockquote>
      )}

      {/* Suggestion chips — tap to auto-submit, wrap on desktop */}
      {suggestions.length > 0 && phase !== "thinking" && (
        <div className="mt-5 flex flex-wrap gap-2">
          {suggestions.map((chip) => (
            <button
              key={chip}
              type="button"
              onClick={() => void submit(chip)}
              className="min-h-[40px] rounded-full border border-zinc-200 bg-white px-4 py-2 text-sm text-[#3f3f46] transition-colors hover:border-lime-600/60 hover:bg-zinc-50 hover:text-[#0c0c0e]"
            >
              {chip}
            </button>
          ))}
        </div>
      )}

      {/* Thinking dot */}
      {phase === "thinking" && (
        <div
          className="mt-5 flex items-center gap-2"
          role="status"
          aria-label="Kria is thinking"
        >
          <span className="h-1.5 w-1.5 motion-safe:animate-ping rounded-full bg-lime-600" />
          <span className="text-sm text-[#71717a]">Thinking…</span>
        </div>
      )}

      {/* Error state */}
      {error && phase === "error" && (
        <div className="mt-5 rounded-lg border border-zinc-200 bg-white px-4 py-3">
          <p className="text-sm text-[#3f3f46]">{error}</p>
          <button
            type="button"
            onClick={() => void retry()}
            className="mt-2 text-xs text-[#71717a] underline hover:text-[#0c0c0e]"
          >
            Try again
          </button>
        </div>
      )}

      {/* Optional Done link */}
      {onDone && phase === "chat" && priorTurns.length > 0 && (
        <div className="mt-6">
          <button
            type="button"
            onClick={onDone}
            className="text-xs text-[#71717a] underline hover:text-[#0c0c0e]"
          >
            Back to workspace
          </button>
        </div>
      )}

      {/* Input — sticky on mobile when keyboard opens */}
      <div className="sticky bottom-0 z-10 mt-8 bg-[#fafaf8] pb-[env(safe-area-inset-bottom)]">
        <div className="flex items-center gap-3 rounded-2xl border border-zinc-200 bg-white px-4 py-2">
          <textarea
            ref={inputRef}
            value={answer}
            rows={1}
            placeholder="Tell me what to change…"
            disabled={phase === "thinking"}
            aria-label="Your style request"
            onChange={(e) => setAnswer(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void submit();
              }
            }}
            className="flex-1 resize-none bg-transparent text-sm text-[#0c0c0e] placeholder-zinc-400 focus:outline-none disabled:opacity-50 [field-sizing:content]"
          />
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!answer.trim() || phase === "thinking"}
            aria-label="Send request"
            className="flex min-h-[44px] min-w-[44px] flex-shrink-0 items-center justify-center rounded-full bg-[#0c0c0e] text-sm font-medium text-white transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-25"
          >
            →
          </button>
        </div>
      </div>
    </div>
  );
}
