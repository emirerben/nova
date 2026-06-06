"use client";

import { useState } from "react";
import { sendFeedback } from "@/lib/me-api";

/**
 * The plan-level "Tell the AI" steer box. A free-text note the user can leave any
 * time about what they want more or less of across their plan. It is captured
 * passively (stored as a note); the user applies it with "Regenerate plan with my
 * feedback" — capture is cheap, re-tuning is the deliberate second step.
 */
export default function SteerInput({ contentPlanId }: { contentPlanId: string }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    const text = note.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await sendFeedback({ signal: "note", contentPlanId, note: text });
      setNote("");
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-4">
      <p className="text-xs font-semibold uppercase tracking-wide text-[#a1a1aa]">Steer</p>
      <label className="mt-2 block">
        <span className="sr-only">Tell the AI what&apos;s working and what to change</span>
        <textarea
          value={note}
          onChange={(e) => {
            setNote(e.target.value);
            setSaved(false);
          }}
          rows={2}
          placeholder="Tell the AI what's working — more travel, less talking-head, punchier hooks…"
          className="w-full resize-none rounded-lg border border-zinc-200 bg-[#fafaf8] px-3 py-2 text-sm text-[#3f3f46] placeholder:text-zinc-400 focus:border-lime-600/60 focus:outline-none"
        />
      </label>
      <div className="mt-2 flex items-center gap-3">
        <button
          type="button"
          onClick={() => void submit()}
          disabled={busy || !note.trim()}
          className="min-h-11 rounded-full bg-[#0c0c0e] px-4 py-1.5 text-xs font-semibold text-white transition-opacity hover:opacity-80 disabled:opacity-40"
        >
          {busy ? "Saving…" : "Save for next time"}
        </button>
        {saved && (
          <span className="text-xs text-lime-700" aria-live="polite">
            Got it — I&apos;ll use this when you regenerate.
          </span>
        )}
        {error && <span className="text-xs text-red-600">{error}</span>}
      </div>
    </div>
  );
}
