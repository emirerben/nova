/**
 * Pure (no-canvas) projection of one card's 4 face corners onto the
 * 1080x1920 canvas. Port of `project_card_corners` +the parity-tuning
 * constants from `app/pipeline/carousel/renderer.py` — ONLY that function
 * and its constants are ported here; the rest of renderer.py (Skia frame
 * compositing) is out of scope for the editor's live preview.
 *
 * Replicates CSS `perspective(1200px)` (perspective-origin = viewport
 * center (540, 960)) combined with a per-card
 * `transform: rotateY(deg) translateZ(px) scale(s)` (transform-origin =
 * card center).
 */

import type { CardGeometry, CardTransform } from "./types";

// Mirrors app/pipeline/carousel/renderer.py:CANVAS_W — keep in sync (golden
// trace test pins this).
export const CANVAS_W = 1080;
// Mirrors app/pipeline/carousel/renderer.py:CANVAS_H — keep in sync (golden
// trace test pins this).
export const CANVAS_H = 1920;

// CSS `perspective: 1200px` on the carousel container. Mirrors
// app/pipeline/carousel/renderer.py:PERSPECTIVE_PX.
export const PERSPECTIVE_PX = 1200.0;
// CSS `perspective-origin` = viewport center for a 1080x1920 canvas. Mirrors
// app/pipeline/carousel/renderer.py:PERSPECTIVE_ORIGIN.
export const PERSPECTIVE_ORIGIN: readonly [number, number] = [CANVAS_W / 2.0, CANVAS_H / 2.0];

/**
 * Corner order is [top-left, top-right, bottom-right, bottom-left] — this
 * MUST match the face-image source-quad order the Skia renderer's
 * `setPolyToPoly` consumes on the Python side.
 *
 * Model (CSS composes a `transform` list innermost-first from the RIGHT end
 * of the list: `transform: A B` means B applies to the point first, then A
 * applies to the result — so `rotateY(...) translateZ(...) scale(...)`
 * (cover_flow's keyframes) apply scale, then translateZ, then rotateY LAST;
 * `translateZ(...) rotateY(...)` (flipbook's keyframes) apply rotateY
 * FIRST, then translateZ. `t.rotateBeforeTranslate` picks which):
 *   - Card center on screen (pre-3D): cx = t.x + geo.cardW/2,
 *     cy = t.y + geo.cardH/2.
 *   - Corner offsets from card center after scale:
 *     (+/- geo.cardW/2 * t.scale, +/- geo.cardH/2 * t.scale).
 *   - theta = radians(t.rotateYDeg), z0 = t.translateZPx.
 *     translateZ-then-rotateY (default, rotateBeforeTranslate=false):
 *       x1 = x0*cos(theta) + z0*sin(theta)
 *       z1 = -x0*sin(theta) + z0*cos(theta)
 *     rotateY-then-translateZ (rotateBeforeTranslate=true): rotateY first
 *     sees z=0, so its z0-cross-terms drop out; translateZ then adds its
 *     raw (unrotated) z0 on top:
 *       x1 = x0*cos(theta)
 *       z1 = -x0*sin(theta) + z0
 *     y1 = y0 either way (rotateY doesn't touch Y).
 *   - Perspective projection (d = PERSPECTIVE_PX, origin = (540, 960)):
 *       X = (cx - 540) + x1; Y = (cy - 960) + y1; Z = z1
 *       f = d / (d - Z)   (denominator floored to 1.0 to guard Z >= d,
 *                           which would otherwise flip or blow up the sign)
 *       screen = (540 + X*f, 960 + Y*f)
 *
 * Mirrors app/pipeline/carousel/renderer.py:project_card_corners.
 */
export function projectCardCorners(
  t: CardTransform,
  geo: CardGeometry,
): Array<[number, number]> {
  const cx = t.x + geo.cardW / 2.0;
  const cy = t.y + geo.cardH / 2.0;
  const halfW = (geo.cardW / 2.0) * t.scale;
  const halfH = (geo.cardH / 2.0) * t.scale;

  const theta = (t.rotateYDeg * Math.PI) / 180;
  const cosT = Math.cos(theta);
  const sinT = Math.sin(theta);
  const z0 = t.translateZPx;
  const [originX, originY] = PERSPECTIVE_ORIGIN;

  const cornersLocal: Array<[number, number]> = [
    [-halfW, -halfH], // top-left
    [halfW, -halfH], // top-right
    [halfW, halfH], // bottom-right
    [-halfW, halfH], // bottom-left
  ];

  const projected: Array<[number, number]> = [];
  for (const [x0, y0] of cornersLocal) {
    let x1: number;
    let z1: number;
    if (t.rotateBeforeTranslate) {
      x1 = x0 * cosT;
      z1 = -x0 * sinT + z0;
    } else {
      x1 = x0 * cosT + z0 * sinT;
      z1 = -x0 * sinT + z0 * cosT;
    }
    const y1 = y0;

    const bigX = cx - originX + x1;
    const bigY = cy - originY + y1;
    const z = z1;

    // Guard Z < d: clamp the denominator away from zero/negative so a card
    // pushed past the camera plane never flips or blows up to infinity.
    const denom = Math.max(PERSPECTIVE_PX - z, 1.0);
    const f = PERSPECTIVE_PX / denom;

    projected.push([originX + bigX * f, originY + bigY * f]);
  }

  return projected;
}
