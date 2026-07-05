/**
 * PARITY_VERIFIED_FIELDS — the registry that gates editor inspector controls
 * (plan decision D9/D17).
 *
 * A TextElement field may have an EDITABLE inspector row only once it renders
 * identically in the CSS preview (lib/overlay-layout.ts) and the Skia reburn
 * (app/pipeline/generative_overlays.py), proven by:
 *   1. shared-fixture layout-contract tests (TS resolveTextElementsLayout vs
 *      Python overlay geometry, equal within tolerance),
 *   2. extended Skia render verification (`make verify-overlays`),
 *   3. a documented per-field visual spot-check in the PR.
 *
 * Fields present in saved data but NOT in this registry render READ-ONLY in
 * the inspector — the editor never hides state it preserves, and never offers
 * a control the burn won't honor (no documented-caveat lies).
 *
 * When a new style field (weight / italic / underline / text_case / spacing /
 * background / shadow) passes the gate, add it here in the same PR as its
 * parity tests — the inspector row ungates automatically.
 */

/** Currently-verified base fields: the TextElement fields both renderers
 * honor today (see the renderer-parity invariant in CLAUDE.md). */
export const PARITY_VERIFIED_FIELDS = [
  "text",
  "start_s",
  "end_s",
  "position",
  "x_frac",
  "y_frac",
  "font_family",
  "size_px",
  "size_class",
  "color",
  "highlight_color",
  "stroke_width",
  "alignment",
  "effect",
  // ── T11 gated style fields ──────────────────────────────────────────────
  // Each entry below passed the D17 gate in the same PR that added it:
  // shared fixture tests/fixtures/text-element-parity/<field>.json, asserted
  // by test_text_element_parity_contract.py (burn dict) AND
  // text-element-parity-contract.test.ts (TS layout), plus Skia render
  // verification in test_text_overlay_skia_style_fields.py.
  "text_case", // tests/fixtures/text-element-parity/text_case.json
  "letter_spacing", // tests/fixtures/text-element-parity/letter_spacing.json
  "line_spacing", // tests/fixtures/text-element-parity/line_spacing.json
  "max_width_frac", // tests/fixtures/text-element-parity/max_width_frac.json
] as const;

export type ParityVerifiedField = (typeof PARITY_VERIFIED_FIELDS)[number];

const _verified: ReadonlySet<string> = new Set(PARITY_VERIFIED_FIELDS);

export function isParityVerified(field: string): boolean {
  return _verified.has(field);
}

/**
 * Plumbing fields that are never rendered as inspector rows (identifiers,
 * provenance, renderer-internal timing detail). Everything else that carries
 * a value but has no editable row shows as a read-only row.
 */
export const INSPECTOR_INTERNAL_FIELDS: ReadonlySet<string> = new Set([
  "id",
  "role",
  "source_params",
  "word_timings",
  "z",
  // Timeline-owned (edited on the timeline bars, not in the inspector):
  "start_s",
  "end_s",
  // Canvas-owned (edited by drag/scale on the canvas):
  "x_frac",
  "y_frac",
  "position",
  // Resolved into size_px by the size control:
  "size_class",
]);
