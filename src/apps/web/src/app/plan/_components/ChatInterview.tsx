"use client";

import { useEffect, useRef, useState } from "react";
import { chatStart, chatTurn, NotAuthenticatedError } from "@/lib/plan-api";
import type { TikTokProfile } from "@/lib/plan-api";

type Phase = "loading" | "chat" | "thinking" | "error";

/**
 * Adaptive AI onboarding interview. Replaces the 8-question fixed form.
 *
 * Layout rules (anti-slop):
 * - Agent question: LEFT-ALIGNED Playfair Display, text-3xl, #0c0c0e — it floats
 *   on cream and is the only anchor on the screen.
 * - One prior-answer pull-quote above the current Q (lime left-border). Never
 *   a scrollable chat thread, never left/right bubbles.
 * - No bot avatar, no "AI:" label. This is a dialogue, not a customer service chat.
 * - Suggestion chips: horizontal scroll on mobile, 44px touch targets.
 * - Input: sticky bottom-0, keyboard-safe on iOS (env safe-area-inset-bottom).
 */
export default function ChatInterview({
  onComplete,
  onPersonaCreated,
}: {
  onComplete: () => void;
  /** Fires with persona_id immediately after chatStart() creates the row. */
  onPersonaCreated?: (personaId: string) => void;
}) {
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
        onPersonaCreated?.(res.persona_id);
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

  async function submit(text?: string) {
    const payload = (text ?? answer).trim();
    if (!payload || phase !== "chat" || !personaId) return;
    setAnswer("");
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
        <span className="h-1.5 w-1.5 motion-safe:animate-ping rounded-full bg-lime-600" />
        <span className="text-sm text-[#71717a]">Getting ready…</span>
      </div>
    );
  }

  return (
    <div className="py-2">
      {/* TikTok context chip */}
      {tiktokContext && (
        <div
          className="mb-6 inline-flex items-center gap-2 self-start rounded-full border border-zinc-200 bg-white px-3 py-1.5"
          aria-label={`TikTok profile loaded: @${tiktokContext.handle}`}
        >
          <span className="h-1.5 w-1.5 rounded-full bg-lime-600" />
          <span className="text-xs text-[#3f3f46]">
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

      {/* Eyebrow */}
      <p className="mb-3 text-xs font-medium uppercase tracking-wide text-lime-700">
        {turnLabel || "Getting to know you"}
      </p>

      {/* Question — LEFT-ALIGNED, no card, no border */}
      <div role="status" aria-live="polite">
        <h1 className="animate-fade-up font-display text-2xl leading-snug text-[#0c0c0e] sm:text-3xl">
          {question}
        </h1>
      </div>

      {/* Prior-answer pull-quote — ONE entry, lime left-border, never a thread */}
      {priorAnswer && (
        <blockquote className="mt-5 border-l-2 border-lime-600 pl-3">
          <p className="line-clamp-3 text-sm text-[#71717a]">{priorAnswer}</p>
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

      {/* Input — sits naturally below content, sticky on mobile when keyboard opens */}
      <div className="sticky bottom-0 z-10 mt-8 bg-[#fafaf8] pb-[env(safe-area-inset-bottom)]">
        <div className="flex items-center gap-3 rounded-2xl border border-zinc-200 bg-white px-4 py-2">
          <textarea
            ref={inputRef}
            value={answer}
            rows={1}
            placeholder="Tell me…"
            disabled={phase === "thinking"}
            aria-label="Your answer"
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
            aria-label="Send answer"
            className="flex min-h-[44px] min-w-[44px] flex-shrink-0 items-center justify-center rounded-full bg-[#0c0c0e] text-sm font-medium text-white transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-25"
          >
            →
          </button>
        </div>
      </div>
    </div>
  );
}
