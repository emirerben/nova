/**
 * Type-level contract: PlanItemVariant must stay structurally assignable to the
 * shared EditableVariant so the instant editor (useVariantEditSession +
 * resolveIntroParams) drives plan-item variants without a surface-specific type.
 *
 * This is compile-time only — if a PlanItemVariant field drifts from
 * EditableVariant (e.g. render_status widened back to bare `string`, or a
 * required field made optional), `tsc --noEmit` fails on the assignment below.
 * (The plan item page's `useVariantEditSession(variant, …)` call enforces the
 * same thing at runtime-callsite; this keeps the contract explicit + local.)
 */

import type { PlanItemVariant } from "@/lib/plan-api";
import type { EditableVariant } from "@/lib/variant-editor/types";

// If PlanItemVariant is not assignable to EditableVariant, this errors.
const _assignable = (v: PlanItemVariant): EditableVariant => v;

test("PlanItemVariant is assignable to EditableVariant (compile-time)", () => {
  const v: PlanItemVariant = {
    variant_id: "song_text",
    output_url: null,
    render_status: "ready",
    text_mode: "agent_text",
    style_set_id: "default",
    intro_text_size_px: null,
    base_video_url: "https://cdn/base.mp4",
  };
  const editable = _assignable(v);
  expect(editable.variant_id).toBe("song_text");
});
