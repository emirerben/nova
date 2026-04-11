import {
  SCALE,
  FONT_SIZE_MAP,
  POSITION_Y_MAP,
  isOverlayVisible,
  snapToNearestZone,
  computeBarPosition,
  getEffectiveTiming,
} from "@/app/admin/templates/[id]/components/overlay-constants";
import type { RecipeTextOverlay } from "@/app/admin/templates/[id]/components/recipe-types";

// ── Test fixture ────────────────────────────────────────────────────────────

function makeOverlay(overrides: Partial<RecipeTextOverlay> = {}): RecipeTextOverlay {
  return {
    role: "hook",
    text: "Test overlay",
    position: "center",
    effect: "pop-in",
    font_style: "sans",
    text_size: "medium",
    text_color: "#FFFFFF",
    start_s: 1.0,
    end_s: 3.0,
    start_s_override: null,
    end_s_override: null,
    has_darkening: false,
    has_narrowing: false,
    sample_text: "",
    font_cycle_accel_at_s: null,
    ...overrides,
  };
}

// ── Tier 1: Pure logic tests ────────────────────────────────────────────────

describe("overlay-constants", () => {
  describe("SCALE and font sizes", () => {
    test("SCALE produces readable preview sizes (≥10px for all text sizes)", () => {
      for (const [size, px] of Object.entries(FONT_SIZE_MAP)) {
        const scaled = px * SCALE;
        expect(scaled).toBeGreaterThanOrEqual(10);
      }
    });

    test("POSITION_Y_MAP matches backend values", () => {
      expect(POSITION_Y_MAP.top).toBe(0.15);
      expect(POSITION_Y_MAP.center).toBe(0.50);
      expect(POSITION_Y_MAP.bottom).toBe(0.85);
    });

    test("FONT_SIZE_MAP matches backend values", () => {
      expect(FONT_SIZE_MAP.small).toBe(48);
      expect(FONT_SIZE_MAP.medium).toBe(72);
      expect(FONT_SIZE_MAP.large).toBe(120);
      expect(FONT_SIZE_MAP.xlarge).toBe(150);
    });
  });

  describe("getEffectiveTiming", () => {
    test("uses raw values when no overrides", () => {
      const overlay = makeOverlay({ start_s: 1.0, end_s: 3.0 });
      const timing = getEffectiveTiming(overlay);
      expect(timing.start).toBe(1.0);
      expect(timing.end).toBe(3.0);
    });

    test("uses overrides when present", () => {
      const overlay = makeOverlay({
        start_s: 1.0,
        end_s: 3.0,
        start_s_override: 0.5,
        end_s_override: 2.5,
      });
      const timing = getEffectiveTiming(overlay);
      expect(timing.start).toBe(0.5);
      expect(timing.end).toBe(2.5);
    });

    test("uses partial overrides", () => {
      const overlay = makeOverlay({
        start_s: 1.0,
        end_s: 3.0,
        start_s_override: 0.5,
        end_s_override: null,
      });
      const timing = getEffectiveTiming(overlay);
      expect(timing.start).toBe(0.5);
      expect(timing.end).toBe(3.0);
    });
  });

  describe("isOverlayVisible", () => {
    test("visible when currentTime within range", () => {
      const overlay = makeOverlay({ start_s: 1.0, end_s: 3.0 });
      expect(isOverlayVisible(2.0, overlay)).toBe(true);
    });

    test("visible at exact start boundary", () => {
      const overlay = makeOverlay({ start_s: 1.0, end_s: 3.0 });
      expect(isOverlayVisible(1.0, overlay)).toBe(true);
    });

    test("visible at exact end boundary", () => {
      const overlay = makeOverlay({ start_s: 1.0, end_s: 3.0 });
      expect(isOverlayVisible(3.0, overlay)).toBe(true);
    });

    test("not visible before start", () => {
      const overlay = makeOverlay({ start_s: 1.0, end_s: 3.0 });
      expect(isOverlayVisible(0.5, overlay)).toBe(false);
    });

    test("not visible after end", () => {
      const overlay = makeOverlay({ start_s: 1.0, end_s: 3.0 });
      expect(isOverlayVisible(3.5, overlay)).toBe(false);
    });

    test("uses overrides for visibility", () => {
      const overlay = makeOverlay({
        start_s: 1.0,
        end_s: 3.0,
        start_s_override: 0.5,
        end_s_override: 2.0,
      });
      // 0.7 is within override range [0.5, 2.0] but would be outside raw range [1.0, 3.0]
      expect(isOverlayVisible(0.7, overlay)).toBe(true);
      // 2.5 is within raw range but outside override range
      expect(isOverlayVisible(2.5, overlay)).toBe(false);
    });
  });

  describe("snapToNearestZone", () => {
    test("snaps to top when near top", () => {
      expect(snapToNearestZone(0.1)).toBe("top");
      expect(snapToNearestZone(0.15)).toBe("top");
      expect(snapToNearestZone(0.2)).toBe("top");
    });

    test("snaps to center when near center", () => {
      expect(snapToNearestZone(0.4)).toBe("center");
      expect(snapToNearestZone(0.5)).toBe("center");
      expect(snapToNearestZone(0.6)).toBe("center");
    });

    test("snaps to bottom when near bottom", () => {
      expect(snapToNearestZone(0.75)).toBe("bottom");
      expect(snapToNearestZone(0.85)).toBe("bottom");
      expect(snapToNearestZone(0.95)).toBe("bottom");
    });

    test("snaps to nearest at boundary between zones", () => {
      // Midpoint between top (0.15) and center (0.50) = 0.325
      expect(snapToNearestZone(0.32)).toBe("top");
      expect(snapToNearestZone(0.33)).toBe("center");
    });

    test("handles extreme values", () => {
      expect(snapToNearestZone(0)).toBe("top");
      expect(snapToNearestZone(1)).toBe("bottom");
    });
  });

  describe("computeBarPosition", () => {
    test("computes correct position for simple overlay", () => {
      const overlay = makeOverlay({ start_s: 1.0, end_s: 3.0 });
      const { leftPct, widthPct } = computeBarPosition(overlay, 5.0);
      expect(leftPct).toBe(20); // 1/5 = 20%
      expect(widthPct).toBe(40); // 2/5 = 40%
    });

    test("uses overrides for bar position", () => {
      const overlay = makeOverlay({
        start_s: 1.0,
        end_s: 3.0,
        start_s_override: 0.5,
        end_s_override: 2.0,
      });
      const { leftPct, widthPct } = computeBarPosition(overlay, 5.0);
      expect(leftPct).toBe(10); // 0.5/5 = 10%
      expect(widthPct).toBe(30); // 1.5/5 = 30%
    });

    test("clamps to slot duration", () => {
      const overlay = makeOverlay({ start_s: 1.0, end_s: 8.0 });
      const { leftPct, widthPct } = computeBarPosition(overlay, 5.0);
      expect(leftPct).toBe(20); // 1/5 = 20%
      expect(widthPct).toBe(80); // (5-1)/5 = 80% (clamped end)
    });

    test("handles zero duration slot", () => {
      const overlay = makeOverlay({ start_s: 0, end_s: 1.0 });
      const { leftPct, widthPct } = computeBarPosition(overlay, 0);
      expect(leftPct).toBe(0);
      expect(widthPct).toBe(0);
    });

    test("clamps negative start to 0", () => {
      const overlay = makeOverlay({ start_s: 0, end_s: 2.0, start_s_override: -1 });
      const { leftPct, widthPct } = computeBarPosition(overlay, 5.0);
      expect(leftPct).toBe(0);
      expect(widthPct).toBe(40); // 2/5 = 40%
    });
  });
});
