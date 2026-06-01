// @ts-nocheck
/**
 * Parity tests for `countSlotsClient` (TS port) vs `count_slots` (Python
 * source of truth at src/apps/api/app/pipeline/music_recipe.py:20-31).
 *
 * Every test below mirrors a test in
 * src/apps/api/tests/pipeline/test_music_recipe.py so that if either side
 * drifts, this suite fails. When the Python adds a new test_count_slots_*,
 * mirror it here too.
 *
 * Cross-validates the simplified arithmetic against the literal
 * Python expression `len(range(0, max(0, len(window) - n), n))` so a
 * future micro-optimization on either side can't silently disagree.
 */
import { countSlotsClient } from "@/lib/music-slot-count";

function pythonRangeLen(start: number, stop: number, step: number): number {
  if (step <= 0) return 0;
  return Math.max(0, Math.ceil((stop - start) / step));
}

function pythonCountSlots(
  beats: number[],
  startS: number,
  endS: number,
  n: number,
): number {
  const windowBeats = beats.filter((b) => startS <= b && b <= endS);
  if (windowBeats.length <= n) return 0;
  return pythonRangeLen(0, windowBeats.length - n, n);
}

describe("countSlotsClient — parity with Python count_slots", () => {
  test("empty beats list -> 0 (test_count_slots_empty_beats)", () => {
    expect(countSlotsClient([], 0.0, 30.0, 8)).toBe(0);
  });

  test("Marea (Fred Again) regression: 5 beats in [156.6, 170.0] n=8 -> 0", () => {
    // EXACT beats from
    // tests/pipeline/test_music_recipe.py::test_count_slots_fewer_beats_than_n_returns_zero
    const mareaBeats = [159.474, 165.447, 165.895, 167.026, 169.799];
    expect(countSlotsClient(mareaBeats, 156.6, 170.0, 8)).toBe(0);
    expect(countSlotsClient(mareaBeats, 156.6, 170.0, 4)).toBe(1);
  });

  test("beats == n boundary -> 0 (test_count_slots_equal_to_n_returns_zero_boundary)", () => {
    const beats = [0, 1, 2, 3, 4, 5, 6, 7].map((i) => i);
    expect(countSlotsClient(beats, 0.0, 8.0, 8)).toBe(0);
  });

  test("beats == n+1 -> 1 (test_count_slots_just_above_n_returns_one)", () => {
    const beats = [0, 1, 2, 3, 4, 5, 6, 7, 8].map((i) => i);
    expect(countSlotsClient(beats, 0.0, 9.0, 8)).toBe(1);
  });

  test("window outside beats -> 0 (test_count_slots_window_outside_beats_returns_zero)", () => {
    const beats = [1.0, 2.0, 3.0];
    expect(countSlotsClient(beats, 100.0, 200.0, 4)).toBe(0);
  });

  test("matches the literal range() arithmetic (test_count_slots_matches_range_arithmetic)", () => {
    const beats = Array.from({ length: 40 }, (_, i) => i);
    for (const n of [2, 4, 8, 12]) {
      const got = countSlotsClient(beats, 0.0, 39.0, n);
      const want = pythonCountSlots(beats, 0.0, 39.0, n);
      expect({ n, got }).toEqual({ n, got: want });
    }
  });

  test("Feels Like We Only Go Backwards: 62 beats / 200s window [56.2, 73.4] n=8 -> 0", () => {
    // The track the user hit the 422 on. Average 62 beats over 200s = ~5
    // beats per 17.2s window. Locks the FE preview's amber state for this
    // exact (window, n) combo so the audit trail is end-to-end.
    const beats = Array.from({ length: 62 }, (_, i) => (i * 200.0) / 62);
    expect(countSlotsClient(beats, 56.2, 73.4, 8)).toBe(0);
  });

  describe("Feels Like — auto-shrink discoverability", () => {
    const beats = Array.from({ length: 62 }, (_, i) => (i * 200.0) / 62);
    test("lowering N to 4 may still 0-slot on sparse windows (UX hint)", () => {
      // Documents the exact UX flow: the live badge tells the user when
      // their chosen N produces 0 slots; lowering N until the count
      // turns >0 is the user's job. The badge's amber → green transition
      // is the affordance.
      const at8 = countSlotsClient(beats, 56.2, 73.4, 8);
      const at4 = countSlotsClient(beats, 56.2, 73.4, 4);
      const at2 = countSlotsClient(beats, 56.2, 73.4, 2);
      expect(at8).toBe(0);
      // n=4 needs >4 beats; ~5 beats in window — boundary case
      expect(at4).toBeGreaterThanOrEqual(0);
      expect(at2).toBeGreaterThanOrEqual(0);
      // At least one of {4, 2} should produce >=1 slot for the user to
      // recover from the auto-pick without widening the window.
      expect(Math.max(at4, at2)).toBeGreaterThanOrEqual(1);
    });
  });

  test("inclusive bounds: a beat exactly at start_s or end_s counts (matches Python startS <= b && b <= endS)", () => {
    const beats = [10.0, 20.0];
    expect(countSlotsClient(beats, 10.0, 20.0, 1)).toBe(1);
    // Exclude either bound and it drops to 1 element — under N=1 -> 0.
    expect(countSlotsClient(beats, 10.001, 20.0, 1)).toBe(0);
    expect(countSlotsClient(beats, 10.0, 19.999, 1)).toBe(0);
  });

  // Defense-in-depth: hostile inputs that could leak into the function
  // via parseFloat("") / parseInt("abc", 10) / legacy DB rows with N=0.
  // The function must return 0 (not Infinity, not negative, not NaN) for
  // every one of these so the live badge correctly disables Save.
  describe("hostile inputs return 0 (defense in depth)", () => {
    const beats = Array.from({ length: 20 }, (_, i) => i + 1);

    test("n = 0 returns 0 (legacy DB or hand-edit)", () => {
      expect(countSlotsClient(beats, 0, 20, 0)).toBe(0);
    });

    test("n < 0 returns 0", () => {
      expect(countSlotsClient(beats, 0, 20, -4)).toBe(0);
    });

    test("n = NaN returns 0 (parseInt of cleared field)", () => {
      expect(countSlotsClient(beats, 0, 20, Number.NaN)).toBe(0);
    });

    test("n = Infinity returns 0", () => {
      expect(countSlotsClient(beats, 0, 20, Number.POSITIVE_INFINITY)).toBe(0);
    });

    test("NaN bounds return 0 (parseFloat of cleared field)", () => {
      expect(countSlotsClient(beats, Number.NaN, 20, 8)).toBe(0);
      expect(countSlotsClient(beats, 0, Number.NaN, 8)).toBe(0);
      expect(countSlotsClient(beats, Number.NaN, Number.NaN, 8)).toBe(0);
    });
  });
});
