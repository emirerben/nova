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

// Map raw footage-type keys to human-readable labels
const FOOTAGE_LABELS: Record<string, string> = {
  broll: "B-roll",
  action: "Action shots",
  talking_head: "Talking to camera",
  ambience: "Ambience & mood",
};

export function StyleCard({ style, status, styleSetPreview, fontPreview }: StyleCardProps) {
  if (status === "absent") {
    return (
      <LightCard className="px-6 py-5">
        <Eyebrow tone="muted">Your style</Eyebrow>
        <p className="mt-3 text-[13px] text-[#a1a1aa]">
          <Link
            href="/plan/style"
            className="text-[#3f3f46] underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
          >
            Set up your style
          </Link>
        </p>
      </LightCard>
    );
  }

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
  const styleLabel =
    styleSetPreview?.label ??
    style?.style_set_id ??
    null;
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

  return (
    <LightCard className="px-6 py-5">
      <div className="flex items-start justify-between">
        <Eyebrow tone="muted">Your style</Eyebrow>
        <Link
          href="/plan/style"
          className="text-[13px] text-[#71717a] underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
        >
          Edit
        </Link>
      </div>

      {/* Look */}
      {styleLabel && (
        <div className="mt-3">
          <p className="mb-1.5 text-[10px] font-medium uppercase tracking-wide text-[#a1a1aa]">Look</p>
          <span className="rounded-full border border-lime-200 bg-lime-50 px-3 py-1 text-[11px] font-medium text-lime-800">
            {styleLabel}
          </span>
        </div>
      )}

      {/* Font */}
      {fontName && (
        <div className="mt-3">
          <p className="mb-1.5 text-[10px] font-medium uppercase tracking-wide text-[#a1a1aa]">Font</p>
          <span
            className="rounded-full border border-zinc-200 bg-white px-3 py-1 text-[12px] text-[#3f3f46]"
            style={cssFamily ? { fontFamily: cssFamily } : undefined}
          >
            {fontName}
          </span>
        </div>
      )}

      {/* Colors */}
      {(textColor || highlightColor) && (
        <div className="mt-3">
          <p className="mb-1.5 text-[10px] font-medium uppercase tracking-wide text-[#a1a1aa]">Colors</p>
          <div className="flex flex-wrap gap-2">
            {textColor && (
              <span className="flex items-center gap-1.5 rounded-full border border-zinc-200 bg-white px-3 py-1 text-[11px] text-[#3f3f46]">
                <span className="inline-block h-3 w-3 rounded-full border border-zinc-200" style={{ background: textColor }} aria-hidden="true" />
                Text color
              </span>
            )}
            {highlightColor && (
              <span className="flex items-center gap-1.5 rounded-full border border-zinc-200 bg-white px-3 py-1 text-[11px] text-[#3f3f46]">
                <span className="inline-block h-3 w-3 rounded-full border border-zinc-200" style={{ background: highlightColor }} aria-hidden="true" />
                Highlight
              </span>
            )}
          </div>
        </div>
      )}

      {/* Video types */}
      {footageBias.length > 0 && (
        <div className="mt-3">
          <p className="mb-1.5 text-[10px] font-medium uppercase tracking-wide text-[#a1a1aa]">Your videos lean</p>
          <div className="flex flex-wrap gap-1.5">
            {footageBias.map((bias) => (
              <span
                key={bias}
                className="rounded-full border border-lime-200 bg-lime-50 px-3 py-1 text-[11px] font-medium text-lime-800"
              >
                {FOOTAGE_LABELS[bias] ?? bias}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Instruction level */}
      {instructionLevel && instructionLevel !== "full" && (
        <p className="mt-3 text-[11px] text-[#71717a]">
          {instructionLevel === "light" ? "Light guidance" : "No instructions"}
        </p>
      )}
    </LightCard>
  );
}
