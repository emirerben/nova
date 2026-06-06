"use client";

import { cn } from "@/lib/cn";
import type { GenerativeStyleSet } from "@/lib/generative-api";

/**
 * One selectable text-style option, rendered in the style's REAL typeface +
 * color so the user previews the look before committing a re-render. The font
 * comes from `css_family` (matched to a `@font-face` in lib/font-faces.ts); if
 * the API didn't supply typography (older build) or the font 404s, it falls
 * back to the page font via `font-display: swap` — the chip still works, it just
 * shows the label in the default face.
 *
 * `role="radio"` — render inside a `role="radiogroup"` parent for keyboard + SR.
 */
export default function StyleChip({
  styleSet,
  selected,
  disabled,
  sampleText,
  onSelect,
}: {
  styleSet: GenerativeStyleSet;
  selected: boolean;
  disabled?: boolean;
  /** Preview copy — the user's caption if known, else the style label. */
  sampleText?: string;
  onSelect: () => void;
}) {
  const preview = (sampleText?.trim() || styleSet.label || "Aa").slice(0, 22);
  // css_family already includes its fallback list, so it works directly as a
  // CSS `fontFamily` value.
  const fontFamily = styleSet.css_family ?? undefined;
  const color = styleSet.text_color ?? "#FFFFFF";

  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      aria-label={`Text style: ${styleSet.label}`}
      disabled={disabled}
      onClick={onSelect}
      className={cn(
        "flex min-w-[7rem] max-w-[12rem] flex-col gap-1 rounded-lg border px-3 py-2 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-40",
        selected
          ? "border-lime-600 bg-white ring-1 ring-lime-600"
          : "border-zinc-200 bg-white hover:border-zinc-400",
      )}
    >
      <span
        className="truncate text-lg leading-tight"
        style={{ fontFamily, color, fontWeight: styleSet.font_weight ?? undefined }}
      >
        {preview}
      </span>
      <span className="truncate text-[11px] text-[#71717a]">{styleSet.label}</span>
    </button>
  );
}
