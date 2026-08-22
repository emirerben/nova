"use client";

/**
 * AskKriaPanel — per-item "Ask Kria" filming advisor (dogfood feedback #2).
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
import { ArrowUp, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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

export interface AskKriaPanelProps {
  item: PlanItem;
  /** "contest" when opened via "Looks wrong? Tell Kria" on the verdict tile. */
  mode: "default" | "contest";
  onClose: () => void;
  /** Refetch the item after a note is applied (conformance re-runs). */
  onItemChanged: () => void;
}

export default function AskKriaPanel({ item, mode, onClose, onItemChanged }: AskKriaPanelProps) {
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
      // Contest mode marks the verdict contested exactly once (idempotent
      // server-side). Set the ref only AFTER the POST succeeds — otherwise a
      // failed contest is never retried and the backend never learns of it
      // (review finding).
      if (mode === "contest" && !contested.current) {
        contestConformance(item.id)
          .then(() => {
            contested.current = true;
          })
          .catch(() => {});
      }
      const res = await planItemAdvisorTurn(item.id, message, nextTurns.slice(0, -1));
      setTurns([...nextTurns, { role: "agent", content: res.reply }]);
      setSuggestions(res.suggestions.length > 0 ? res.suggestions : []);
      setSuggestedNote(res.suggested_note ?? "");
    } catch {
      setError("Kria couldn't think that through — try again.");
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
    <div className="mt-3 border-t border-border pt-4" data-testid="ask-kria-panel">
      <div className="flex items-baseline justify-between">
        <span className="text-sm font-semibold text-foreground">Ask Kria</span>
        <Button type="button" variant="ghost" size="icon" aria-label="Close" onClick={onClose}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      {/* Prior-utterance pull-quote (editorial pattern — one quote, lime border) */}
      {lastUser && (
        <p className="mt-3 border-l-2 border-lime-600 pl-3 text-sm italic text-muted-foreground line-clamp-3">
          {lastUser.content}
        </p>
      )}

      {/* Agent reply — capped at text-xl: a sub-surface, never the page title. */}
      <p className="font-display mt-3 max-w-prose text-xl leading-snug text-foreground" aria-live="polite">
        {lastAgent?.content}
      </p>

      {thinking && (
        <p className="mt-2 flex items-center gap-2 text-sm text-muted-foreground">
          <span className="inline-block h-1.5 w-1.5 rounded-full bg-lime-600 motion-safe:animate-ping" />
          Thinking it through…
        </p>
      )}

      {error && (
        <div className="mt-2 rounded border border-border bg-background px-3 py-2 text-sm text-foreground">
          {error}
        </div>
      )}

      {/* Re-read offer (the recourse with teeth) */}
      {suggestedNote && noteTargetPath && (
        <div className="mt-3 rounded-lg border border-lime-200 bg-lime-50 p-3">
          <p className="text-sm text-lime-800">
            Re-read the clip with this context? <em>&ldquo;{suggestedNote}&rdquo;</em>
          </p>
          <div className="mt-2 flex gap-1">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={applyingNote}
              onClick={applySuggestedNote}
              className="text-lime-700 hover:text-lime-800"
            >
              {applyingNote ? "Saving…" : "Yes — re-read it"}
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setSuggestedNote("")}
            >
              No thanks
            </Button>
          </div>
        </div>
      )}

      {/* Suggestion chips */}
      {!thinking && suggestions.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {suggestions.map((s) => (
            <Button
              key={s}
              type="button"
              variant="outline"
              size="sm"
              onClick={() => send(s)}
              className="hover:border-lime-400 hover:text-lime-700"
            >
              {s}
            </Button>
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
        <label className="sr-only" htmlFor="ask-kria-input">
          Tell Kria about your clips
        </label>
        <Input
          id="ask-kria-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Tell Kria about your clips…"
          disabled={thinking}
          className="rounded-full"
        />
        <Button
          type="submit"
          size="icon"
          disabled={thinking || !input.trim()}
          aria-label="Send"
          className="shrink-0 rounded-full"
        >
          <ArrowUp className="h-4 w-4" />
        </Button>
      </form>
    </div>
  );
}
