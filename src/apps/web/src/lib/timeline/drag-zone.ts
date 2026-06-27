/**
 * Drag-zone primitives: hit-testing (left / right / body), client-X → fraction
 * conversion, and seconds clamping.
 *
 * Generalises the independent drag implementations in:
 *   - app/generative/TimelineEditor.tsx  (HANDLE_PX=12, classifyZone)
 *   - app/plan/_components/MediaOverlayEditor.tsx  (DragState / startDrag)
 */

export type DragZone = "left" | "right" | "body";

/**
 * Convert a pointer clientX to a [0, 1] fraction within a bounding rect.
 * Clamps to [0, 1] — out-of-bounds pointers clamp to the edge.
 */
export function clientXToFrac(clientX: number, rect: DOMRect): number {
  if (rect.width === 0) return 0;
  return Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
}

/**
 * Classify a pointer position within a bar element as left-handle, right-handle,
 * or body.
 *
 * @param clientX   - pointer event clientX
 * @param rect      - bounding rect of the bar element (from getBoundingClientRect)
 * @param handlePx  - hit-zone width for each edge handle (default 12 — mirrors
 *                    TimelineEditor's HANDLE_PX)
 */
export function classifyZone(
  clientX: number,
  rect: DOMRect,
  handlePx = 12,
): DragZone {
  const x = clientX - rect.left;
  // Degenerate bar (too narrow for two handles): always body to avoid jitter.
  if (rect.width <= handlePx * 2) return "body";
  if (x <= handlePx) return "left";
  if (x >= rect.width - handlePx) return "right";
  return "body";
}

/**
 * Clamp a seconds value to [0, maxS], rounded to `step` to avoid
 * floating-point drift during drag.
 *
 * Default step: 0.05 s — enough precision for human editing, avoids noise.
 */
export function clampSeconds(value: number, maxS: number, step = 0.05): number {
  const raw = Math.max(0, Math.min(value, maxS));
  return Math.round(raw / step) * step;
}
