/**
 * overlayCardStyle — the ONE place that turns a media-overlay envelope into
 * CSS positioning for a card rendered over a 9:16 video box (plan 007 code
 * quality spec: three call sites, zero copies).
 *
 * Consumed by:
 *   - the Hero's instant CSS preview stack (plan/items/[id]/page.tsx)
 *   - SuggestionRail's in-card 9:16 mini-preview
 *   - HeroOverlayEditor's direct-manipulation cards (plan 007 Fix 2)
 *
 * CONTAINER-BOX == CONTENT-BOX ASSUMPTION (pinned): this percent math is only
 * correct because every consumer renders inside an `aspect-[9/16]` container
 * whose box exactly matches the video content area (output is 1080×1920, also
 * 9:16 — so `object-contain` never letterboxes and container percentages equal
 * frame fractions). If a consumer ever renders a non-9:16 container (or the
 * output aspect changes), x_frac/y_frac/scale would need remapping from the
 * content box instead. HeroOverlayEditor carries a dev-time assert for this.
 *
 * Plan 009 (fullscreen cutaways): display_mode === "fullscreen" branches to
 * full-frame positioning (all four insets 0, no translate, no width%) —
 * mirroring the FFmpeg builder's cover-crop takeover
 * (`scale=1080:1920:force_original_aspect_ratio=increase,crop` + `overlay=0:0`)
 * exactly. x_frac/y_frac/scale are intentionally IGNORED here but preserved on
 * the envelope so toggling back to pip restores the prior layout. Parity is
 * pinned by the two-test guard: the python builder test on display_mode and
 * the jest "parity: style util branches on display_mode like the ffmpeg
 * builder" test on this function.
 */

import type { CSSProperties } from "react";
import type { MediaOverlay } from "@/lib/plan-api";

export function overlayCardStyle(
  overlay: Pick<MediaOverlay, "x_frac" | "y_frac" | "scale" | "display_mode">,
): CSSProperties {
  if (overlay.display_mode === "fullscreen") {
    return {
      position: "absolute",
      left: 0,
      top: 0,
      right: 0,
      bottom: 0,
    };
  }
  return {
    position: "absolute",
    left: `${overlay.x_frac * 100}%`,
    top: `${overlay.y_frac * 100}%`,
    transform: "translate(-50%, -50%)",
    width: `${overlay.scale * 100}%`,
  };
}
