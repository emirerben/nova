import type { FrameState } from "@/lib/carousel-preview";
import {
  FPS,
  MAX_FOCUS_TOTAL_S,
  buildMomentTimeline,
  computeFocusStartTimeline,
  fitDuration,
  resolveEffectiveMode,
  resolveFocusMoments,
  resolveFrameIndex,
} from "@/app/plan/items/[id]/_editor/carousel-preview-impl/geometry";

function frame(overrides: Partial<FrameState> & { tS: number; scrollX: number }): FrameState {
  return { focusCard: null, focusT: 0, dim: 0, ...overrides };
}

describe("resolveEffectiveMode", () => {
  it("defaults to focus (mirrors CarouselPanel's prefill default)", () => {
    expect(resolveEffectiveMode(undefined)).toBe("focus");
  });
  it("passes rolling through", () => {
    expect(resolveEffectiveMode("rolling")).toBe("rolling");
  });
  it("treats any non-rolling value as focus", () => {
    expect(resolveEffectiveMode("focus")).toBe("focus");
  });
});

describe("resolveFocusMoments", () => {
  it("null focusClipIndex -> min(1, nCards-1), mirroring segment.py's auto-pick fallback", () => {
    expect(resolveFocusMoments(null, 4)[0].cardIndex).toBe(1);
    expect(resolveFocusMoments(undefined, 4)[0].cardIndex).toBe(1);
  });
  it("single-card pool clamps the auto-pick fallback to card 0", () => {
    expect(resolveFocusMoments(null, 1)[0].cardIndex).toBe(0);
  });
  it("clamps an explicit out-of-range index into [0, nCards-1]", () => {
    expect(resolveFocusMoments(99, 3)[0].cardIndex).toBe(2);
    expect(resolveFocusMoments(-5, 3)[0].cardIndex).toBe(0);
  });
  it("passes a valid explicit index through", () => {
    expect(resolveFocusMoments(2, 4)[0].cardIndex).toBe(2);
  });
  it("empty pool -> no moments", () => {
    expect(resolveFocusMoments(0, 0)).toEqual([]);
  });
});

describe("fitDuration", () => {
  const frames: FrameState[] = [
    frame({ tS: 1 / FPS, scrollX: 0 }),
    frame({ tS: 2 / FPS, scrollX: 10 }),
    frame({ tS: 3 / FPS, scrollX: 20, focusCard: 1, focusT: 0.5, dim: 0.2 }),
  ];

  it("truncates when there are too many frames", () => {
    const fit = fitDuration(frames, 2);
    expect(fit).toHaveLength(2);
    expect(fit[0].scrollX).toBe(0);
    expect(fit[1].scrollX).toBe(10);
  });

  it("pads by repeating the LAST frame's full state, only advancing tS", () => {
    const fit = fitDuration(frames, 5);
    expect(fit).toHaveLength(5);
    // Original frames pass through untouched.
    expect(fit.slice(0, 3)).toEqual(frames);
    // Padded frames repeat frame 2's scroll/focus/dim state exactly.
    expect(fit[3]).toEqual({ ...frames[2], tS: frames[2].tS + 1 / FPS });
    expect(fit[4]).toEqual({ ...frames[2], tS: frames[2].tS + 2 / FPS });
  });

  it("returns empty for an empty input regardless of target", () => {
    expect(fitDuration([], 10)).toEqual([]);
  });

  it("is a no-op when frames.length === targetN", () => {
    expect(fitDuration(frames, 3)).toEqual(frames);
  });
});

describe("resolveFrameIndex", () => {
  it("rounds local time to the nearest frame at 30fps", () => {
    expect(resolveFrameIndex(100, 1.0)).toBe(30);
    expect(resolveFrameIndex(100, 1.001)).toBe(30); // rounds down within a frame
    expect(resolveFrameIndex(100, 1.02)).toBe(31); // rounds up to the next frame
  });
  it("clamps to [0, frameCount - 1]", () => {
    expect(resolveFrameIndex(10, -5)).toBe(0);
    expect(resolveFrameIndex(10, 999)).toBe(9);
  });
  it("returns -1 for an empty timeline", () => {
    expect(resolveFrameIndex(0, 1)).toBe(-1);
  });
});

describe("buildMomentTimeline", () => {
  it("empty clip pool -> empty timeline", () => {
    expect(buildMomentTimeline({ mode: "rolling" }, 0, 4)).toEqual([]);
  });

  it("rolling mode fits to round(durationS * FPS) frames", () => {
    const frames = buildMomentTimeline({ mode: "rolling" }, 3, 2.0);
    expect(frames).toHaveLength(Math.round(2.0 * FPS));
  });

  it("focus mode ALWAYS fits to durationS (documented divergence from segment.py, which only fits on an explicit override) — pads short, trims long", () => {
    const shortFrames = buildMomentTimeline({ mode: "focus", focus_clip_index: 1 }, 3, 0.3);
    expect(shortFrames).toHaveLength(Math.round(0.3 * FPS));

    const longFrames = buildMomentTimeline({ mode: "focus", focus_clip_index: 1 }, 3, 12.0);
    expect(longFrames).toHaveLength(Math.round(12.0 * FPS));
  });

  it("focus mode caps at MAX_FOCUS_TOTAL_S even if durationS asks for more", () => {
    const frames = buildMomentTimeline({ mode: "focus", focus_clip_index: 1 }, 3, 999);
    expect(frames).toHaveLength(Math.round(MAX_FOCUS_TOTAL_S * FPS));
  });

  it("defaults to focus mode when config.mode is undefined", () => {
    const focusFrames = buildMomentTimeline({ focus_clip_index: 1 }, 3, 2.0);
    const explicitFocusFrames = buildMomentTimeline({ mode: "focus", focus_clip_index: 1 }, 3, 2.0);
    expect(focusFrames).toEqual(explicitFocusFrames);
  });

  it("a focus-mode timeline visits the requested card (focusCard appears at some frame)", () => {
    const frames = buildMomentTimeline({ mode: "focus", focus_clip_index: 2 }, 4, 6.0);
    expect(frames.some((f) => f.focusCard === 2 && f.focusT > 0)).toBe(true);
  });
});

describe("computeFocusStartTimeline", () => {
  it("null for every unfocused frame", () => {
    const frames = [frame({ tS: 0, scrollX: 0 }), frame({ tS: 1 / FPS, scrollX: 0 })];
    expect(computeFocusStartTimeline(frames)).toEqual([null, null]);
  });

  it("records the tS at which a focus streak begins, and holds it for the rest of the streak", () => {
    const frames = [
      frame({ tS: 0, scrollX: 0 }),
      frame({ tS: 1, scrollX: 0, focusCard: 0, focusT: 0.3 }),
      frame({ tS: 2, scrollX: 0, focusCard: 0, focusT: 1.0 }),
      frame({ tS: 3, scrollX: 0, focusCard: 0, focusT: 1.0 }),
    ];
    expect(computeFocusStartTimeline(frames)).toEqual([null, 1, 1, 1]);
  });

  it("REFOCUS SEMANTICS: the same card focused twice (with an unfocused gap) restarts its start time — mirrors renderer.py's focus_start_frame reset-on-refocus", () => {
    const frames = [
      frame({ tS: 0, scrollX: 0, focusCard: 0, focusT: 0.5 }), // streak 1 begins
      frame({ tS: 1, scrollX: 0, focusCard: 0, focusT: 1.0 }),
      frame({ tS: 2, scrollX: 0 }), // unfocused gap
      frame({ tS: 3, scrollX: 0, focusCard: 0, focusT: 0.2 }), // streak 2 begins (same card!)
      frame({ tS: 4, scrollX: 0, focusCard: 0, focusT: 1.0 }),
    ];
    expect(computeFocusStartTimeline(frames)).toEqual([0, 0, null, 3, 3]);
  });

  it("switching focus directly to a different card (no gap) also restarts the start time", () => {
    const frames = [
      frame({ tS: 0, scrollX: 0, focusCard: 0, focusT: 1.0 }),
      frame({ tS: 1, scrollX: 0, focusCard: 1, focusT: 0.1 }), // different card, same frame index
      frame({ tS: 2, scrollX: 0, focusCard: 1, focusT: 1.0 }),
    ];
    expect(computeFocusStartTimeline(frames)).toEqual([0, 1, 1]);
  });

  it("focusT === 0 does not count as focused even if focusCard is set", () => {
    const frames = [frame({ tS: 0, scrollX: 0, focusCard: 0, focusT: 0 })];
    expect(computeFocusStartTimeline(frames)).toEqual([null]);
  });
});
