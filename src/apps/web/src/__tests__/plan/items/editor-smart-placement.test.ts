import { resolveSmartPlacementCandidate } from "@/app/plan/items/[id]/_editor/editor-smart-placement";
import type { PlanItemVariant, TextPlacementCandidate } from "@/lib/plan-api";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

const bar: TextElementBar = {
  id: "text-1",
  role: "generative_intro",
  text: "hello",
  start_s: 0,
  end_s: 2,
  x_frac: 0.5,
  y_frac: 0.4,
  position: "custom",
  size_px: 64,
};

function variant(overrides: Partial<PlanItemVariant> = {}): PlanItemVariant {
  return {
    variant_id: "song_text",
    output_url: "https://example.com/out.mp4",
    render_status: "ready",
    text_mode: "agent_text",
    music_track_id: "track-1",
    style_set_id: null,
    intro_text_size_px: null,
    ...overrides,
  } as PlanItemVariant;
}

describe("resolveSmartPlacementCandidate", () => {
  it("uses server placement candidates when present", () => {
    const candidate: TextPlacementCandidate = {
      source: "masonry_whitespace",
      x_frac: 0.22,
      y_frac: 0.33,
      max_width_frac: 0.44,
    };

    expect(
      resolveSmartPlacementCandidate(
        variant({ text_placement_candidates: [candidate] }),
        bar,
      ),
    ).toBe(candidate);
  });

  it("falls back for existing masonry variants generated before candidates existed", () => {
    expect(
      resolveSmartPlacementCandidate(
        variant({ montage_preset_rendered: "masonry", text_placement_candidates: null }),
        bar,
      ),
    ).toMatchObject({
      source: "editor_fallback_masonry",
      x_frac: 0.5,
      max_width_frac: 0.68,
    });
  });

  it("stays unavailable until text is selected", () => {
    expect(resolveSmartPlacementCandidate(variant(), null)).toBeNull();
  });
});
