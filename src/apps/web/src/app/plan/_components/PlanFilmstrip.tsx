"use client";

import { cn } from "@/lib/cn";
import type { PlanItemVariant } from "@/lib/plan-api";

const TEXT_MODE_LABEL: Record<string, string> = {
  lyrics: "Lyrics",
  agent_text: "AI text",
  none: "No text",
};

/**
 * Horizontal strip of variant thumbnails. Clicking (or arrow-keying) one makes
 * it the focused hero. A `radiogroup` with roving tabindex so keyboard users can
 * move between variants with ← / →. A re-rendering thumb shows a spinner badge;
 * a failed one a ⚠ badge.
 */
export default function PlanFilmstrip({
  variants,
  focusedId,
  onFocus,
}: {
  variants: PlanItemVariant[];
  focusedId: string | null;
  onFocus: (variantId: string) => void;
}) {
  function onKeyDown(e: React.KeyboardEvent, index: number) {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const delta = e.key === "ArrowRight" ? 1 : -1;
    const next = variants[(index + delta + variants.length) % variants.length];
    if (next) onFocus(next.variant_id);
  }

  return (
    <div
      role="radiogroup"
      aria-label="Choose a video variant"
      className="flex gap-2 overflow-x-auto pb-1"
    >
      {variants.map((v, i) => {
        const selected = v.variant_id === focusedId;
        const rendering = v.render_status === "rendering";
        const failed = v.render_status === "failed";
        const label = TEXT_MODE_LABEL[v.text_mode ?? ""] ?? "Edit";
        return (
          <button
            key={v.variant_id}
            type="button"
            role="radio"
            aria-checked={selected}
            aria-label={`${label} — ${v.track_title ?? "original audio"}`}
            tabIndex={selected ? 0 : -1}
            onClick={() => onFocus(v.variant_id)}
            onKeyDown={(e) => onKeyDown(e, i)}
            className={cn(
              "relative aspect-[9/16] w-16 shrink-0 overflow-hidden rounded-md border bg-black transition-colors",
              selected ? "border-amber-400 ring-1 ring-amber-400" : "border-zinc-700 hover:border-zinc-500",
            )}
          >
            {v.output_url ? (
              <video src={v.output_url} muted preload="metadata" className="h-full w-full object-cover" />
            ) : (
              <div className="h-full w-full bg-zinc-900" />
            )}
            <span className="absolute inset-x-0 bottom-0 truncate bg-black/60 px-1 py-0.5 text-[9px] text-zinc-200">
              {label}
            </span>
            {rendering && (
              <span className="absolute inset-0 flex items-center justify-center bg-black/50 text-[10px] text-amber-300">
                …
              </span>
            )}
            {failed && <span className="absolute right-0.5 top-0.5 text-[10px]">⚠</span>}
          </button>
        );
      })}
    </div>
  );
}
