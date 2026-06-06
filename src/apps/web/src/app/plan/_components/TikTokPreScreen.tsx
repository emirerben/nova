"use client";

import { useState } from "react";

/**
 * One-field pre-screen before the chat interview.
 * Accepts a TikTok handle (@username or full URL), fires the async scrape,
 * then calls onContinue whether or not the scrape succeeds — it's best-effort.
 * "Skip →" jumps straight to the chat without scraping.
 */
export default function TikTokPreScreen({
  onContinue,
  submitting,
}: {
  /**
   * Called with the raw handle string (may be empty when the user skips).
   * The caller is responsible for firing POST /personas/tiktok-scrape when
   * handle is non-empty.
   */
  onContinue: (handle: string) => void;
  submitting?: boolean;
}) {
  const [handle, setHandle] = useState("");

  function submit() {
    if (submitting) return;
    onContinue(handle.trim());
  }

  return (
    <div className="animate-fade-up py-4">
      <p className="mb-1 text-xs font-medium uppercase tracking-wide text-amber-300">
        Getting to know you
      </p>
      <h1 className="font-display text-3xl leading-snug text-white">
        Are you a TikTok creator?
      </h1>
      <p className="mt-2 text-zinc-400">
        Drop your handle and we&apos;ll skip straight to the interesting questions.
      </p>

      <div className="mt-6">
        <input
          type="text"
          value={handle}
          onChange={(e) => setHandle(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              submit();
            }
          }}
          placeholder="@username or vm.tiktok.com/…"
          aria-label="Your TikTok handle"
          autoComplete="off"
          spellCheck={false}
          className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-4 py-3 text-lg text-white placeholder-zinc-600 transition-colors focus:border-amber-400/60 focus:outline-none"
        />
      </div>

      <div className="mt-4 flex items-center gap-4">
        <button
          type="button"
          onClick={submit}
          disabled={submitting || !handle.trim()}
          className="inline-flex min-h-[44px] items-center rounded-full bg-white px-5 py-2 text-sm font-medium text-black transition-colors hover:bg-zinc-200 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-400"
        >
          {submitting ? "Looking up…" : "Continue →"}
        </button>

        <button
          type="button"
          onClick={() => onContinue("")}
          disabled={submitting}
          className="py-3 text-sm text-zinc-500 transition-colors hover:text-zinc-300 disabled:opacity-50"
        >
          Skip →
        </button>
      </div>
    </div>
  );
}
