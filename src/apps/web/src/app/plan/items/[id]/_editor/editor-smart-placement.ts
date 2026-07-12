import type { PlanItemVariant, TextPlacementCandidate } from "@/lib/plan-api";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

const DEFAULT_SMART_PLACE: TextPlacementCandidate = {
  source: "editor_fallback",
  x_frac: 0.5,
  y_frac: 0.18,
  max_width_frac: 0.72,
  confidence: 0.35,
};

const MASONRY_FALLBACK_SMART_PLACE: TextPlacementCandidate = {
  source: "editor_fallback_masonry",
  x_frac: 0.5,
  y_frac: 0.16,
  max_width_frac: 0.68,
  confidence: 0.4,
};

export function resolveSmartPlacementCandidate(
  variant: PlanItemVariant | null | undefined,
  selectedBar: TextElementBar | null | undefined,
): TextPlacementCandidate | null {
  if (!selectedBar) return null;
  const serverCandidate = variant?.text_placement_candidates?.[0] ?? null;
  if (serverCandidate) return serverCandidate;
  return variant?.montage_preset === "masonry" || variant?.montage_preset_rendered === "masonry"
    ? MASONRY_FALLBACK_SMART_PLACE
    : DEFAULT_SMART_PLACE;
}
