import {
  allocateSmartPlacementCandidates,
  masonryBoardXFrac,
  masonryLayerPositionForBoardX,
  masonryMotionOffsetFrac,
  reflowTextForSmartPlacement,
  resolveSmartPlacementAssignments,
  resolveSmartPlacementCandidate,
  resolveSmartPlacementCandidates,
  splitTextForSmartPlacement,
  smartPlacementCandidateFitsBar,
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
  it("uses server placement candidates for non-masonry variants", () => {
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

  it("discovers safe pockets for masonry variants generated before candidates existed", () => {
    const candidate = resolveSmartPlacementCandidate(
      variant({ montage_preset_rendered: "masonry", text_placement_candidates: null }),
      bar,
      8,
    );
    expect(candidate).toMatchObject({
      source: "masonry_whitespace",
      x_frac: 0.5861,
      y_frac: 0.6625,
      max_width_frac: 0.2266,
      rotation_deg: 0,
      masonry_motion: expect.objectContaining({ mode: "masonry_pan_x", duration_s: 8 }),
    });
  });

  it("stays unavailable until text is selected", () => {
    expect(resolveSmartPlacementCandidate(variant(), null)).toBeNull();
  });

  it("matches backend canonical masonry pockets", () => {
    const candidates = resolveSmartPlacementCandidates(
      variant({ montage_preset_rendered: "masonry", text_placement_candidates: null }),
      [bar, { ...bar, id: "text-2" }],
      8,
    );

    expect(
      candidates.map(({ x_frac, y_frac, max_width_frac, rotation_deg }) => [
        x_frac,
        y_frac,
        max_width_frac,
        rotation_deg,
      ]),
    ).toEqual([
      [0.5861, 0.6625, 0.2266, 0],
      [0.5, 0.9359, 0.3944, 0],
    ]);
    expect(candidates.map((candidate) => candidate.masonry_motion?.layer_origin_px)).toEqual([
      0,
      505.5,
    ]);
  });

  it("matches backend canonical Polaroid pockets and board motion", () => {
    const candidates = resolveSmartPlacementCandidates(
      variant({ montage_preset_rendered: "polaroid_wall", text_placement_candidates: null }),
      [{ ...bar, end_s: 8 }],
      15,
      2,
    );

    expect(candidates).toHaveLength(1);
    expect(candidates[0]).toMatchObject({
      source: "polaroid_wall_whitespace",
      x_frac: 0.775,
      y_frac: 0.9324,
      max_width_frac: 0.2615,
      rotation_deg: 0,
      masonry_motion: expect.objectContaining({
        board_width_px: 2366,
        pan_px: 1286,
        layer_origin_px: 0,
        pocket_left_px: 683.5,
        pocket_top_px: 1696.5,
        pocket_right_px: 990.5,
        pocket_bottom_px: 1884,
      }),
    });
  });

  it("discovers a genuinely empty rotated Polaroid pocket at the end of the pan", () => {
    const candidates = resolveSmartPlacementCandidates(
      variant({ montage_preset_rendered: "polaroid_wall" }),
      [{ ...bar, end_s: 15.1 }],
      15,
      15,
    );

    expect(candidates).toHaveLength(1);
    expect(candidates[0]).toMatchObject({
      source: "polaroid_wall_whitespace",
      x_frac: 0.8238,
      y_frac: 0.1603,
      max_width_frac: 0.4328,
      rotation_deg: 90,
      masonry_motion: expect.objectContaining({
        board_width_px: 2366,
        layer_origin_px: 1286,
        pocket_left_px: 2021.5,
        pocket_top_px: 36,
        pocket_right_px: 2330,
        pocket_bottom_px: 579.5,
      }),
    });
  });

  it("includes safe pockets that enter the viewport later in the board pan", () => {
    const candidates = resolveSmartPlacementCandidates(
      variant({ montage_preset_rendered: "masonry" }),
      [{ ...bar, end_s: 8 }],
      8,
      6,
    );
    const late = candidates.filter(
      (candidate) => Number(candidate.masonry_motion?.layer_origin_px ?? 0) > 0,
    );

    expect(late.length).toBeGreaterThan(0);
    expect(
      late.map((candidate) => [
        candidate.x_frac,
        candidate.y_frac,
        candidate.max_width_frac,
        candidate.rotation_deg,
        candidate.masonry_motion?.layer_origin_px,
      ]),
    ).toEqual([
      [0.5, 0.8021, 0.5479, 90, 854],
      [0.6505, 0.8641, 0.36, 90, 932],
    ]);
    expect(late.every((candidate) => candidate.x_frac > 0 && candidate.x_frac < 1)).toBe(true);
    expect(
      late.some((candidate) => {
        const motion = candidate.masonry_motion ?? {};
        return (
          Number(motion.layer_origin_px) +
            candidate.x_frac * Number(motion.frame_width_px) >
          Number(motion.frame_width_px)
        );
      }),
    ).toBe(true);
  });

  it("anchors selected-block placement to the playhead", () => {
    const candidate = resolveSmartPlacementCandidate(
      variant({ montage_preset_rendered: "masonry" }),
      { ...bar, end_s: 8 },
      8,
      6,
    );

    expect(candidate).toMatchObject({
      x_frac: 0.5,
      y_frac: 0.8021,
      masonry_motion: expect.objectContaining({ layer_origin_px: 854 }),
    });
  });

  it("rejects a selected-block pocket that cannot fit the text", () => {
    expect(
      resolveSmartPlacementCandidate(
        variant({ montage_preset_rendered: "masonry" }),
        { ...bar, text: "one two three four five six", end_s: 8 },
        8,
        6,
      ),
    ).toBeNull();
  });

  it("places each temporal group in pockets visible during that part of the pan", () => {
    const bars = [
      { ...bar, end_s: 3 },
      { ...bar, id: "text-2", start_s: 5, end_s: 8 },
    ];
    const assignments = resolveSmartPlacementAssignments(
      variant({ montage_preset_rendered: "masonry" }),
      bars,
      8,
      0,
    );

    expect(assignments).not.toBeNull();
    expect(assignments?.[0].masonry_motion?.layer_origin_px).toBe(0);
    expect(Number(assignments?.[1].masonry_motion?.layer_origin_px)).toBeGreaterThan(0);
  });

  it("honors an early active window instead of forcing placement to two seconds", () => {
    const earlyBar = { ...bar, end_s: 1 };
    const assignments = resolveSmartPlacementAssignments(
      variant({ montage_preset_rendered: "masonry" }),
      [earlyBar],
      8,
      0,
    );

    expect(assignments).not.toBeNull();
    expect(assignments?.[0].masonry_motion?.layer_origin_px).toBe(0);
  });

  it("keeps simultaneous late text in distinct revealed pockets", () => {
    const bars = [
      { ...bar, start_s: 5, end_s: 8 },
      { ...bar, id: "text-2", start_s: 5, end_s: 8 },
    ];
    const assignments = resolveSmartPlacementAssignments(
      variant({ montage_preset_rendered: "masonry" }),
      bars,
      8,
      6,
    );

    expect(assignments).not.toBeNull();
    expect(assignments?.map((candidate) => candidate.masonry_motion?.layer_origin_px)).toEqual([
      854,
      932,
    ]);
  });

  it("rejects persisted legacy curated masonry coordinates", () => {
    const candidates = resolveSmartPlacementCandidates(
      variant({
        montage_preset_rendered: "masonry",
        text_placement_candidates: [
          {
            source: "masonry_whitespace",
            x_frac: 0.1468,
            y_frac: 0.4979,
            max_width_frac: 0.6435,
            rotation_deg: 90,
          },
          {
            source: "masonry_whitespace",
            x_frac: 0.6379,
            y_frac: 0.0529,
            max_width_frac: 0.4873,
          },
        ],
      }),
      [bar, { ...bar, id: "text-2" }],
      8,
    );

    expect(candidates).toHaveLength(2);
    expect(candidates.map((candidate) => [candidate.x_frac, candidate.y_frac])).not.toContainEqual([
      0.1468,
      0.4979,
    ]);
    expect(candidates[0]).toMatchObject({ x_frac: 0.5861, y_frac: 0.6625 });
  });

  it("allocates distinct pockets to simultaneous bars", () => {
    const bars = [bar, { ...bar, id: "text-2" }];
    const candidates = resolveSmartPlacementCandidates(
      variant({ montage_preset_rendered: "masonry" }),
      bars,
      8,
    );

    expect(allocateSmartPlacementCandidates(bars, candidates)).toEqual(candidates);
  });

  it("reserves the only large pocket for the text that needs it", () => {
    const assignments = resolveSmartPlacementAssignments(
      variant({ montage_preset_rendered: "masonry" }),
      [
        { ...bar, text: "one", start_s: 5, end_s: 8 },
        { ...bar, id: "text-2", text: "one two three four", start_s: 5, end_s: 8 },
      ],
      8,
      6,
    );

    expect(assignments?.map((candidate) => candidate.masonry_motion?.layer_origin_px)).toEqual([
      932,
      854,
    ]);
  });

  it("reuses a pocket for non-overlapping bars", () => {
    const bars = [bar, { ...bar, id: "text-2", start_s: 2, end_s: 4 }];
    const candidate: TextPlacementCandidate = {
      source: "masonry_whitespace",
      x_frac: 0.8,
      y_frac: 0.8,
      max_width_frac: 0.2,
    };

    expect(allocateSmartPlacementCandidates(bars, [candidate])).toEqual([
      candidate,
      candidate,
    ]);
  });

  it("aborts when simultaneous bars exceed safe capacity", () => {
    const bars = [bar, { ...bar, id: "text-2" }];
    const candidate: TextPlacementCandidate = {
      source: "masonry_whitespace",
      x_frac: 0.8,
      y_frac: 0.8,
      max_width_frac: 0.2,
    };

    expect(allocateSmartPlacementCandidates(bars, [candidate])).toBeNull();
  });

  it("uses the same masonry pan expression as the final renderer", () => {
    const motion = {
      mode: "masonry_pan_x",
      duration_s: 8,
      pan_px: 932,
      board_width_px: 2012,
      frame_width_px: 1080,
    };

    expect(masonryMotionOffsetFrac(motion, 0)).toBe(0);
    expect(masonryMotionOffsetFrac(motion, 2)).toBeCloseTo(233 / 1080, 8);
    expect(masonryMotionOffsetFrac(motion, 99)).toBeCloseTo(932 / 1080, 8);
  });

  it("round-trips manual positions across later board-local text layers", () => {
    const motion = {
      mode: "masonry_pan_x",
      duration_s: 8,
      pan_px: 932,
      board_width_px: 2012,
      frame_width_px: 1080,
    };
    const localized = masonryLayerPositionForBoardX(motion, 1.4);
    const localizedMotion = { ...motion, layer_origin_px: localized.layerOriginPx };

    expect(localized.layerOriginPx).toBeGreaterThan(0);
    expect(localized.xFrac).toBeGreaterThan(0);
    expect(localized.xFrac).toBeLessThan(1);
    expect(masonryBoardXFrac(localizedMotion, localized.xFrac)).toBeCloseTo(1.4, 8);
  });

  it("rejects malformed or impossible masonry motion metadata", () => {
    expect(
      masonryMotionOffsetFrac(
        { mode: "masonry_pan_x", duration_s: "8", pan_px: 932, frame_width_px: 1080 },
        2,
      ),
    ).toBe(0);
    expect(
      masonryMotionOffsetFrac(
        {
          mode: "masonry_pan_x",
          duration_s: 8,
          pan_px: 933,
          board_width_px: 2012,
          frame_width_px: 1080,
        },
        2,
      ),
    ).toBe(0);
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

  it("rejects rotated text whose rendered footprint exceeds the vertical pocket", () => {
    const candidate: TextPlacementCandidate = {
      source: "masonry_whitespace",
      x_frac: 0.5,
      y_frac: 0.5,
      max_width_frac: 0.4,
      rotation_deg: 90,
      masonry_motion: {
        pocket_left_px: 400,
        pocket_top_px: 500,
        pocket_right_px: 520,
        pocket_bottom_px: 900,
      },
    };

    expect(smartPlacementCandidateFitsBar({ ...bar, text: "tall" }, candidate)).toBe(true);
    expect(
      smartPlacementCandidateFitsBar(
        { ...bar, text: "a label that is much too long for this pocket" },
        candidate,
      ),
    ).toBe(false);
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

  it("rejects copy that cannot fit a pocket at a readable size", () => {
    const candidate = resolveSmartPlacementCandidates(
      variant({ montage_preset_rendered: "masonry" }),
      [bar],
      8,
    )[0];
    const longBar = { ...bar, text: "find your favorite hidden swimming spot", size_px: 96 };

    expect(smartPlacementCandidateFitsBar(longBar, candidate)).toBe(false);
  });

  it("fits short copy without shrinking below the readable minimum", () => {
    const candidate = resolveSmartPlacementCandidates(
      variant({ montage_preset_rendered: "masonry" }),
      [bar],
      8,
    )[0];
    const shortBar = { ...bar, text: "pocket", size_px: 96 };
    const patch = smartPlacementPatchForBar(shortBar, candidate);

    expect(smartPlacementCandidateFitsBar(shortBar, candidate)).toBe(true);
    expect(patch.size_px).toBeGreaterThanOrEqual(40);
    expect(patch.size_px).toBeLessThan(96);
  });

  it("splits a whole masonry title into coherent pocket-sized blocks", () => {
    const candidates = resolveSmartPlacementCandidates(
      variant({ montage_preset_rendered: "masonry", text_placement_candidates: null }),
      [bar, { ...bar, id: "text-2" }, { ...bar, id: "text-3" }],
    );

    expect(splitTextForSmartPlacement("take the scenic route home", candidates)).toEqual([
      "take the scenic",
      "route home",
    ]);
  });

  it("caps split output to the available concurrent pockets", () => {
    const candidates = resolveSmartPlacementCandidates(
      variant({ montage_preset_rendered: "masonry" }),
      Array.from({ length: 6 }, (_unused, index) => ({ ...bar, id: `text-${index}` })),
      8,
    );

    expect(candidates).toHaveLength(2);
    expect(
      splitTextForSmartPlacement(
        "one two three four five six seven eight nine ten eleven twelve thirteen",
        candidates,
      ),
    ).toHaveLength(2);
  });

  it("keeps manual line breaks as intended split blocks", () => {
    const candidates = resolveSmartPlacementCandidates(
      variant({ montage_preset_rendered: "masonry", text_placement_candidates: null }),
      [bar, { ...bar, id: "text-2" }, { ...bar, id: "text-3" }],
    );

    expect(splitTextForSmartPlacement("first pocket\nsecond pocket\nthird pocket", candidates)).toEqual([
      "first pocket",
      "second pocket",
      "third pocket",
    ]);
  });
});
