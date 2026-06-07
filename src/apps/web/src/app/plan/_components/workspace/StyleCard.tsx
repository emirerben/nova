import Link from "next/link";
import { LightCard } from "@/components/ui/LightCard";
import { Eyebrow } from "@/components/ui/Eyebrow";
import type { UserStyle, StyleResponse, StyleSetPreview, FontPreview } from "@/lib/plan-api";

interface StyleCardProps {
  style: UserStyle | null;
  status: StyleResponse["status"];
  styleSetPreview?: StyleSetPreview | null;
  fontPreview?: FontPreview | null;
}

export function StyleCard({ style, status, styleSetPreview, fontPreview }: StyleCardProps) {
  if (status === "absent") return null;

  if (status === "deriving") {
    return (
      <LightCard className="px-6 py-5">
        <Eyebrow tone="muted">Your style</Eyebrow>
        <div className="mt-3 space-y-2">
          <div className="h-3 w-40 animate-pulse rounded bg-zinc-100" />
          <div className="h-3 w-28 animate-pulse rounded bg-zinc-100" />
        </div>
        <p className="mt-3 text-[13px] text-[#a1a1aa]">Learning your style…</p>
      </LightCard>
    );
  }

  if (status === "failed") {
    return (
      <LightCard className="px-6 py-5">
        <Eyebrow tone="muted">Your style</Eyebrow>
        <p className="mt-3 text-[13px] text-[#a1a1aa]">Style unavailable</p>
      </LightCard>
    );
  }

  // status === "ready" | "edited"
  const fontName =
    fontPreview?.display_name ??
    styleSetPreview?.font_family ??
    style?.knobs?.font_family ??
    null;
  const cssFamily =
    fontPreview?.css_family ?? styleSetPreview?.css_family ?? null;
  const textColor =
    styleSetPreview?.text_color ?? style?.knobs?.text_color ?? null;
  const highlightColor =
    styleSetPreview?.highlight_color ?? style?.knobs?.highlight_color ?? null;
  const instructionLevel = style?.instruction_level ?? null;
  const footageBias = style?.footage_type_bias ?? [];

  // Style-set label from the style_set_id on UserStyle
  const styleSetLabel = style?.style_set_id ?? null;

  return (
    <LightCard className="px-6 py-5">
      <Eyebrow tone="muted">Your style</Eyebrow>

      <div className="mt-3 flex flex-wrap gap-2">
        {/* Style-set label pill */}
        {styleSetLabel && (
          <span className="truncate rounded-full border border-lime-200 bg-lime-50 px-3 py-1 text-[11px] font-medium text-lime-800">
            {styleSetLabel}
          </span>
        )}

        {/* Font name pill rendered in its REAL typeface (mirror StyleChip css_family trick) */}
        {fontName && (
          <span
            className="truncate rounded-full border border-zinc-200 bg-white px-3 py-1 text-[12px]"
            style={cssFamily ? { fontFamily: cssFamily } : undefined}
          >
            {fontName}
          </span>
        )}

        {/* text_color swatch */}
        {textColor && (
          <span className="flex items-center gap-1.5 rounded-full border border-zinc-200 bg-white px-3 py-1 text-[11px] text-[#3f3f46]">
            <span
              className="inline-block h-3 w-3 rounded-full border border-zinc-200"
              style={{ background: textColor }}
              aria-hidden="true"
            />
            Text
          </span>
        )}

        {/* highlight_color swatch */}
        {highlightColor && (
          <span className="flex items-center gap-1.5 rounded-full border border-zinc-200 bg-white px-3 py-1 text-[11px] text-[#3f3f46]">
            <span
              className="inline-block h-3 w-3 rounded-full border border-zinc-200"
              style={{ background: highlightColor }}
              aria-hidden="true"
            />
            Highlight
          </span>
        )}

        {/* instruction_level chip */}
        {instructionLevel && instructionLevel !== "full" && (
          <span className="truncate rounded-full border border-zinc-200 bg-white px-3 py-1 text-[11px] text-[#3f3f46]">
            {instructionLevel === "light" ? "Light guidance" : "No instructions"}
          </span>
        )}
      </div>

      {/* footage_type_bias chips */}
      {footageBias.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {footageBias.map((bias) => (
            <span
              key={bias}
              className="truncate rounded-full border border-lime-200 bg-lime-50 px-3 py-1 text-[11px] font-medium text-lime-800"
            >
              {bias}
            </span>
          ))}
        </div>
      )}

      <Link
        href="/plan/style"
        className="mt-4 inline-block text-[11px] text-[#a1a1aa] hover:text-[#3f3f46] transition-colors"
      >
        Edit →
      </Link>
    </LightCard>
  );
}
