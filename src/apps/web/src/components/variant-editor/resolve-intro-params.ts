/**
 * Merge a variant + its (draft-selected) style set + the edit draft into the
 * IntroOverlayParams the client preview renders — the browser mirror of
 * `_resolve_intro_overlay_params` (generative_build.py).
 *
 * Precedence (most-specific wins), matching the server resolver minus
 * user_style_knobs (dark-launched, job-level — not exposed on the variant):
 *   draft (text/size) > variant persisted values > style set intro role > defaults.
 *
 * Defaults mirror generative_overlays.py: effect "karaoke-line" (agent_form
 * default), position "center", #FFFFFF text, #FFD24A highlight, center anchor.
 *
 * Surface-agnostic: takes the shared `EditableVariant`, so both the generative
 * page and the plan flow resolve previews through one copy.
 */

import type { GenerativeStyleSet } from "@/lib/generative-api";
import type { IntroOverlayParams } from "@/lib/overlay-layout";
import type { EditableVariant } from "@/lib/variant-editor/types";
import type { EditDraft } from "@/lib/variant-editor/useVariantEditSession";

export function resolveIntroParams(
  variant: EditableVariant,
  styleSets: GenerativeStyleSet[],
  draft: EditDraft,
): IntroOverlayParams {
  const setId = draft.styleSetId ?? variant.style_set_id ?? null;
  const intro = styleSets.find((s) => s.id === setId)?.intro ?? null;

  return {
    text: draft.removed ? "" : draft.text,
    effect: intro?.effect ?? "karaoke-line",
    textColor: intro?.text_color ?? "#FFFFFF",
    highlightColor: intro?.highlight_color ?? "#FFD24A",
    fontFamily: intro?.font_family ?? null,
    textSizePx: draft.sizePx ?? variant.intro_text_size_px ?? intro?.text_size_px ?? null,
    position: intro?.position ?? "center",
    positionXFrac: intro?.position_x_frac ?? null,
    positionYFrac: intro?.position_y_frac ?? null,
    textAnchor: (intro?.text_anchor === "left" || intro?.text_anchor === "right"
      ? intro.text_anchor
      : "center") as "left" | "right" | "center",
    strokeWidth: intro?.stroke_width ?? null,
  };
}
