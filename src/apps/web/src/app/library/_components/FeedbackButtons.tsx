"use client";

import { useState } from "react";
import {
  sendFeedback,
  clearFeedback,
  type FeedbackSignal,
} from "@/lib/me-api";

/**
 * Per-video feedback: three mutually-exclusive reactions + an optional note.
 * Reactions steer future generation (the feedback loop) — they never overwrite a
 * hand-edited plan day. Text labels, not emoji-as-UI (design guardrail); the
 * selected reaction is the one amber accent on the tile.
 */
const REACTIONS: { signal: FeedbackSignal; label: string }[] = [
  { signal: "up", label: "Like" },
  { signal: "more_like_this", label: "More like this" },
  { signal: "down", label: "Not for me" },
];

export default function FeedbackButtons({
  jobId,
  initialSignal,
}: {
  jobId: string;
  initialSignal: FeedbackSignal | null;
}) {
  const [signal, setSignal] = useState<FeedbackSignal | null>(initialSignal);
  // The id of a reaction WE wrote this session — lets us toggle it back off.
  // null after a reload even when a reaction shows (the list omits the id).
  const [feedbackId, setFeedbackId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [noteOpen, setNoteOpen] = useState(false);
  const [note, setNote] = useState("");
  const [noteSaved, setNoteSaved] = useState(false);

  async function react(next: FeedbackSignal) {
    if (busy) return;
    setBusy(true);
    setError(null);
    const prev = signal;
    try {
      if (next === prev && feedbackId) {
        // Toggle the same reaction back off.
        await clearFeedback(feedbackId);
        setSignal(null);
        setFeedbackId(null);
      } else if (next === prev) {
        // Selected from a prior load (no id to clear) — leave it as-is.
      } else {
        // Optimistic: the server's one-thumb rule replaces any prior reaction.
        setSignal(next);
        const res = await sendFeedback({ signal: next, jobId });
        setFeedbackId(res.id);
      }
    } catch (err) {
      setSignal(prev);
      setError(err instanceof Error ? err.message : "Couldn't save");
    } finally {
      setBusy(false);
    }
  }

  async function submitNote() {
    const text = note.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    try {
      await sendFeedback({ signal: "note", jobId, note: text });
      setNote("");
      setNoteOpen(false);
      setNoteSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't save note");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-2">
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Rate this video">
        {REACTIONS.map((r) => {
          const active = signal === r.signal;
          return (
            <button
              key={r.signal}
              type="button"
              onClick={() => void react(r.signal)}
              disabled={busy}
              aria-pressed={active}
              className={`min-h-11 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-60 ${
                active
                  ? "border-amber-400 text-amber-300"
                  : "border-zinc-700 text-zinc-300 hover:border-zinc-400 hover:text-white"
              }`}
            >
              {r.label}
            </button>
          );
        })}
        <button
          type="button"
          onClick={() => {
            setNoteOpen((v) => !v);
            setNoteSaved(false);
          }}
          disabled={busy}
          aria-expanded={noteOpen}
          className="min-h-11 rounded-full border border-zinc-700 px-3 py-1.5 text-xs font-medium text-zinc-300 transition-colors hover:border-zinc-400 hover:text-white disabled:opacity-60"
        >
          {noteSaved ? "Note saved" : "Add note"}
        </button>
      </div>

      {noteOpen && (
        <div className="mt-2">
          <label className="block">
            <span className="sr-only">Tell the AI what you think of this video</span>
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              rows={2}
              placeholder="What worked, or what you'd change…"
              className="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-200 placeholder:text-zinc-600"
            />
          </label>
          <button
            type="button"
            onClick={() => void submitNote()}
            disabled={busy || !note.trim()}
            className="mt-1.5 min-h-11 rounded-full bg-amber-400 px-4 py-1.5 text-xs font-semibold text-zinc-950 transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {busy ? "Saving…" : "Send"}
          </button>
        </div>
      )}

      {error && <p className="mt-1 text-xs text-red-400">{error}</p>}
    </div>
  );
}
