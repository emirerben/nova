/**
 * Per-frame CSS pose for one carousel card. Pure math (no React/DOM) —
 * independently unit testable.
 *
 * DESIGN DECISION (documented divergence from the Python renderer): rather
 * than porting `renderer.py:project_card_corners` (which projects each
 * card's 4 corners through a manual perspective-matrix and warps the face
 * image onto that quad via `setPolyToPoly` — necessary there because Skia
 * has no native CSS 3D engine) this component sets a real `perspective` +
 * `transform-style: preserve-3d` on the container and lets the BROWSER do
 * the actual 3D projection, applying `rotateY()/translateZ()/scale()`
 * transform functions directly to each card `<div>`. This is closer to
 * `tools/carousel_reference/*.html` (the pages the Python math was FIT to
 * in the first place) than to the Python port, and is exact for the
 * non-focused pose (same transform functions, same composition order —
 * see `composeTransform`'s docstring).
 *
 * For the FOCUSED card's zoom-to-fullscreen pose, the Python renderer
 * linearly interpolates the 4 PROJECTED SCREEN-SPACE CORNERS between the
 * card's normal quad and the full-canvas quad (`renderer.py`'s
 * `render_choreography_frames`, the `is_focus` branch). This component
 * instead lerps the CSS TRANSFORM VALUES themselves (translate/scale/
 * rotateY/translateZ) toward a "fill the canvas" pose — cheaper, avoids
 * re-deriving a projection matrix from 4 arbitrary points every frame, and
 * visually equivalent for this geometry (a flat rectangle, not a deep
 * mesh). The two lerps are NOT pixel-identical at intermediate `focusT`
 * values (corner-lerp is linear in screen space, transform-value-lerp is
 * linear in a different parameterization that the browser then projects) —
 * they agree exactly at `focusT` = 0 and 1 and stay visually close between.
 * Accepted per the approved design; flag if a future frame-by-frame parity
 * test against the Python renderer is added for this path.
 */

import type { CardGeometry, CardTransform, EffectName, FrameState } from "@/lib/carousel-preview";
import { CANVAS_H, CANVAS_W, transformFor } from "@/lib/carousel-preview";

// Mirrors app/pipeline/carousel/renderer.py:SHADOW_DY_PX — keep in sync.
export const SHADOW_DY_PX = 18.0;
// Mirrors app/pipeline/carousel/renderer.py:SHADOW_SIGMA_PX — keep in sync.
export const SHADOW_SIGMA_PX = 24.0;

// Always above any effect's own zIndex (viewTimelineZIndex peaks at 1000) —
// mirrors renderer.py's "the focused card is ALWAYS drawn last (on top)".
export const FOCUSED_Z_INDEX = 10000;

const CANVAS_CENTER_X = CANVAS_W / 2;
const CANVAS_CENTER_Y = CANVAS_H / 2;

function round(value: number, decimals = 4): number {
  const factor = 10 ** decimals;
  return Math.round(value * factor) / factor;
}

/**
 * Compose the CSS `transform` string for one card, honoring
 * `CardTransform.rotateBeforeTranslate` (see `types.ts`'s docstring on that
 * field for the CSS-composes-right-to-left rationale): cover_flow/
 * scale_sweep/cards_stack use `rotateY() translateZ() scale()`; flipbook
 * uses `translateZ() rotateY()`. `scale()` is always innermost (rightmost)
 * here, including for the focus lerp — a leading `translate(dx, dy)` (used
 * only by the focused card, to recenter it) is prepended OUTSIDE all of
 * that so it moves the card by exactly `dx`/`dy` real pixels regardless of
 * how much `scale()` shrinks/grows it (translate is unaffected by an INNER
 * scale in CSS's transform-function composition). Omitted entirely when
 * `dx === dy === 0` so a non-focused card's string, and a focused card's
 * string at `focusT === 0`, are byte-identical — no seam at the pose's own
 * continuous limit.
 */
export function composeTransform(
  rotateBeforeTranslate: boolean,
  dxPx: number,
  dyPx: number,
  rotateYDeg: number,
  translateZPx: number,
  scaleX: number,
  scaleY: number,
): string {
  const parts: string[] = [];
  if (dxPx !== 0 || dyPx !== 0) {
    parts.push(`translate(${round(dxPx)}px, ${round(dyPx)}px)`);
  }
  if (rotateBeforeTranslate) {
    parts.push(`translateZ(${round(translateZPx)}px)`);
    parts.push(`rotateY(${round(rotateYDeg)}deg)`);
  } else {
    parts.push(`rotateY(${round(rotateYDeg)}deg)`);
    parts.push(`translateZ(${round(translateZPx)}px)`);
  }
  parts.push(`scale(${round(scaleX)}, ${round(scaleY)})`);
  return parts.join(" ");
}

export interface ResolvedCardStyle {
  left: number;
  top: number;
  width: number;
  height: number;
  transform: string;
  zIndex: number;
  opacity: number;
  boxShadow: string;
  borderRadius: number;
  /** 0..1 black-wash overlay opacity to render as a sibling div — mirrors
   * renderer.py's flat-alpha `dim` approximation of `filter: brightness()`. */
  dim: number;
  isFocused: boolean;
}

/**
 * Resolve one card's full CSS pose for one frame.
 *
 * `progressScrollX` vs `positionScrollX`: the same one-frame LAG split
 * `renderer.py:lagged_frame_scroll_x` documents — the view-timeline-driven
 * VISUAL transform (scale/rotate/opacity/shadow/zIndex, everything
 * `effects.transformFor` derives from view progress) reads the PRECEDING
 * frame's scroll position; the card's LAYOUT position (`x`/`left`) reads
 * THIS frame's own scroll position. Passing the same value for both is
 * harmless (just skips the lag) — callers that don't need parity with the
 * captured-browser-trace quirk may do that.
 */
export function cardStyleFor(
  effect: EffectName,
  cardIndex: number,
  fstate: FrameState,
  progressScrollX: number,
  positionScrollX: number,
  geo: CardGeometry,
  viewportW: number = CANVAS_W,
): ResolvedCardStyle {
  const t: CardTransform = transformFor(effect, progressScrollX, cardIndex, geo, viewportW, {
    positionScrollX,
  });

  const isFocused = fstate.focusCard === cardIndex && fstate.focusT > 0;

  if (!isFocused) {
    const otherCardFocused = fstate.focusCard != null && fstate.focusCard !== cardIndex;
    return {
      left: t.x,
      top: t.y,
      width: geo.cardW,
      height: geo.cardH,
      transform: composeTransform(t.rotateBeforeTranslate, 0, 0, t.rotateYDeg, t.translateZPx, t.scale, t.scale),
      zIndex: t.zIndex,
      opacity: t.opacity,
      boxShadow:
        t.shadowAlpha > 0
          ? `0 ${SHADOW_DY_PX}px ${SHADOW_SIGMA_PX}px rgba(0, 0, 0, ${round(t.shadowAlpha)})`
          : "none",
      borderRadius: geo.cornerRadius,
      dim: otherCardFocused ? fstate.dim : 0,
      isFocused: false,
    };
  }

  const ft = Math.max(0, Math.min(1, fstate.focusT));
  const cx = t.x + geo.cardW / 2;
  const cy = t.y + geo.cardH / 2;
  const dx = (CANVAS_CENTER_X - cx) * ft;
  const dy = (CANVAS_CENTER_Y - cy) * ft;
  const targetScaleX = viewportW / geo.cardW;
  const targetScaleY = CANVAS_H / geo.cardH;
  const scaleX = t.scale + (targetScaleX - t.scale) * ft;
  const scaleY = t.scale + (targetScaleY - t.scale) * ft;
  const rotateYDeg = t.rotateYDeg * (1 - ft);
  const translateZPx = t.translateZPx * (1 - ft);

  return {
    left: t.x,
    top: t.y,
    width: geo.cardW,
    height: geo.cardH,
    transform: composeTransform(t.rotateBeforeTranslate, dx, dy, rotateYDeg, translateZPx, scaleX, scaleY),
    // Fullscreen never casts a shadow / is never dimmed — mirrors
    // renderer.py: "the focused card ... skips its own shadow/opacity/dim".
    zIndex: FOCUSED_Z_INDEX,
    opacity: 1,
    boxShadow: "none",
    borderRadius: geo.cornerRadius * (1 - ft),
    dim: 0,
    isFocused: true,
  };
}
