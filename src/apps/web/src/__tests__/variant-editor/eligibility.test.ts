import {
  isCaptionArchetype,
  isInstantEditEligible,
  isTextLaneEligible,
} from "@/lib/variant-editor/eligibility";
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

  // Subtitled single-clip is a caption archetype too: captions are edited via the
  // on-video CaptionEditor and the hero must play the burned, captioned output —
  // NOT the caption-free base. Same exclusion as narrated.
  it("is NOT eligible for subtitled variants (captions edited via CaptionEditor)", () => {
    expect(
      isInstantEditEligible(
        variant({ text_mode: "none", resolved_archetype: "subtitled" }),
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

describe("isCaptionArchetype", () => {
  it("is true for narrated and subtitled with a base video", () => {
    expect(isCaptionArchetype(variant({ resolved_archetype: "narrated" }))).toBe(true);
    expect(isCaptionArchetype(variant({ resolved_archetype: "subtitled" }))).toBe(true);
  });

  // Mirrors the backend _is_editable_caption_variant contract: a base-less
  // caption variant must NOT be classified as editable — routing it to the
  // Captions tab would open a CaptionEditor with nothing to render.
  it("requires a base video", () => {
    expect(
      isCaptionArchetype(variant({ resolved_archetype: "subtitled", base_video_url: null })),
    ).toBe(false);
    expect(
      isCaptionArchetype(variant({ resolved_archetype: "narrated", base_video_url: null })),
    ).toBe(false);
  });

  it("is false for non-caption and missing archetypes", () => {
    expect(isCaptionArchetype(variant({ resolved_archetype: "original_text" }))).toBe(false);
    expect(isCaptionArchetype(variant({ resolved_archetype: null }))).toBe(false);
    expect(isCaptionArchetype(variant())).toBe(false); // resolved_archetype undefined
  });
});

describe("isTextLaneEligible", () => {
  const oldFlag = process.env.NEXT_PUBLIC_SUBTITLED_TEXT_LANE_ENABLED;

  afterEach(() => {
    process.env.NEXT_PUBLIC_SUBTITLED_TEXT_LANE_ENABLED = oldFlag;
  });

  it("keeps montage text variants eligible", () => {
    expect(isTextLaneEligible(variant({ text_mode: "agent_text" }))).toBe(true);
    expect(isTextLaneEligible(variant({ text_mode: "none" }))).toBe(true);
    expect(isTextLaneEligible(variant({ text_mode: "lyrics" }))).toBe(false);
  });

  it("gates subtitled text lane on the frontend flag and base video", () => {
    const subtitled = variant({ text_mode: "none", resolved_archetype: "subtitled" });

    process.env.NEXT_PUBLIC_SUBTITLED_TEXT_LANE_ENABLED = "false";
    expect(isTextLaneEligible(subtitled)).toBe(false);

    process.env.NEXT_PUBLIC_SUBTITLED_TEXT_LANE_ENABLED = "true";
    expect(isTextLaneEligible(subtitled)).toBe(true);
    expect(isTextLaneEligible({ ...subtitled, base_video_url: null })).toBe(false);
  });
});
