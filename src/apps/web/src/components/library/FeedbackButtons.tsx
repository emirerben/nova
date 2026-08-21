"use client";

import { useState } from "react";
import {
  sendFeedback,
  clearFeedback,
  type FeedbackSignal,
} from "@/lib/me-api";

/**
 * Per-video feedback: three mutually-exclusive reactions + an optional note.
 * Light editorial canvas (D20/D21) — both /library and /plan/items now use
 * the light system so this component converts outright (no tone prop).
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
        await clearFeedback(feedbackId);
        setSignal(null);
        setFeedbackId(null);
      } else if (next === prev) {
        // Selected from a prior load (no id to clear) — leave it.
      } else {
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
                  ? "border-lime-300 text-lime-700"
                  : "border-zinc-200 text-[#3f3f46] hover:border-zinc-400"
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
          className="min-h-11 rounded-full border border-zinc-200 px-3 py-1.5 text-xs font-medium text-[#3f3f46] transition-colors hover:border-zinc-400 disabled:opacity-60"
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
              className="w-full rounded-lg border border-zinc-200 bg-white px-2 py-1.5 text-sm text-[#3f3f46] placeholder:text-[#a1a1aa]"
            />
          </label>
          <button
            type="button"
            onClick={() => void submitNote()}
            disabled={busy || !note.trim()}
            className="mt-1.5 inline-flex items-center justify-center min-h-11 rounded-full bg-[#0c0c0e] px-6 py-[11px] text-xs font-semibold text-white transition-opacity hover:opacity-80 disabled:opacity-40"
          >
            {busy ? "Saving…" : "Send"}
          </button>
        </div>
      )}

      {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
    </div>
  );
}
