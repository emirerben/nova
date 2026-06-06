/**
 * Tests for progress/logic.ts pure functions.
 * No DOM, no React — runs in Node (jsdom env but unused).
 */

import {
  computeBarPosition,
  etaLadder,
  ETA_OVERRUN_COPY,
  stallTier,
  detailLine,
  updateSeenReady,
  shouldShowAwayNote,
  formatElapsed,
} from "../../components/progress/logic";
import { AWAY_HIDDEN_THRESHOLD_MS } from "../../components/progress/constants";

// ===== etaLadder =====

describe("etaLadder", () => {
  it("returns ~N min left for >= 90s", () => {
    expect(etaLadder(90_000)).toBe("~2 min left");
    expect(etaLadder(120_000)).toBe("~2 min left");
    expect(etaLadder(300_000)).toBe("~5 min left");
  });

  it("returns about a minute left for 25–90s", () => {
    expect(etaLadder(25_000)).toBe("about a minute left");
    expect(etaLadder(60_000)).toBe("about a minute left");
    expect(etaLadder(89_999)).toBe("about a minute left");
  });

  it("returns less than a minute for < 25s", () => {
    expect(etaLadder(24_999)).toBe("less than a minute…");
    expect(etaLadder(0)).toBe("less than a minute…");
    expect(etaLadder(1000)).toBe("less than a minute…");
  });

  it("returns null when remaining is null", () => {
    expect(etaLadder(null)).toBeNull();
  });

  it("returns null when remaining is NaN/Infinity", () => {
    expect(etaLadder(NaN)).toBeNull();
    expect(etaLadder(Infinity)).toBeNull();
  });

  it("ETA_OVERRUN_COPY is defined", () => {
    expect(typeof ETA_OVERRUN_COPY).toBe("string");
    expect(ETA_OVERRUN_COPY.length).toBeGreaterThan(0);
  });
});

// ===== computeBarPosition =====

describe("computeBarPosition", () => {
  const from = 0.2;
  const to = 0.4;
  const baseline = 30_000;

  it("returns fromFraction when elapsed = 0", () => {
    const now = Date.now();
    const pos = computeBarPosition(from, to, now, now, baseline);
    expect(pos).toBeCloseTo(from, 5);
  });

  it("is monotone — never below fromFraction", () => {
    const now = Date.now();
    for (const elapsed of [0, 100, 1000, 5000, 30_000, 120_000]) {
      const pos = computeBarPosition(from, to, now - elapsed, now, baseline);
      expect(pos).toBeGreaterThanOrEqual(from);
    }
  });

  it("asymptotically approaches toFraction but never reaches it", () => {
    const now = Date.now();
    // Very large elapsed — should be close to toFraction but strictly less.
    const pos = computeBarPosition(from, to, now - 10_000_000, now, baseline);
    expect(pos).toBeLessThan(to);
    expect(pos).toBeGreaterThan(to - 0.01);
  });

  it("is deterministic from timestamps — no accumulated state", () => {
    const now = Date.now();
    const lastEventAt = now - 60_000;
    const pos1 = computeBarPosition(from, to, lastEventAt, now, baseline);
    const pos2 = computeBarPosition(from, to, lastEventAt, now, baseline);
    expect(pos1).toBe(pos2);
  });

  it("increases with elapsed time (monotone growth property)", () => {
    const now = Date.now();
    const pos1 = computeBarPosition(from, to, now - 1000, now, baseline);
    const pos2 = computeBarPosition(from, to, now - 5000, now, baseline);
    const pos3 = computeBarPosition(from, to, now - 15000, now, baseline);
    expect(pos2).toBeGreaterThan(pos1);
    expect(pos3).toBeGreaterThan(pos2);
  });
});

// ===== stallTier =====

describe("stallTier", () => {
  it("returns 0 for elapsed < 1.5× baseline", () => {
    expect(stallTier(1000, 10_000)).toBe(0);
    expect(stallTier(14_999, 10_000)).toBe(0);
  });

  it("returns 1 for 1.5× to 2.5× baseline", () => {
    expect(stallTier(15_000, 10_000)).toBe(1);
    expect(stallTier(20_000, 10_000)).toBe(1);
    expect(stallTier(24_999, 10_000)).toBe(1);
  });

  it("returns 2 for > 2.5× baseline", () => {
    expect(stallTier(25_000, 10_000)).toBe(2);
    expect(stallTier(100_000, 10_000)).toBe(2);
  });

  it("returns 0 when baseline is null", () => {
    expect(stallTier(99999, null)).toBe(0);
  });

  it("returns 0 when baseline is 0", () => {
    expect(stallTier(99999, 0)).toBe(0);
  });
});

// ===== detailLine =====

describe("detailLine", () => {
  it("returns empty string for null/undefined", () => {
    expect(detailLine(null)).toBe("");
    expect(detailLine(undefined)).toBe("");
    expect(detailLine([])).toBe("");
  });

  it("returns empty string when all variants are ready", () => {
    const variants = [
      { variant_id: "song_lyrics", render_status: "ready" },
      { variant_id: "song_text", render_status: "ready" },
    ];
    expect(detailLine(variants)).toBe("");
  });

  it("shows count when some ready but none rendering", () => {
    const variants = [
      { variant_id: "song_lyrics", render_status: "ready" },
      { variant_id: "song_text", render_status: null },
    ];
    const line = detailLine(variants);
    expect(line).toMatch(/1 of 2/);
  });

  it("names a single rendering variant", () => {
    const variants = [
      { variant_id: "song_text", render_status: "rendering" },
      { variant_id: "original_text", render_status: null },
    ];
    const line = detailLine(variants);
    expect(line).toContain("Song Text");
    expect(line).toContain("Rendering");
  });

  it("shows count for multiple rendering variants", () => {
    const variants = [
      { variant_id: "song_lyrics", render_status: "rendering" },
      { variant_id: "song_text", render_status: "rendering" },
      { variant_id: "original_text", render_status: "rendering" },
    ];
    const line = detailLine(variants);
    expect(line).toMatch(/3 edits/);
  });
});

// ===== updateSeenReady =====

describe("updateSeenReady", () => {
  it("adds only ready variants", () => {
    const prev = new Set<string>();
    const variants = [
      { variant_id: "song_lyrics", render_status: "ready" },
      { variant_id: "song_text", render_status: "rendering" },
    ];
    const next = updateSeenReady(prev, variants);
    expect(next.has("song_lyrics")).toBe(true);
    expect(next.has("song_text")).toBe(false);
  });

  it("grows monotonically — never removes ids", () => {
    const prev = new Set(["original_text"]);
    const variants = [
      { variant_id: "song_lyrics", render_status: "ready" },
    ];
    const next = updateSeenReady(prev, variants);
    expect(next.has("original_text")).toBe(true);
    expect(next.has("song_lyrics")).toBe(true);
  });

  it("handles null/undefined variants gracefully", () => {
    const prev = new Set(["song_lyrics"]);
    expect(updateSeenReady(prev, null).has("song_lyrics")).toBe(true);
    expect(updateSeenReady(prev, undefined).has("song_lyrics")).toBe(true);
  });
});

// ===== shouldShowAwayNote =====

describe("shouldShowAwayNote", () => {
  it("returns false when hiddenAtMs is null", () => {
    expect(shouldShowAwayNote(null, new Set(), new Set(["a"]))).toBe(false);
  });

  it("returns false when hidden < AWAY_HIDDEN_THRESHOLD_MS", () => {
    const hiddenAt = Date.now() - AWAY_HIDDEN_THRESHOLD_MS + 500;
    expect(shouldShowAwayNote(hiddenAt, new Set(), new Set(["a"]))).toBe(false);
  });

  it("returns false when hidden long but no new ids", () => {
    const hiddenAt = Date.now() - AWAY_HIDDEN_THRESHOLD_MS - 1000;
    const before = new Set(["a"]);
    const now = new Set(["a"]);
    expect(shouldShowAwayNote(hiddenAt, before, now)).toBe(false);
  });

  it("returns true when hidden > threshold AND new ids appeared", () => {
    const hiddenAt = Date.now() - AWAY_HIDDEN_THRESHOLD_MS - 1000;
    const before = new Set<string>();
    const now = new Set(["song_lyrics"]);
    expect(shouldShowAwayNote(hiddenAt, before, now)).toBe(true);
  });
});

// ===== formatElapsed =====

describe("formatElapsed", () => {
  it("formats 0ms as 0:00", () => {
    expect(formatElapsed(0)).toBe("0:00");
  });

  it("formats 65 seconds as 1:05", () => {
    expect(formatElapsed(65_000)).toBe("1:05");
  });

  it("formats 7 seconds as 0:07", () => {
    expect(formatElapsed(7_000)).toBe("0:07");
  });

  it("handles negative values (clamp to 0)", () => {
    expect(formatElapsed(-1000)).toBe("0:00");
  });
});
