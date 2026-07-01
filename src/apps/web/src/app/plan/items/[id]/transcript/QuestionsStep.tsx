"use client";

import { useEffect, useRef, useState } from "react";
import {
  transcriptInterview,
  NotAuthenticatedError,
  type TranscriptTurn,
} from "@/lib/transcript-api";

type Phase = "loading" | "chat" | "thinking" | "error";

/**
 * Step 2 — Questions.
 *
 * Reuses the editorial-interview layout (DESIGN §2, StyleAgentInterview):
 * left-aligned Fraunces question, suggestion chips, ONE prior-answer pull-quote
 * with a lime left-border, sticky input. NO chat bubbles, NO avatar.
 *
 * Collects the user's answers (user turns) and advances to Script when the agent
 * says is_final, or when the user clicks "Skip — just write it".
 */
export default function QuestionsStep({
  itemId,
  brief,
  footageSummary,
  onDone,
}: {
  itemId: string;
  brief: string;
  footageSummary: string | null;
  onDone: (answers: string[]) => void;
}) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [question, setQuestion] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [priorUtterance, setPriorUtterance] = useState<string | null>(null);
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState<string | null>(null);

  const turnsRef = useRef<TranscriptTurn[]>([]);
  const answersRef = useRef<string[]>([]);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    transcriptInterview(itemId, { brief, footage_summary: footageSummary, turns: [] })
      .then((res) => {
        if (res.is_final) {
          // Agent had nothing to ask — go straight to writing.
          onDone([]);
          return;
        }
        turnsRef.current = [{ role: "agent", content: res.question }];
        setQuestion(res.question);
        setSuggestions(res.suggestions);
        setPhase("chat");
      })
      .catch((err: unknown) => {
        if (err instanceof NotAuthenticatedError) {
          window.location.href = `/api/auth/signin?callbackUrl=/plan/items/${itemId}/transcript`;
          return;
        }
        setError("Couldn't start the questions. You can skip straight to writing.");
        setPhase("error");
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (phase === "chat") inputRef.current?.focus();
  }, [question, phase]);

  async function submit(text?: string) {
    const payload = (text ?? answer).trim();
    if (!payload || phase !== "chat") return;
    setAnswer("");
    setPriorUtterance(payload);
    setPhase("thinking");
    setError(null);

    const nextTurns: TranscriptTurn[] = [
      ...turnsRef.current,
      { role: "user", content: payload },
    ];
    answersRef.current = [...answersRef.current, payload];

    try {
      const res = await transcriptInterview(itemId, {
        brief,
        footage_summary: footageSummary,
        turns: nextTurns,
      });
      turnsRef.current = [...nextTurns, { role: "agent", content: res.question }];
      if (res.is_final) {
        onDone(answersRef.current);
        return;
      }
      setQuestion(res.question);
      setSuggestions(res.suggestions);
      setPhase("chat");
    } catch {
      setError("Something went wrong — you can skip to writing.");
      setPhase("error");
    }
  }

  if (phase === "loading") {
    return (
      <div className="flex items-center gap-2 py-10">
        <span className="h-1.5 w-1.5 motion-safe:animate-ping rounded-full bg-lime-600" />
        <span className="text-sm text-[#71717a]">Thinking of a good question…</span>
      </div>
    );
  }

  return (
    <div className="max-w-xl py-2">
      <p className="mb-3 text-xs font-medium uppercase tracking-wide text-lime-700">
        A couple of questions
      </p>

      {/* Agent question — LEFT-ALIGNED Fraunces, no card, no bubble */}
      <div role="status" aria-live="polite">
        <h1 className="animate-fade-up font-display text-2xl leading-snug text-[#0c0c0e] sm:text-3xl">
          {question}
        </h1>
      </div>

      {/* Prior-answer pull-quote — ONE entry, lime left-border, never a thread */}
      {priorUtterance && (
        <blockquote className="mt-5 border-l-2 border-lime-600 pl-3">
          <p className="line-clamp-3 text-sm italic text-[#71717a]">{priorUtterance}</p>
        </blockquote>
      )}

      {/* Suggestion chips — tap to auto-submit */}
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
        <div className="mt-5 flex items-center gap-2" role="status" aria-label="Thinking">
          <span className="h-1.5 w-1.5 motion-safe:animate-ping rounded-full bg-lime-600" />
          <span className="text-sm text-[#71717a]">Thinking…</span>
        </div>
      )}

      {/* Error state — quiet, not alarming */}
      {error && phase === "error" && (
        <div className="mt-5 rounded-lg border border-zinc-200 bg-white px-4 py-3">
          <p className="text-sm text-[#3f3f46]">{error}</p>
        </div>
      )}

      {/* Always-available skip */}
      <div className="mt-6">
        <button
          type="button"
          onClick={() => onDone(answersRef.current)}
          className="text-xs text-[#71717a] underline underline-offset-4 hover:text-[#0c0c0e]"
        >
          Skip — just write it
        </button>
      </div>

      {/* Sticky input */}
      <div className="sticky bottom-0 z-10 mt-8 bg-[#fafaf8] pb-[env(safe-area-inset-bottom)]">
        <div className="flex items-center gap-3 rounded-2xl border border-zinc-200 bg-white px-4 py-2">
          <textarea
            ref={inputRef}
            value={answer}
            rows={1}
            placeholder="Type your answer…"
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
