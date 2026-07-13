import {
  reflowTextForSmartPlacement,
  resolveSmartPlacementCandidate,
  resolveSmartPlacementCandidates,
  smartPlacementPatchForBar,
} from "@/app/plan/items/[id]/_editor/editor-smart-placement";
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
      x_frac: expect.any(Number),
      y_frac: expect.any(Number),
      rotation_deg: 90,
      masonry_motion: expect.objectContaining({ mode: "masonry_pan_x" }),
    });
    const candidate = resolveSmartPlacementCandidate(
      variant({ montage_preset_rendered: "masonry", text_placement_candidates: null }),
      bar,
    );
    expect(candidate?.x_frac).toBeLessThan(0.25);
    expect(candidate?.y_frac).toBeGreaterThan(0.45);
  });

  it("stays unavailable until text is selected", () => {
    expect(resolveSmartPlacementCandidate(variant(), null)).toBeNull();
  });

  it("returns multiple masonry pockets for harmony placement", () => {
    const candidates = resolveSmartPlacementCandidates(
      variant({ montage_preset_rendered: "masonry", text_placement_candidates: null }),
      [bar, { ...bar, id: "text-2" }],
    );

    expect(candidates).toHaveLength(3);
    expect(candidates[0]).toMatchObject({ rotation_deg: 90 });
    expect(candidates[1]?.y_frac).toBeLessThan(0.12);
  });

  it("extends short legacy server masonry candidate lists with fallback pockets", () => {
    const candidates = resolveSmartPlacementCandidates(
      variant({
        montage_preset_rendered: "masonry",
        text_placement_candidates: [
          { source: "masonry_whitespace", x_frac: 0.82, y_frac: 0.85, max_width_frac: 0.2 },
        ],
      }),
      [bar, { ...bar, id: "text-2" }],
    );

    expect(candidates).toHaveLength(3);
    expect(candidates[0]).toMatchObject({ x_frac: 0.82 });
    expect(candidates[1]).toMatchObject({ rotation_deg: 90 });
  });

  it("reflows longer copy into a compact stack for narrow smart placements", () => {
    expect(
      reflowTextForSmartPlacement("find your favorite hidden swimming spot", {
        source: "masonry_whitespace",
        x_frac: 0.86,
        y_frac: 0.9,
        max_width_frac: 0.2,
      }),
    ).toBe("find your\nfavorite hidden\nswimming spot");
  });

  it("leaves explicit line breaks alone", () => {
    expect(
      reflowTextForSmartPlacement("already\nstacked", {
        source: "masonry_whitespace",
        x_frac: 0.86,
        y_frac: 0.9,
        max_width_frac: 0.2,
      }),
    ).toBe("already\nstacked");
  });

  it("keeps rotated masonry text as one line", () => {
    expect(
      reflowTextForSmartPlacement("already\nstacked pocket label", {
        source: "masonry_whitespace",
        x_frac: 0.15,
        y_frac: 0.5,
        max_width_frac: 0.64,
        rotation_deg: 90,
      }),
    ).toBe("already stacked pocket label");
  });

  it("builds a smart placement patch with rotation and motion metadata", () => {
    const patch = smartPlacementPatchForBar(bar, {
      source: "masonry_whitespace",
      x_frac: 0.15,
      y_frac: 0.5,
      max_width_frac: 0.64,
      rotation_deg: 90,
      masonry_motion: { mode: "masonry_pan_x", pan_px: 932 },
    });

    expect(patch).toMatchObject({
      x_frac: 0.15,
      y_frac: 0.5,
      max_width_frac: 0.64,
      rotation_deg: 90,
      position: "custom",
      source_params: {
        masonry_motion: { mode: "masonry_pan_x", pan_px: 932 },
      },
    });
  });
});
