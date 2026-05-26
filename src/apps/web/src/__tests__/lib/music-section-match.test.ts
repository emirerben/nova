// @ts-nocheck
/**
 * Unit tests for the shared section-matcher used by:
 *   - AudioPlayer.tsx (per-band ✓ + thicker stroke "isSelected" indicator)
 *   - page.tsx ConfigTabContent (top metadata Row "Section #N" label)
 *
 * Both surfaces MUST agree on which band is selected. This test pins the
 * tolerance and the null/empty/drift behavior so a future change to either
 * surface can't silently desync them.
 */
import {
  SELECTED_TOLERANCE_S,
  matchSectionByBounds,
} from "@/lib/music-section-match";

const sections = [
  {
    rank: 1,
    start_s: 60,
    end_s: 78,
    label: "chorus",
    energy: "high",
    suggested_use: "hook",
    rationale: "peak energy chorus.",
  },
  {
    rank: 2,
    start_s: 100,
    end_s: 118,
    label: "bridge",
    energy: "medium",
    suggested_use: "build",
    rationale: "mid-energy bridge.",
  },
  {
    rank: 3,
    start_s: 140,
    end_s: 156,
    label: "verse",
    energy: "medium",
    suggested_use: "transition",
    rationale: "second verse.",
  },
];

test("exposes a 0.5s tolerance constant — pins the shared threshold", () => {
  expect(SELECTED_TOLERANCE_S).toBe(0.5);
});

test("exact bound match returns that section", () => {
  const matched = matchSectionByBounds(sections, 100, 118);
  expect(matched).toBe(sections[1]);
});

test("0.3s drift on start stays within tolerance — still matches", () => {
  const matched = matchSectionByBounds(sections, 100.3, 118);
  expect(matched).toBe(sections[1]);
});

test("0.3s drift on end stays within tolerance — still matches", () => {
  const matched = matchSectionByBounds(sections, 100, 117.7);
  expect(matched).toBe(sections[1]);
});

test("0.6s drift on start exceeds tolerance — no match", () => {
  const matched = matchSectionByBounds(sections, 100.6, 118);
  expect(matched).toBeUndefined();
});

test("bounds outside any section's range — no match (Custom window territory)", () => {
  const matched = matchSectionByBounds(sections, 200, 210);
  expect(matched).toBeUndefined();
});

test("null sections returns undefined (unanalyzed track path)", () => {
  expect(matchSectionByBounds(null, 60, 78)).toBeUndefined();
});

test("undefined sections returns undefined (defensive)", () => {
  expect(matchSectionByBounds(undefined, 60, 78)).toBeUndefined();
});

test("empty sections array returns undefined", () => {
  expect(matchSectionByBounds([], 60, 78)).toBeUndefined();
});

test("NaN bounds return undefined (cleared input mid-edit)", () => {
  expect(matchSectionByBounds(sections, NaN, 78)).toBeUndefined();
  expect(matchSectionByBounds(sections, 60, NaN)).toBeUndefined();
  expect(matchSectionByBounds(sections, NaN, NaN)).toBeUndefined();
});

test("returns first match — rank order preserved", () => {
  // Two sections happen to overlap (hypothetical bad data, but the matcher
  // should still return the first one Array.find encounters, not crash).
  const overlapping = [
    { ...sections[0], start_s: 50, end_s: 60 },
    { ...sections[1], start_s: 50, end_s: 60 },
  ];
  const matched = matchSectionByBounds(overlapping, 50, 60);
  expect(matched).toBe(overlapping[0]);
});

test("matches at exactly tolerance boundary returns undefined (strict <)", () => {
  // Documents the half-open semantics: 0.5s exactly is NOT a match.
  const matched = matchSectionByBounds(sections, 100.5, 118);
  expect(matched).toBeUndefined();
});
