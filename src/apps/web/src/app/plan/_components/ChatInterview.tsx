"use client";

import { useEffect, useRef, useState } from "react";
import { chatStart, chatTurn, NotAuthenticatedError } from "@/lib/plan-api";
import type { TikTokProfile } from "@/lib/plan-api";

type Phase = "loading" | "chat" | "thinking" | "error";

/**
 * Adaptive AI onboarding interview. Replaces the 8-question fixed form.
 *
 * Layout rules (anti-slop):
 * - Agent question: LEFT-ALIGNED Playfair Display, text-3xl, white — it floats
 *   on black and is the only anchor on the screen.
 * - One prior-answer pull-quote above the current Q (amber left-border). Never
 *   a scrollable chat thread, never left/right bubbles.
 * - No bot avatar, no "AI:" label. This is a dialogue, not a customer service chat.
 * - Suggestion chips: horizontal scroll on mobile, 44px touch targets.
 * - Input: sticky bottom-0, keyboard-safe on iOS (env safe-area-inset-bottom).
 */
export default function ChatInterview({ onComplete }: { onComplete: () => void }) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [personaId, setPersonaId] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [turnLabel, setTurnLabel] = useState("");
  const [tiktokContext, setTiktokContext] = useState<TikTokProfile | null>(null);
  const [priorAnswer, setPriorAnswer] = useState<string | null>(null);
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [retryPayload, setRetryPayload] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    chatStart()
      .then((res) => {
        if (["generating", "ready", "edited"].includes(res.persona_status)) {
          onComplete();
          return;
        }
        setPersonaId(res.persona_id);
        setQuestion(res.question);
        setSuggestions(res.suggestions);
        setTurnLabel(res.turn_label);
        setTiktokContext(res.tiktok_context ?? null);
        setPhase("chat");
      })
      .catch((err: unknown) => {
        if (err instanceof NotAuthenticatedError) {
          window.location.href = "/api/auth/signin?callbackUrl=/plan";
          return;
        }
        setError("Couldn't start the interview. Try refreshing.");
        setPhase("error");
      });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (phase === "chat") inputRef.current?.focus();
  }, [question, phase]);

  function resetHeight() {
    if (inputRef.current) inputRef.current.style.height = "auto";
  }

  async function submit(text?: string) {
    const payload = (text ?? answer).trim();
    if (!payload || phase !== "chat" || !personaId) return;
    setAnswer("");
    resetHeight();
    setPriorAnswer(payload);
    setPhase("thinking");
    setError(null);

    try {
      const res = await chatTurn(personaId, payload);
      if (res.is_final) {
        onComplete();
        return;
      }
      setQuestion(res.question!);
      setSuggestions(res.suggestions);
      setTurnLabel(res.turn_label);
      setPhase("chat");
    } catch {
      setRetryPayload(payload);
      setError("Something went wrong — let me try again.");
      setPhase("error");
    }
  }

  async function retry() {
    if (!retryPayload || !personaId) return;
    setPhase("thinking");
    setError(null);
    try {
      const res = await chatTurn(personaId, retryPayload);
      setRetryPayload(null);
      if (res.is_final) {
        onComplete();
        return;
      }
      setQuestion(res.question!);
      setSuggestions(res.suggestions);
      setTurnLabel(res.turn_label);
      setPhase("chat");
    } catch {
      setError("I hit a snag. Try refreshing.");
    }
  }

  if (phase === "loading") {
    return (
      <div className="flex items-center gap-2 py-10">
        <span className="h-1.5 w-1.5 animate-ping rounded-full bg-amber-400" />
        <span className="text-sm text-zinc-500">Getting ready…</span>
      </div>
    );
  }

  return (
    <div className="flex min-h-[60vh] flex-col">
      {/* TikTok context chip — persists for the rest of the chat */}
      {tiktokContext && (
        <div
          className="mb-6 inline-flex items-center gap-2 self-start rounded-full bg-zinc-800 px-3 py-1.5"
          aria-label={`TikTok profile loaded: @${tiktokContext.handle}`}
        >
          <span className="h-1.5 w-1.5 rounded-full bg-amber-400" />
          <span className="text-xs text-zinc-300">
            @{tiktokContext.handle}
            {tiktokContext.follower_count != null && (
              <>
                {" "}
                ·{" "}
                {tiktokContext.follower_count >= 1000
                  ? `${(tiktokContext.follower_count / 1000).toFixed(1)}K`
                  : tiktokContext.follower_count}{" "}
                followers
              </>
            )}
            {tiktokContext.video_count != null && (
              <> · {tiktokContext.video_count} videos analyzed</>
            )}
          </span>
        </div>
      )}

      {/* Question area — grows to fill available space */}
      <div className="flex-1">
        {/* Eyebrow */}
        <p className="mb-3 text-xs font-medium uppercase tracking-wide text-amber-300">
          {turnLabel || "Getting to know you"}
        </p>

        {/* The question: LEFT-ALIGNED, no card, no border, just text on black */}
        <div role="status" aria-live="polite">
          <h1 className="animate-fade-up font-display text-2xl leading-snug text-white sm:text-3xl">
            {question}
          </h1>
        </div>

        {/* Prior-answer pull-quote — ONE entry, amber left-border, never a thread */}
        {priorAnswer && (
          <blockquote className="mt-5 border-l-2 border-amber-400 pl-3">
            <p className="line-clamp-3 text-sm text-zinc-400">{priorAnswer}</p>
          </blockquote>
        )}

        {/* Suggestion chips — horizontal scroll on mobile, no wrapping */}
        {suggestions.length > 0 && phase !== "thinking" && (
          <div
            className="mt-4 flex gap-2 overflow-x-auto pb-1"
            style={{ WebkitOverflowScrolling: "touch" } as React.CSSProperties}
          >
            {suggestions.map((chip) => (
              <button
                key={chip}
                type="button"
                onClick={() => setAnswer(chip)}
                className="min-h-[44px] flex-none whitespace-nowrap rounded-full border border-zinc-800 bg-zinc-900 px-4 py-2 text-xs text-amber-300 transition-colors hover:border-amber-400/50 hover:text-amber-200"
              >
                {chip}
              </button>
            ))}
          </div>
        )}

        {/* Thinking dot — single amber ping, no full-screen spinner */}
        {phase === "thinking" && (
          <div
            className="mt-5 flex items-center gap-2"
            role="status"
            aria-label="Nova is thinking"
          >
            <span className="h-1.5 w-1.5 animate-ping rounded-full bg-amber-400" />
          </div>
        )}

        {/* Error state */}
        {error && phase === "error" && (
          <div className="mt-5 rounded-lg border border-amber-400/30 bg-zinc-900 px-4 py-3">
            <p className="text-sm text-amber-300">{error}</p>
            <button
              type="button"
              onClick={() => void retry()}
              className="mt-2 text-xs text-zinc-400 underline hover:text-white"
            >
              Try again
            </button>
          </div>
        )}
      </div>

      {/* Sticky input — iOS safe-area-aware, keyboard doesn't bury it */}
      <div className="sticky bottom-0 z-10 bg-zinc-950 pb-[env(safe-area-inset-bottom)] pt-4">
        <div className="flex items-end gap-3 rounded-2xl border border-zinc-800 bg-zinc-900 px-4 py-3">
          <textarea
            ref={inputRef}
            value={answer}
            rows={1}
            placeholder="Tell me…"
            disabled={phase === "thinking" || phase === "loading"}
            aria-label="Your answer"
            onChange={(e) => {
              setAnswer(e.target.value);
              // Auto-resize
              e.target.style.height = "auto";
              e.target.style.height = `${e.target.scrollHeight}px`;
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void submit();
              }
            }}
            className="max-h-[120px] flex-1 resize-none bg-transparent text-sm text-white placeholder-zinc-600 focus:outline-none disabled:opacity-50"
          />
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!answer.trim() || phase === "thinking" || phase === "loading"}
            aria-label="Send answer"
            className="flex min-h-[44px] min-w-[44px] flex-shrink-0 items-center justify-center rounded-full bg-amber-400 text-sm font-medium text-black transition-colors hover:bg-amber-300 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-500"
          >
            →
          </button>
        </div>
      </div>
    </div>
  );
}
