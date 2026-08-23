"use client";

import { Button } from "@/components/ui/button";
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
        Let&apos;s make your first video
      </h1>

      {/* Primary: footage card */}
      <Button
        type="button"
        variant="outline"
        onClick={onFootage}
        className="flex h-auto min-h-[44px] w-full flex-col items-start justify-start gap-0 rounded-2xl border-2 border-lime-700 bg-[#ffffff] p-6 text-left hover:bg-lime-50 focus-visible:ring-lime-600"
      >
        <p className="font-display text-xl text-[#0c0c0e] mb-1">
          Use footage I already have
        </p>
        <p className="text-sm text-[#71717a]">
          Upload clips from your camera roll. Kria will build a share-ready first video.
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
      </Button>

      {/* Secondary: fresh text link */}
      <Button
        type="button"
        variant="link"
        onClick={onFresh}
        className="h-auto p-0 text-sm text-[#71717a] underline underline-offset-2 hover:text-[#0c0c0e] focus-visible:ring-lime-600 rounded"
      >
        Start with an idea
      </Button>

      {/* Tertiary: skip */}
      <Button
        type="button"
        variant="ghost"
        onClick={onSkip}
        className="h-auto p-0 text-xs text-[#a1a1aa] hover:bg-transparent hover:text-[#71717a] focus-visible:ring-lime-600 rounded"
      >
        Skip for now
      </Button>
    </div>
  );
}
