/**
 * Tests for lib/job-phases.ts and the template-job-phases regression invariant.
 *
 * REGRESSION IRON RULE:
 * The template screen imports PHASE_ORDER, PHASE_LABEL, phaseProgress directly
 * from "../../lib/template-job-phases". These exports must remain byte-identical.
 * This test suite locks them in.
 */

// ===== REGRESSION: template-job-phases direct imports =====

import {
  PHASE_ORDER,
  PHASE_LABEL,
  phaseProgress,
} from "../../lib/template-job-phases";

// Lock in exact 9-phase list — any removal or rename fails this test.
const EXPECTED_PHASE_ORDER = [
  "queued",
  "download_clips",
  "analyze_clips",
  "match_clips",
  "assemble",
  "mix_audio",
  "generate_copy",
  "upload",
  "finalize",
] as const;

describe("template-job-phases regression invariant", () => {
  it("PHASE_ORDER has exactly 9 phases in the correct order", () => {
    expect(PHASE_ORDER).toHaveLength(9);
    expect(PHASE_ORDER).toEqual(EXPECTED_PHASE_ORDER);
  });

  it("PHASE_LABEL has an entry for every phase in PHASE_ORDER", () => {
    for (const phase of PHASE_ORDER) {
      expect(PHASE_LABEL).toHaveProperty(phase);
      expect(typeof PHASE_LABEL[phase]).toBe("string");
      expect(PHASE_LABEL[phase].length).toBeGreaterThan(0);
    }
  });

  it("phaseProgress('queued') returns 1/9 (index 0, idx+1/length)", () => {
    const expected = 1 / 9;
    expect(phaseProgress("queued")).toBeCloseTo(expected, 5);
  });

  it("phaseProgress returns index-derived values (unchanged from legacy)", () => {
    // Index 0 → 1/9, index 4 → 5/9, index 8 → capped at 0.98
    expect(phaseProgress("queued")).toBeCloseTo(1 / 9, 5);
    expect(phaseProgress("assemble")).toBeCloseTo(5 / 9, 5);
    expect(phaseProgress("finalize")).toBeLessThanOrEqual(0.98);
    expect(phaseProgress("finalize")).toBeCloseTo(Math.min(0.98, 9 / 9), 5);
  });

  it("phaseProgress returns 0.02 for null/undefined (tiny non-zero, bar visible)", () => {
    expect(phaseProgress(null)).toBe(0.02);
    expect(phaseProgress(undefined)).toBe(0.02);
  });

  it("phaseProgress returns 0.5 for unknown phase (forward compat)", () => {
    expect(phaseProgress("future_phase_not_in_list")).toBe(0.5);
  });
});

// ===== lib/job-phases.ts: computeAnchors =====

import { computeAnchors, dampedPos } from "../../lib/job-phases";

describe("computeAnchors", () => {
  it("returns equal-width slices when expectedMs is null", () => {
    const phases = ["a", "b", "c", "d"] as const;
    const anchors = computeAnchors(phases, null);
    expect(anchors["a"]).toEqual([0, 0.25]);
    expect(anchors["b"][0]).toBeCloseTo(0.25, 5);
    expect(anchors["b"][1]).toBeCloseTo(0.5, 5);
  });

  it("returns equal-width slices when all durations are zero", () => {
    const phases = ["a", "b"] as const;
    const anchors = computeAnchors(phases, { a: 0, b: 0 });
    expect(anchors["a"]).toEqual([0, 0.5]);
    expect(anchors["b"][0]).toBeCloseTo(0.5, 5);
    expect(anchors["b"][1]).toBeCloseTo(1.0, 5);
  });

  it("weights slices by duration when present", () => {
    const phases = ["fast", "slow"] as const;
    // fast = 10s, slow = 90s → fast gets 10%, slow gets 90%
    const anchors = computeAnchors(phases, { fast: 10_000, slow: 90_000 });
    expect(anchors["fast"]).toEqual([0, 0.1]);
    expect(anchors["slow"][0]).toBeCloseTo(0.1, 5);
    expect(anchors["slow"][1]).toBeCloseTo(1.0, 5);
  });

  it("fractions sum to 1.0 (last entry ends at exactly 1.0)", () => {
    const phases = ["a", "b", "c"] as const;
    const anchors = computeAnchors(phases, { a: 5000, b: 15000, c: 30000 });
    const [, lastEnd] = anchors["c"];
    expect(lastEnd).toBe(1.0);
  });

  it("returns empty object for empty phase list", () => {
    expect(computeAnchors([], null)).toEqual({});
  });
});

// ===== lib/job-phases.ts: dampedPos =====

describe("dampedPos", () => {
  it("returns fromFraction when elapsed = 0", () => {
    expect(dampedPos(0.2, 0.4, 0, 10_000)).toBeCloseTo(0.2, 5);
  });

  it("returns fromFraction when baselineMs <= 0", () => {
    expect(dampedPos(0.2, 0.4, 5000, 0)).toBe(0.2);
    expect(dampedPos(0.2, 0.4, 5000, -1)).toBe(0.2);
  });

  it("approaches toFraction asymptotically (< toFraction for all finite elapsed)", () => {
    // elapsed >> baseline → should be very close to toFraction but strictly less
    const pos = dampedPos(0.0, 1.0, 10_000_000, 1_000);
    expect(pos).toBeLessThan(1.0);
    expect(pos).toBeGreaterThan(0.99);
  });

  it("is monotone — larger elapsed → larger output", () => {
    const baseline = 30_000;
    const from = 0.1;
    const to = 0.9;
    const pos1 = dampedPos(from, to, 1000, baseline);
    const pos2 = dampedPos(from, to, 5000, baseline);
    const pos3 = dampedPos(from, to, 20000, baseline);
    expect(pos2).toBeGreaterThan(pos1);
    expect(pos3).toBeGreaterThan(pos2);
  });

  it("never exceeds toFraction", () => {
    for (const elapsed of [0, 1000, 30_000, 500_000]) {
      const pos = dampedPos(0.3, 0.7, elapsed, 30_000);
      expect(pos).toBeLessThan(0.7);
    }
  });

  it("never goes below fromFraction", () => {
    for (const elapsed of [0, 1, 100, 10_000]) {
      const pos = dampedPos(0.3, 0.7, elapsed, 30_000);
      expect(pos).toBeGreaterThanOrEqual(0.3);
    }
  });
});

// ===== Generative phase exports from job-phases.ts =====

import {
  GENERATIVE_PHASE_ORDER,
  GENERATIVE_PHASE_LABEL,
} from "../../lib/job-phases";

describe("generative phase exports", () => {
  it("exports 5 generative phases", () => {
    expect(GENERATIVE_PHASE_ORDER).toHaveLength(5);
  });

  it("GENERATIVE_PHASE_LABEL has an entry for every phase", () => {
    for (const phase of GENERATIVE_PHASE_ORDER) {
      expect(GENERATIVE_PHASE_LABEL).toHaveProperty(phase);
      expect(typeof GENERATIVE_PHASE_LABEL[phase]).toBe("string");
    }
  });

  it("re-exports TEMPLATE_PHASE_ORDER from template-job-phases", () => {
    const { TEMPLATE_PHASE_ORDER } = require("../../lib/job-phases");
    expect(TEMPLATE_PHASE_ORDER).toEqual(EXPECTED_PHASE_ORDER);
  });
});

// ===== Activation phase exports from job-phases.ts (PR4) =====

import {
  ACTIVATION_PHASE_ORDER,
  ACTIVATION_PHASE_LABEL,
} from "../../lib/job-phases";

describe("activation phase exports", () => {
  it("ACTIVATION_PHASE_ORDER has exactly 3 entries", () => {
    expect(ACTIVATION_PHASE_ORDER).toHaveLength(3);
  });

  it("ACTIVATION_PHASE_ORDER entries are matching_clips, picking_days, starting_renders", () => {
    expect(ACTIVATION_PHASE_ORDER[0]).toBe("matching_clips");
    expect(ACTIVATION_PHASE_ORDER[1]).toBe("picking_days");
    expect(ACTIVATION_PHASE_ORDER[2]).toBe("starting_renders");
  });

  it("ACTIVATION_PHASE_LABEL has all 3 keys with non-empty string values", () => {
    for (const phase of ACTIVATION_PHASE_ORDER) {
      expect(ACTIVATION_PHASE_LABEL).toHaveProperty(phase);
      expect(typeof ACTIVATION_PHASE_LABEL[phase]).toBe("string");
      expect(ACTIVATION_PHASE_LABEL[phase].length).toBeGreaterThan(0);
    }
  });
});
