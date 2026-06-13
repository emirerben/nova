"use client";

import { Eyebrow } from "@/components/ui/Eyebrow";

export function ForkScreen({
  onFootage,
  onFresh,
  onSkip,
}: {
  onFootage: () => void;
  onFresh: () => void;
  onSkip: () => void;
}) {
  return (
    <div className="flex flex-col items-center gap-6 px-4 py-8 max-w-lg mx-auto animate-fade-up">
      <Eyebrow tone="lime">First edit</Eyebrow>
      <h1 className="font-display text-3xl text-[#0c0c0e] text-center leading-tight">
        Let&apos;s make your first edit
      </h1>

      {/* Primary: footage card */}
      <button
        onClick={onFootage}
        className="w-full rounded-2xl border-2 border-lime-700 bg-[#fafaf8] p-6 text-left hover:bg-lime-50 transition focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 min-h-[44px]"
      >
        <p className="font-display text-xl text-[#0c0c0e] mb-1">
          I have footage to use
        </p>
        <p className="text-sm text-[#71717a]">
          Upload clips from your camera roll → get a share-ready edit in ~90s
        </p>
        {/* thumbnail strip hint */}
        <div className="mt-3 flex gap-1.5 opacity-60">
          {[...Array(4)].map((_, i) => (
            <div
              key={i}
              className="w-12 h-16 rounded bg-lime-100 border border-lime-200"
            />
          ))}
        </div>
      </button>

      {/* Secondary: fresh text link */}
      <button
        onClick={onFresh}
        className="text-sm text-[#71717a] underline underline-offset-2 hover:text-[#0c0c0e] focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 rounded"
      >
        or start from a blank slate
      </button>

      {/* Tertiary: skip */}
      <button
        onClick={onSkip}
        className="text-xs text-[#a1a1aa] hover:text-[#71717a] focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 rounded"
      >
        skip, just make something
      </button>
    </div>
  );
}
