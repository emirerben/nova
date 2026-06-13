"use client";

/**
 * Instant-edit controls for one variant: style chips (rendered in their REAL
 * typeface over the user's CURRENT draft text), a size slider, remove-text
 * toggle, Done/Cancel. Every control mutates the local draft only — ZERO
 * network until "Done" commits the whole session as one /edit request.
 *
 * Shared by the generative page and (later lane) the plan flow.
 */

import { useRef } from "react";
import {
  INTRO_SIZE_MAX,
  INTRO_SIZE_MIN,
  type GenerativeStyleSet,
} from "@/lib/generative-api";
import StyleChip from "@/components/ui/StyleChip";
import type { VariantEditSession } from "@/lib/variant-editor/useVariantEditSession";

export function EditToolbar({
  session,
  styleSets,
  fallbackSizePx,
}: {
  session: VariantEditSession;
  styleSets: GenerativeStyleSet[];
  /** Slider position when the draft has no explicit size yet. */
  fallbackSizePx: number | null;
}) {
  const { draft } = session;
  const sliderPx = draft.sizePx ?? fallbackSizePx ?? 60;
  // The chip sample is the user's live hook text (so they preview their OWN
  // copy in each typeface), trimmed; StyleChip falls back to the style label
  // when empty. Removed-text drafts have nothing to preview → label fallback.
  const sample = draft.removed ? "" : draft.text;

  const chipRefs = useRef<Array<HTMLButtonElement | null>>([]);

  // Arrow-key roving focus across the radiogroup (W7 a11y).
  const onChipKeyDown = (e: React.KeyboardEvent, index: number) => {
    if (styleSets.length === 0) return;
    let next: number | null = null;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (index + 1) % styleSets.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp")
      next = (index - 1 + styleSets.length) % styleSets.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = styleSets.length - 1;
    if (next === null) return;
    e.preventDefault();
    const target = chipRefs.current[next];
    target?.focus();
    session.setStyle(styleSets[next].id);
  };

  return (
    <div className="mt-3 space-y-3">
      {styleSets.length > 0 && (
        <div
          role="radiogroup"
          aria-label="Text style"
          className="flex flex-wrap gap-2"
        >
          {styleSets.map((s, i) => {
            const selected = (draft.styleSetId ?? "") === s.id;
            return (
              <div
                key={s.id}
                ref={(el) => {
                  // StyleChip is the focusable radio; reach it through the wrapper.
                  chipRefs.current[i] =
                    (el?.querySelector("button") as HTMLButtonElement | null) ?? null;
                }}
                onKeyDown={(e) => onChipKeyDown(e, i)}
              >
                <StyleChip
                  styleSet={s}
                  selected={selected}
                  sampleText={sample}
                  darkTile
                  onSelect={() => session.setStyle(s.id)}
                />
              </div>
            );
          })}
        </div>
      )}

      {!draft.removed && (
        <div>
          <div className="mb-1 flex items-center justify-between text-xs text-[#71717a]">
            <label htmlFor="intro-size-slider">Text size</label>
            <span className="tabular-nums">{sliderPx}px</span>
          </div>
          <input
            id="intro-size-slider"
            type="range"
            min={INTRO_SIZE_MIN}
            max={INTRO_SIZE_MAX}
            step={1}
            value={sliderPx}
            aria-label="Intro text size"
            onChange={(e) => session.setSize(Number(e.target.value))}
            className="w-full accent-lime-600"
          />
        </div>
      )}

      {session.commitError && (
        <p className="text-xs text-red-600" role="alert">
          {session.commitError}
        </p>
      )}

      <div className="flex items-center justify-between">
        <button
          onClick={() => session.setRemoved(!draft.removed)}
          className="rounded border border-zinc-200 px-2 py-1 text-xs text-[#3f3f46] hover:border-zinc-400"
        >
          {draft.removed ? "Add text back" : "Remove text"}
        </button>
        <div className="flex gap-2">
          <button
            onClick={session.cancel}
            className="rounded border border-zinc-200 px-3 py-1 text-xs text-[#3f3f46] hover:border-zinc-400"
          >
            Cancel
          </button>
          <button
            onClick={() => void session.commit()}
            disabled={!session.isDirty}
            className="rounded bg-[#0c0c0e] px-4 py-1 text-xs font-medium text-white disabled:opacity-40"
          >
            Done
          </button>
        </div>
      </div>
    </div>
  );
}
