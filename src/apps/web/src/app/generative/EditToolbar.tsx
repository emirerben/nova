"use client";

/**
 * Instant-edit controls for one variant: style chips (rendered in their REAL
 * typeface), a size slider, remove-text toggle, Done/Cancel. Every control
 * mutates the local draft only — ZERO network until "Done" commits the whole
 * session as one /edit request.
 */

import {
  INTRO_SIZE_MAX,
  INTRO_SIZE_MIN,
  type GenerativeStyleSet,
} from "@/lib/generative-api";
import type { VariantEditSession } from "./useVariantEditSession";

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

  return (
    <div className="mt-3 space-y-3">
      {styleSets.length > 0 && (
        <div className="flex flex-wrap gap-1.5" role="listbox" aria-label="Text style">
          {styleSets.map((s) => {
            const selected = (draft.styleSetId ?? "") === s.id;
            const chipFamily = s.intro?.css_family ?? s.css_family ?? undefined;
            const chipWeight = s.intro?.font_weight ?? s.font_weight ?? undefined;
            return (
              <button
                key={s.id}
                role="option"
                aria-selected={selected}
                onClick={() => session.setStyle(s.id)}
                className={
                  selected
                    ? "rounded-full border border-[#0c0c0e] bg-[#0c0c0e] px-3 py-1 text-xs text-white"
                    : "rounded-full border border-zinc-200 bg-white px-3 py-1 text-xs text-[#3f3f46] hover:border-zinc-400"
                }
                style={{ fontFamily: chipFamily, fontWeight: chipWeight }}
              >
                {s.label}
              </button>
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
