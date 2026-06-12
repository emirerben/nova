/**
 * Tests for the pure math helpers added for edge-drag resize:
 *   - beatsForWindowSeconds (grid right-edge snap)
 *   - fieldsDiffer (per-slot "edited" badge)
 */

import {
  beatsForWindowSeconds,
  fieldsDiffer,
  type DraftSlot,
} from "@/app/generative/timeline-math";

// ── beatsForWindowSeconds ─────────────────────────────────────────────────────

/** NON-uniform grid: intervals 0.5, 0.7, 0.4, 0.9, 0.5, 0.6, 0.4, 1.0 (9 stamps, 8 beats). */
const GRID = [0, 0.5, 1.2, 1.6, 2.5, 3.0, 3.6, 4.0, 5.0];

describe("beatsForWindowSeconds", () => {
  it("returns 1 when maxK is 0 or negative", () => {
    expect(beatsForWindowSeconds(GRID, 0, 1.0, 0)).toBe(1);
    expect(beatsForWindowSeconds(GRID, 0, 1.0, -1)).toBe(1);
  });

  it("picks the beat count whose window length is nearest the target", () => {
    // From offset 0: k=1→0.5s, k=2→1.2s, k=3→1.6s, k=4→2.5s
    // target 1.1s → nearest is k=2 (|1.2-1.1|=0.1 vs |0.5-1.1|=0.6)
    expect(beatsForWindowSeconds(GRID, 0, 1.1, 8)).toBe(2);

    // target 0.3s → nearest is k=1 (|0.5-0.3|=0.2 vs |1.2-0.3|=0.9)
    expect(beatsForWindowSeconds(GRID, 0, 0.3, 8)).toBe(1);

    // target 5.0s → nearest is k=8 (last interval, grid[8]-grid[0] = 5.0)
    expect(beatsForWindowSeconds(GRID, 0, 5.0, 8)).toBe(8);
  });

  it("respects offsetBeats — window is relative to the slot start", () => {
    // From offset 2 (grid position 1.2s):
    //   k=1 → grid[3]-grid[2] = 1.6-1.2 = 0.4s
    //   k=2 → grid[4]-grid[2] = 2.5-1.2 = 1.3s
    //   k=3 → grid[5]-grid[2] = 3.0-1.2 = 1.8s
    // target 1.2s → nearest k=2 (|1.3-1.2|=0.1 vs |0.4-1.2|=0.8)
    expect(beatsForWindowSeconds(GRID, 2, 1.2, 6)).toBe(2);
  });

  it("respects maxK — never exceeds the allowed beat ceiling", () => {
    // target 5.0s from offset 0 but maxK=2 → best of k=1,2 only
    // k=1→0.5, k=2→1.2; nearest to 5.0 is k=2
    expect(beatsForWindowSeconds(GRID, 0, 5.0, 2)).toBe(2);
  });

  it("respects grid boundary — never goes past maxGridBeats - offsetBeats", () => {
    // Grid has 8 beats (9 stamps). From offset 6, only 2 beats remain.
    // Even if maxK=8, limit = min(8, 8-6) = 2.
    expect(beatsForWindowSeconds(GRID, 6, 99, 8)).toBe(2);
  });

  it("returns 1 when offsetBeats equals or exceeds maxGridBeats", () => {
    // offset 8 = maxGridBeats → limit = min(8, 8-8) = 0 → fallback 1
    expect(beatsForWindowSeconds(GRID, 8, 1.0, 8)).toBe(1);
  });

  it("works on a uniform grid (degenerate beat intervals)", () => {
    const uniform = [0, 1, 2, 3, 4, 5];
    // From offset 0: k=1→1s, k=2→2s, k=3→3s, k=4→4s, k=5→5s
    expect(beatsForWindowSeconds(uniform, 0, 2.4, 5)).toBe(2); // |2-2.4|=0.4 < |3-2.4|=0.6
    expect(beatsForWindowSeconds(uniform, 0, 2.6, 5)).toBe(3); // |3-2.6|=0.4 < |2-2.6|=0.6
  });
});

// ── fieldsDiffer ──────────────────────────────────────────────────────────────

function slot(overrides: Partial<DraftSlot> = {}): DraftSlot {
  return {
    key: "s1",
    slotId: "s1",
    clipIndex: 0,
    inS: 1.0,
    durationBeats: 2,
    durationS: 1.2,
    removed: false,
    momentDescription: null,
    ...overrides,
  };
}

describe("fieldsDiffer", () => {
  it("returns false for identical slots", () => {
    const a = slot();
    expect(fieldsDiffer(a, { ...a })).toBe(false);
  });

  it("returns true when clipIndex changes", () => {
    expect(fieldsDiffer(slot(), slot({ clipIndex: 1 }))).toBe(true);
  });

  it("returns true when inS changes beyond epsilon", () => {
    expect(fieldsDiffer(slot(), slot({ inS: 1.0 + 1e-5 }))).toBe(true);
    // Below epsilon is not a change
    expect(fieldsDiffer(slot(), slot({ inS: 1.0 + 1e-7 }))).toBe(false);
  });

  it("returns true when durationBeats changes", () => {
    expect(fieldsDiffer(slot(), slot({ durationBeats: 3 }))).toBe(true);
  });

  it("ignores durationS when durationBeats is non-null (grid slot)", () => {
    // Grid slots: beat count is authoritative; durationS is derived and may differ.
    expect(fieldsDiffer(slot({ durationBeats: 2, durationS: 1.2 }),
                        slot({ durationBeats: 2, durationS: 9.9 }))).toBe(false);
  });

  it("returns true when durationS changes on a seconds slot (null-beats)", () => {
    const a = slot({ durationBeats: null, durationS: 2.0 });
    const b = slot({ durationBeats: null, durationS: 2.5 });
    expect(fieldsDiffer(a, b)).toBe(true);
  });

  it("returns false for identical durationS on a seconds slot", () => {
    const a = slot({ durationBeats: null, durationS: 2.0 });
    expect(fieldsDiffer(a, { ...a })).toBe(false);
  });

  it("returns true when removed changes", () => {
    expect(fieldsDiffer(slot(), slot({ removed: true }))).toBe(true);
  });

  it("changing both inS and durationS on a seconds slot is a single edit", () => {
    // fieldsDiffer is a per-slot boolean — two field changes still = one diff = one edit count.
    const a = slot({ durationBeats: null, inS: 0, durationS: 2.0 });
    const b = slot({ durationBeats: null, inS: 0.5, durationS: 1.5 });
    expect(fieldsDiffer(a, b)).toBe(true); // counts as 1 via countEdits
  });
});
