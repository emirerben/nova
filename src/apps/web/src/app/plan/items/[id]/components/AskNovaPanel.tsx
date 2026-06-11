"use client";

/**
 * AskNovaPanel — per-item "Ask Nova" filming advisor (dogfood feedback #2).
 *
 * Editorial interview, not a chat app (DESIGN.md §2/§9): left-aligned Playfair
 * question, lime left-border pull-quote of the user's last message, suggestion
 * chips, NO bubbles, NO avatar. Bounded sub-surface: the agent reply is capped
 * at text-xl (never the page-h1 scale) and the input is part of the panel —
 * never viewport-sticky — so Generate stays the page's primary element.
 *
 * Stateless backend contract (same as the style agent): the full conversation
 * rides in every call; the thread is ephemeral and lost on reload (v1 —
 * anything durable persists via the clip-note PATCH, not chat).
 */

import { useRef, useState } from "react";
import {
  contestConformance,
  planItemAdvisorTurn,
  setClipNote,
  type PlanItem,
} from "@/lib/plan-api";

interface Turn {
  role: "agent" | "user";
  content: string;
}

const OPENERS: Record<string, string> = {
  default: "What are you deciding? Describe your clips — I'll give you a read.",
  contest: "Tell me about the clip — what does it actually show?",
};

const DEFAULT_CHIPS = ["Which of my clips fits shot 1?", "What should I film instead?"];

export interface AskNovaPanelProps {
  item: PlanItem;
  /** "contest" when opened via "Looks wrong? Tell Nova" on the verdict tile. */
  mode: "default" | "contest";
  onClose: () => void;
  /** Refetch the item after a note is applied (conformance re-runs). */
  onItemChanged: () => void;
}

export default function AskNovaPanel({ item, mode, onClose, onItemChanged }: AskNovaPanelProps) {
  const [turns, setTurns] = useState<Turn[]>([
    { role: "agent", content: OPENERS[mode] ?? OPENERS.default },
  ]);
  const [input, setInput] = useState("");
  const [thinking, setThinking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<string[]>(DEFAULT_CHIPS);
  const [suggestedNote, setSuggestedNote] = useState("");
  const [applyingNote, setApplyingNote] = useState(false);
  const contested = useRef(false);

  // The clip a re-read offer applies to: the one the brief read described,
  // falling back to the first attached clip.
  const noteTargetPath =
    item.conformance?.clip_gcs_path ?? item.clip_gcs_paths[0] ?? null;

  async function send(text: string) {
    const message = text.trim();
    if (!message || thinking) return;
    setError(null);
    setInput("");
    setSuggestedNote("");
    const nextTurns: Turn[] = [...turns, { role: "user", content: message }];
    setTurns(nextTurns);
    setThinking(true);
    try {
      // Contest mode marks the verdict contested exactly once (idempotent server-side).
      if (mode === "contest" && !contested.current) {
        contested.current = true;
        contestConformance(item.id).catch(() => {});
      }
      const res = await planItemAdvisorTurn(item.id, message, nextTurns.slice(0, -1));
      setTurns([...nextTurns, { role: "agent", content: res.reply }]);
      setSuggestions(res.suggestions.length > 0 ? res.suggestions : []);
      setSuggestedNote(res.suggested_note ?? "");
    } catch {
      setError("Nova couldn't think that through — try again.");
      setInput(message); // give their words back
      setTurns(turns);
    } finally {
      setThinking(false);
    }
  }

  async function applySuggestedNote() {
    if (!suggestedNote || !noteTargetPath) return;
    setApplyingNote(true);
    try {
      await setClipNote(item.id, noteTargetPath, suggestedNote);
      setTurns((prev) => [
        ...prev,
        {
          role: "agent",
          content: "On it — re-reading the clip with that in mind. The read updates above shortly.",
        },
      ]);
      setSuggestedNote("");
      onItemChanged();
    } catch {
      setError("Couldn't save that note — try again.");
    } finally {
      setApplyingNote(false);
    }
  }

  const lastAgent = [...turns].reverse().find((t) => t.role === "agent");
  const lastUser = [...turns].reverse().find((t) => t.role === "user");

  return (
    <div className="mt-3 border-t border-zinc-200 pt-4" data-testid="ask-nova-panel">
      <div className="flex items-baseline justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-[0.18em] text-lime-700">
          Ask Nova
        </span>
        <button
          type="button"
          onClick={onClose}
          className="text-xs text-[#71717a] underline underline-offset-2 hover:text-[#0c0c0e]"
        >
          Close
        </button>
      </div>

      {/* Prior-utterance pull-quote (editorial pattern — one quote, lime border) */}
      {lastUser && (
        <p className="mt-3 border-l-2 border-lime-600 pl-3 text-sm italic text-[#71717a] line-clamp-3">
          {lastUser.content}
        </p>
      )}

      {/* Agent reply — capped at text-xl: a sub-surface, never the page title. */}
      <p className="font-display mt-3 max-w-prose text-xl leading-snug text-[#0c0c0e]" aria-live="polite">
        {lastAgent?.content}
      </p>

      {thinking && (
        <p className="mt-2 flex items-center gap-2 text-sm text-[#71717a]">
          <span className="inline-block h-1.5 w-1.5 rounded-full bg-lime-600 motion-safe:animate-ping" />
          Thinking it through…
        </p>
      )}

      {error && (
        <div className="mt-2 rounded border border-zinc-200 bg-white px-3 py-2 text-sm text-[#3f3f46]">
          {error}
        </div>
      )}

      {/* Re-read offer (the recourse with teeth) */}
      {suggestedNote && noteTargetPath && (
        <div className="mt-3 rounded-lg border border-lime-200 bg-lime-50 p-3">
          <p className="text-sm text-lime-800">
            Re-read the clip with this context? <em>&ldquo;{suggestedNote}&rdquo;</em>
          </p>
          <div className="mt-2 flex gap-3">
            <button
              type="button"
              disabled={applyingNote}
              onClick={applySuggestedNote}
              className="text-sm font-medium text-lime-700 underline underline-offset-2 hover:text-lime-800 disabled:opacity-50"
            >
              {applyingNote ? "Saving…" : "Yes — re-read it"}
            </button>
            <button
              type="button"
              onClick={() => setSuggestedNote("")}
              className="text-sm text-[#71717a] underline underline-offset-2 hover:text-[#0c0c0e]"
            >
              No thanks
            </button>
          </div>
        </div>
      )}

      {/* Suggestion chips */}
      {!thinking && suggestions.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {suggestions.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => send(s)}
              className="rounded-full border border-zinc-200 px-3 py-1.5 text-xs text-[#3f3f46] transition-colors hover:border-lime-600 hover:text-lime-700"
            >
              {s}
            </button>
          ))}
        </div>
      )}

      {/* Input — part of the panel, never viewport-sticky */}
      <form
        className="mt-3 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void send(input);
        }}
      >
        <label className="sr-only" htmlFor="ask-nova-input">
          Tell Nova about your clips
        </label>
        <input
          id="ask-nova-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Tell Nova about your clips…"
          disabled={thinking}
          className="min-h-11 w-full rounded-full border border-zinc-200 bg-white px-4 text-sm text-[#0c0c0e] placeholder:text-[#a1a1aa] focus:border-lime-600 focus:outline-none"
        />
        <button
          type="submit"
          disabled={thinking || !input.trim()}
          aria-label="Send"
          className="min-h-11 shrink-0 rounded-full bg-[#0c0c0e] px-5 text-sm font-medium text-white transition-opacity hover:opacity-80 disabled:opacity-30"
        >
          →
        </button>
      </form>
    </div>
  );
}
