"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

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
      <p className="mb-1 text-xs font-medium uppercase tracking-wide text-lime-700">
        Getting to know you
      </p>
      <h1 className="font-display text-3xl leading-snug text-[#0c0c0e]">
        Are you a TikTok creator?
      </h1>

      <div className="mt-6">
        <Input
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
          className="h-auto w-full rounded-lg border-zinc-200 bg-white px-4 py-3 text-lg text-[#0c0c0e] placeholder-zinc-400 transition-colors focus-visible:border-lime-600/60 focus-visible:ring-0"
        />
      </div>

      <div className="mt-4 flex items-center gap-4">
        <Button
          type="button"
          onClick={submit}
          disabled={submitting || !handle.trim()}
          className="h-auto min-h-[44px] items-center rounded-full bg-[#0c0c0e] px-9 py-[15px] text-[15px] font-semibold text-white transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {submitting ? "Looking up…" : "Continue →"}
        </Button>

        <Button
          type="button"
          variant="ghost"
          onClick={() => onContinue("")}
          disabled={submitting}
          className="h-auto px-4 py-2 text-sm text-[#71717a] transition-colors hover:bg-transparent hover:underline underline-offset-4 disabled:opacity-50"
        >
          Skip →
        </Button>
      </div>
    </div>
  );
}
