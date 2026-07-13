import type { PlanItemVariant } from "@/lib/plan-api";
import type { CopilotCaptionMetaSnapshot } from "@/lib/edit-copilot/snapshot";

const FRAME_HEIGHT_PX = 1920;
const SUBTITLED_DEFAULT_BOTTOM_CQH = 20;
const NARRATED_DEFAULT_BOTTOM_CQH = 9.4;

export function captionPreviewBottomCqh(variant: PlanItemVariant): number {
  if (variant.caption_margin_v != null) {
    return (variant.caption_margin_v / FRAME_HEIGHT_PX) * 100;
  }
  return variant.resolved_archetype === "subtitled"
    ? SUBTITLED_DEFAULT_BOTTOM_CQH
    : NARRATED_DEFAULT_BOTTOM_CQH;
}

export function captionMetaFromVariant(variant: PlanItemVariant): CopilotCaptionMetaSnapshot {
  return {
    enabled: variant.captions_enabled ?? true,
    style: variant.voiceover_caption_style === "word" ? "word" : "sentence",
    font: variant.voiceover_caption_font ?? null,
    y_frac: 1 - captionPreviewBottomCqh(variant) / 100,
  };
}
