import { isInstantEditEligible } from "@/lib/variant-editor/eligibility";
import type { EditableVariant } from "@/lib/variant-editor/types";

function variant(overrides: Partial<EditableVariant> = {}): EditableVariant {
  return {
    variant_id: "v1",
    intro_text_size_px: null,
    style_set_id: null,
    text_mode: "none",
    render_status: "ready",
    output_url: "https://example.com/variant_1.mp4",
    base_video_url: "https://example.com/variant_1_base.mp4",
    ...overrides,
  };
}

describe("isInstantEditEligible", () => {
  it("is eligible for a montage agent_text variant with a base", () => {
    expect(isInstantEditEligible(variant({ text_mode: "agent_text" }))).toBe(true);
  });

  it("is eligible for a text-removed (none) montage variant with a base", () => {
    expect(isInstantEditEligible(variant({ text_mode: "none" }))).toBe(true);
  });

  it("is NOT eligible without a base video (lyrics / legacy)", () => {
    expect(isInstantEditEligible(variant({ base_video_url: null }))).toBe(false);
    expect(isInstantEditEligible(variant({ text_mode: "lyrics" }))).toBe(false);
  });

  it("is NOT eligible for sequence-synced intros", () => {
    expect(
      isInstantEditEligible(variant({ text_mode: "agent_text", intro_mode: "sequence" })),
    ).toBe(false);
  });

  // Regression: a narrated variant renders with text_mode "none" AND ships a
  // base video, so the original 3-clause guard returned `true` — which drove the
  // hero to LiveEditPreview (the caption-FREE base) instead of the burned,
  // captioned output. The user saw "no captions" and right-click-saved the base.
  it("is NOT eligible for narrated variants (captions edited elsewhere, hero is the burned output)", () => {
    expect(
      isInstantEditEligible(
        variant({ text_mode: "none", resolved_archetype: "narrated" }),
      ),
    ).toBe(false);
  });

  it("stays eligible for montage variants whose archetype is not narrated", () => {
    expect(
      isInstantEditEligible(
        variant({ text_mode: "agent_text", resolved_archetype: "original_text" }),
      ),
    ).toBe(true);
  });
});
