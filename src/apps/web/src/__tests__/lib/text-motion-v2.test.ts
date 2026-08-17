import {
  defaultTextMotion,
  motionPatchForConfig,
  motionPatchForEffect,
  motionPatchForManualEnd,
  motionPatchForText,
  normalizeTextMotion,
  roundTextMotionFrame,
  smoothTypeStateAt,
  smoothTypeLineProgresses,
  textMotionDurationS,
  textMotionGraphemeCount,
  textMotionPreviewDurationS,
  textMotionSettleS,
} from "@/lib/text-motion-v2";
import { animationStateAt } from "@/lib/overlay-animation";

describe("Text Motion v2 timing", () => {
  it("uses the complete overlay duration for v2 exit timing", () => {
    const motion = { ...defaultTextMotion("fade-in"), exit_s: 1 };
    expect(textMotionPreviewDurationS(12, motion, true, 4)).toBe(12);
    expect(textMotionPreviewDurationS(12, motion, false, 4)).toBe(4);
    expect(textMotionPreviewDurationS(3, null, true, 4)).toBe(3);
  });

  it("joins the settled hold to the exit with zero starting velocity", () => {
    const motion = { ...defaultTextMotion("fade-in"), easing: "linear" as const, exit_s: 1 };
    const atJoin = animationStateAt("fade-in", 2, 3, "A", {
      motion,
      motionV2Enabled: true,
    });
    const epsilon = 1e-4;
    const afterJoin = animationStateAt("fade-in", 2 + epsilon, 3, "A", {
      motion,
      motionV2Enabled: true,
    });
    expect(Math.abs((afterJoin.alpha - atJoin.alpha) / epsilon)).toBeLessThan(0.01);
  });

  it("uses roughly 22 grapheme clusters per second plus a 120ms ramp", () => {
    const motion = defaultTextMotion("smooth-type");
    expect(motion.stagger_ms).toBe(45);
    expect(motion.reveal_ramp_ms).toBe(120);
    expect(textMotionSettleS("smooth-type", "AB", motion)).toBeCloseTo(0.165, 9);
    expect(textMotionDurationS("smooth-type", "AB", motion)).toBeCloseTo(1.165, 9);
  });

  it("segments combining marks, ZWJ emoji, Turkish, Arabic, and bidi by grapheme", () => {
    expect(textMotionGraphemeCount("e\u0301")).toBe(1);
    expect(textMotionGraphemeCount("👩‍💻")).toBe(1);
    expect(textMotionGraphemeCount("İyi")).toBe(3);
    expect(textMotionGraphemeCount("مرحبا")).toBe(5);
    expect(textMotionGraphemeCount("A مرحبا 👩‍💻")).toBe(9);
  });

  it("speed keeps start fixed and resizes only the selected overlay", () => {
    const source = {
      text: "Smooth",
      start_s: 2,
      end_s: 5,
      effect: "smooth-type",
      motion: defaultTextMotion("smooth-type"),
    };
    const patch = motionPatchForConfig(source, { speed: 2 }, 20);
    expect(source.start_s).toBe(2);
    expect(patch.motion.speed).toBe(2);
    expect(patch.end_s).toBeLessThan(source.end_s);
  });

  it("selecting an animation is the migration trigger", () => {
    const legacy = { text: "Legacy", start_s: 1, end_s: 4, effect: "fade-in" };
    const patch = motionPatchForEffect(legacy, "smooth-type", 10);
    expect(patch.motion?.version).toBe(2);
    expect(patch.effect).toBe("smooth-type");
    expect(patch.end_s).toBeGreaterThan(legacy.start_s);
  });

  it("selecting no animation clears v2 motion without changing timing", () => {
    const animated = {
      text: "Legacy",
      start_s: 1,
      end_s: 4,
      effect: "fade-in",
      motion: defaultTextMotion("fade-in"),
    };
    expect(motionPatchForEffect(animated, "none", 10)).toEqual({
      effect: "none",
      motion: null,
      end_s: 4,
      reveal_s: null,
    });
  });

  it("ordinary text edits do not migrate legacy elements", () => {
    const legacy = { text: "Legacy", start_s: 1, end_s: 4, effect: "fade-in" };
    expect(motionPatchForText(legacy, "Changed", 10)).toEqual({ text: "Changed" });
  });

  it("v2 text edits recompute end while preserving speed and hold", () => {
    const motion = { ...defaultTextMotion("smooth-type"), speed: 1.5, hold_s: 2 };
    const element = { text: "A", start_s: 1, end_s: 3, effect: "smooth-type", motion };
    const patch = motionPatchForText(element, "A much longer line", 20);
    expect(patch.end_s).toBeGreaterThan(element.end_s);
    expect(element.motion.speed).toBe(1.5);
    expect(element.motion.hold_s).toBe(2);
  });

  it("visual-only controls preserve a manually authored window", () => {
    const motion = defaultTextMotion("smooth-type");
    const element = { text: "A", start_s: 1, end_s: 4.7, effect: "smooth-type", motion };
    const patch = motionPatchForConfig(element, { intensity: 0.25, blur_px: 8 }, 20);
    expect(patch.end_s).toBe(4.7);
  });

  it("manual trims consume hold before raising speed and clamp at 4x", () => {
    const motion = { ...defaultTextMotion("smooth-type"), hold_s: 2 };
    const element = { text: "abcdefghij", start_s: 0, end_s: 3, effect: "smooth-type", motion };
    const holdTrim = motionPatchForManualEnd(element, 1);
    expect(holdTrim.motion?.hold_s).toBeGreaterThanOrEqual(0);
    expect(holdTrim.motion?.speed).toBe(1);
    const settleTrim = motionPatchForManualEnd(element, 0.1);
    expect(settleTrim.motion?.hold_s).toBe(0);
    expect(settleTrim.motion?.speed).toBeGreaterThan(1);
    expect(settleTrim.motion?.speed).toBeLessThanOrEqual(4);
    const minimumEnd = Math.ceil(
      (element.start_s + textMotionSettleS("smooth-type", element.text, {
        ...motion,
        speed: 4,
        hold_s: 0,
      })) * 10,
    ) / 10;
    expect(settleTrim.end_s).toBe(minimumEnd);
    const boundaryTrim = motionPatchForManualEnd(element, 0.1, 0.1);
    expect(boundaryTrim.end_s).toBe(0.1);
    const restored = motionPatchForManualEnd(element, element.end_s);
    expect(restored.motion?.speed).toBe(1);
    expect(restored.motion?.hold_s).toBeGreaterThan(0);
    const extended = motionPatchForManualEnd(element, 30);
    expect(extended.motion?.hold_s).toBeGreaterThan(10);
    expect(normalizeTextMotion("smooth-type", extended.motion).hold_s).toBeCloseTo(
      extended.motion?.hold_s ?? 0,
      9,
    );
  });

  it("clamps unknown and out-of-range control values fail-closed", () => {
    const normalized = normalizeTextMotion("smooth-type", {
      version: 2,
      speed: 99,
      intensity: -1,
      easing: "spring" as never,
      stagger_ms: 999,
      blur_px: 99,
    });
    expect(normalized.speed).toBe(4);
    expect(normalized.intensity).toBe(0);
    expect(normalized.easing).toBe("ease-out-cubic");
    expect(normalized.stagger_ms).toBe(250);
    expect(normalized.blur_px).toBe(12);
  });

  it("rounds renderer phases to exact 30fps samples", () => {
    expect(roundTextMotionFrame(0.049)).toBeCloseTo(1 / 30, 12);
    expect(roundTextMotionFrame(0.051)).toBeCloseTo(2 / 30, 12);
    expect(roundTextMotionFrame(0.15)).toBeCloseTo(5 / 30, 12);
  });

  it("keeps the reveal continuous across cluster boundaries", () => {
    const motion = {
      ...defaultTextMotion("smooth-type"),
      easing: "ease-out-cubic" as const,
      stagger_ms: 45,
      reveal_ramp_ms: 120,
    };
    const before = smoothTypeStateAt("smooth", 0.045 - 1e-6, motion);
    const after = smoothTypeStateAt("smooth", 0.045 + 1e-6, motion);
    expect(Math.abs(after.revealProgress - before.revealProgress)).toBeLessThan(0.0001);
    expect(Math.abs(after.alpha - before.alpha)).toBeLessThan(0.0001);
  });

  it("matches the authored multiline staggered-slice settle formula", () => {
    expect(textMotionSettleS("staggered-slice", "one\ntwo", { version: 2 })).toBeCloseTo(
      1.85,
      9,
    );
  });

  it("never snaps an authored end beyond the video boundary", () => {
    const patch = motionPatchForEffect(
      { text: "A", start_s: 9, end_s: 9.5 },
      "smooth-type",
      9.96,
    );
    expect(patch.end_s).toBeLessThanOrEqual(9.96);
  });

  it("settles to an exact static full-run state", () => {
    const state = smoothTypeStateAt("office 👩‍💻 مرحبا", 10, defaultTextMotion("smooth-type"));
    expect(state).toMatchObject({
      alpha: 1,
      xTranslate: 0,
      yTranslate: 0,
      blurPx: 0,
      revealProgress: 1,
      settled: true,
    });
  });

  it("renders persisted Smooth Type as settled static while the rollout flag is off", () => {
    const motion = defaultTextMotion("smooth-type");
    expect(
      animationStateAt("smooth-type", 0, 2, "Smooth", {
        motion,
        motionV2Enabled: false,
      }),
    ).toMatchObject({ alpha: 1, revealProgress: 1, blurPx: 0, yTranslate: 0 });
    expect(
      animationStateAt("smooth-type", 0, 2, "Smooth", {
        motion,
        motionV2Enabled: true,
      }).revealProgress,
    ).toBe(0);
  });

  it("renders Smooth Type without a v2 config as settled static", () => {
    expect(
      animationStateAt("smooth-type", 0, 2, "Smooth", {
        motionV2Enabled: true,
      }),
    ).toMatchObject({ alpha: 1, revealProgress: 1, blurPx: 0, yTranslate: 0 });
  });

  it("uses the v2 grapheme clock instead of a stale legacy typewriter schedule", () => {
    const motion = { ...defaultTextMotion("typewriter"), hold_s: 0 };
    const state = animationStateAt("typewriter", 0, 1, "👩‍💻A", {
      motion,
      motionV2Enabled: true,
      revealScheduleS: [0, 10, 20, 30],
      absoluteStartS: 0,
    });

    expect(state.visibleText).toBe("👩‍💻");
  });

  it("applies a configured exit phase without changing text layout", () => {
    const motion = { ...defaultTextMotion("smooth-type"), exit_s: 1 };
    const beforeExit = animationStateAt("smooth-type", 1.9, 3, "Smooth", {
      motion,
      motionV2Enabled: true,
    });
    const duringExit = animationStateAt("smooth-type", 2.5, 3, "Smooth", {
      motion,
      motionV2Enabled: true,
    });
    expect(beforeExit.alpha).toBe(1);
    expect(duringExit.alpha).toBeGreaterThan(0);
    expect(duringExit.alpha).toBeLessThan(1);
    expect(duringExit.visibleText).toBe("Smooth");
  });

  it("reveals shaped lines in global logical order", () => {
    const motion = defaultTextMotion("smooth-type");
    const forward = smoothTypeLineProgresses(["FIRST", "SECOND"], 0.2, motion);
    const reverse = smoothTypeLineProgresses(
      ["FIRST", "SECOND"],
      0.2,
      { ...motion, order: "reverse" },
    );
    expect(forward[0]).toBeGreaterThan(forward[1]);
    expect(reverse[1]).toBeGreaterThan(reverse[0]);
  });

  it("uses one frame-rounded settle window without double speed compression", () => {
    const fade = defaultTextMotion("fade-in");
    const fadeState = animationStateAt("fade-in", 0.1, 0.1, "FAST", {
      motion: { ...fade, speed: 4, hold_s: 0 },
      motionV2Enabled: true,
    });
    expect(fadeState.alpha).toBeCloseTo(1, 9);

    const handwriting = defaultTextMotion("ink-reveal");
    const inkState = animationStateAt("ink-reveal", 17 / 30, 0.6, "INK", {
      motion: { ...handwriting, speed: 4, hold_s: 0 },
      motionV2Enabled: true,
    });
    expect(inkState.revealProgress).toBeCloseTo(1, 9);
  });

  it("makes partial intensity meaningful for discrete reveal effects", () => {
    const full = animationStateAt("typewriter", 0, 2, "ABCDEFGHIJ", {
      motion: { ...defaultTextMotion("typewriter"), intensity: 1 },
      motionV2Enabled: true,
    });
    const half = animationStateAt("typewriter", 0, 2, "ABCDEFGHIJ", {
      motion: { ...defaultTextMotion("typewriter"), intensity: 0.5 },
      motionV2Enabled: true,
    });
    expect(half.visibleText.length).toBeGreaterThan(full.visibleText.length);
    expect(half.visibleText.length).toBeLessThan("ABCDEFGHIJ".length);
  });
});
