/**
 * Tests for the admin job-debug SilenceCutStrip (plans/010 T9).
 *
 * Coverage focuses on the pure band-layout helper (layoutSilenceCutBands):
 *
 *   - absent/empty/garbage silence_cut → null (old jobs + version skew)
 *   - proportional positions/widths against the inferred original duration
 *     (max removed end when the variant duration is unknown; cut duration +
 *     removed total when it is)
 *   - persisted original_duration_s preferred over inference when finite
 *     and >= the last removed end; old blobs without it infer unchanged
 *   - time_saved_s preferred when present, summed removals otherwise
 *   - overlap merge: same-reason overlaps merge into one band;
 *     different-reason overlaps clip the later band so bands never stack
 *   - out-of-range input never escapes 0–100%
 *
 * Plus thin render checks: absent blob renders nothing; present blob with
 * no cuts renders the header + "no cuts made" note; populated blob renders
 * one band per removed range, the saved-time header, a per-reason legend,
 * and a role="img" strip whose aria-label enumerates the cuts.
 */

import React from "react";
import { render, screen } from "@testing-library/react";
import {
  layoutSilenceCutBands,
  readSilenceCut,
  SilenceCutStrip,
  type SilenceCut,
} from "@/app/admin/jobs/[id]/SilenceCutStrip";

describe("layoutSilenceCutBands", () => {
  test("returns null for absent input", () => {
    expect(layoutSilenceCutBands(null)).toBeNull();
    expect(layoutSilenceCutBands(undefined)).toBeNull();
  });

  test("returns null when removed is missing, empty, or not an array", () => {
    expect(layoutSilenceCutBands({})).toBeNull();
    expect(layoutSilenceCutBands({ removed: [], time_saved_s: 3 })).toBeNull();
    expect(
      layoutSilenceCutBands({ removed: "nope" } as unknown as SilenceCut),
    ).toBeNull();
  });

  test("returns null when every range is unusable", () => {
    const cut: SilenceCut = {
      removed: [
        { start_s: 5, end_s: 5, reason: "silence" }, // zero-length
        { start_s: 8, end_s: 6, reason: "retake" }, // inverted
        { start_s: Number.NaN, end_s: 2, reason: "silence" }, // non-finite
        { reason: "silence" }, // fields missing entirely
      ],
    };
    expect(layoutSilenceCutBands(cut)).toBeNull();
  });

  test("positions bands proportionally using max removed end when variant duration unknown", () => {
    const cut: SilenceCut = {
      removed: [
        { start_s: 0, end_s: 2, reason: "silence" },
        { start_s: 8, end_s: 10, reason: "retake" },
      ],
      time_saved_s: 4.0,
      version: 1,
    };
    const layout = layoutSilenceCutBands(cut)!;
    expect(layout).not.toBeNull();
    expect(layout.originalDurationS).toBeCloseTo(10);
    expect(layout.bands).toHaveLength(2);
    expect(layout.bands[0].leftPct).toBeCloseTo(0);
    expect(layout.bands[0].widthPct).toBeCloseTo(20);
    expect(layout.bands[1].leftPct).toBeCloseTo(80);
    expect(layout.bands[1].widthPct).toBeCloseTo(20);
    expect(layout.timeSavedS).toBeCloseTo(4.0);
  });

  test("infers original duration as cut duration + removed total when provided", () => {
    const cut: SilenceCut = {
      removed: [{ start_s: 2, end_s: 4, reason: "filler_lexical" }],
    };
    // 8s cut output + 2s removed = 10s original → band at 20%, width 20%.
    const layout = layoutSilenceCutBands(cut, 8)!;
    expect(layout.originalDurationS).toBeCloseTo(10);
    expect(layout.bands[0].leftPct).toBeCloseTo(20);
    expect(layout.bands[0].widthPct).toBeCloseTo(20);
    // No time_saved_s in the blob → falls back to summed removals.
    expect(layout.timeSavedS).toBeCloseTo(2);
  });

  test("never lets a band escape 0–100% when inputs are inconsistent", () => {
    const cut: SilenceCut = {
      removed: [
        { start_s: -1, end_s: 2, reason: "silence" }, // negative start
        { start_s: 50, end_s: 60, reason: "retake" }, // past cut+removed sum
      ],
    };
    // Claimed cut duration (5s) + removed (13s) = 18s < max end (60s) →
    // the larger of the two wins so nothing overflows.
    const layout = layoutSilenceCutBands(cut, 5)!;
    expect(layout.originalDurationS).toBeCloseTo(60);
    for (const b of layout.bands) {
      expect(b.leftPct).toBeGreaterThanOrEqual(0);
      expect(b.widthPct).toBeGreaterThanOrEqual(0);
      expect(b.leftPct + b.widthPct).toBeLessThanOrEqual(100.0001);
    }
  });

  test("clips a different-reason overlap so bands never stack", () => {
    const cut: SilenceCut = {
      removed: [
        { start_s: 3, end_s: 6, reason: "retake" },
        { start_s: 1, end_s: 5, reason: "silence" },
      ],
    };
    const layout = layoutSilenceCutBands(cut)!;
    expect(layout.bands.map((b) => b.reason)).toEqual(["silence", "retake"]);
    expect(layout.originalDurationS).toBeCloseTo(6);
    // silence keeps [1,5]; the later retake is clipped to [5,6].
    expect(layout.bands[0].startS).toBeCloseTo(1);
    expect(layout.bands[0].endS).toBeCloseTo(5);
    expect(layout.bands[1].startS).toBeCloseTo(5);
    expect(layout.bands[1].endS).toBeCloseTo(6);
    // The later band starts exactly where the earlier one ends — no stacking.
    expect(layout.bands[1].leftPct).toBeCloseTo(
      layout.bands[0].leftPct + layout.bands[0].widthPct,
    );
    for (const b of layout.bands) {
      expect(b.leftPct).toBeGreaterThanOrEqual(0);
      expect(b.leftPct + b.widthPct).toBeLessThanOrEqual(100.0001);
    }
  });

  test("merges same-reason overlapping ranges into one band", () => {
    const cut: SilenceCut = {
      removed: [
        { start_s: 4, end_s: 8, reason: "silence" },
        { start_s: 2, end_s: 5, reason: "silence" },
      ],
    };
    const layout = layoutSilenceCutBands(cut)!;
    expect(layout.bands).toHaveLength(1);
    expect(layout.bands[0].startS).toBeCloseTo(2);
    expect(layout.bands[0].endS).toBeCloseTo(8);
    expect(layout.originalDurationS).toBeCloseTo(8);
    expect(layout.bands[0].leftPct).toBeCloseTo(25);
    expect(layout.bands[0].widthPct).toBeCloseTo(75);
    // Fallback saved time counts the overlap once (6s union, not 7s sum).
    expect(layout.timeSavedS).toBeCloseTo(6);
  });

  test("drops a different-reason range fully contained in an earlier one", () => {
    const cut: SilenceCut = {
      removed: [
        { start_s: 0, end_s: 10, reason: "silence" },
        { start_s: 2, end_s: 4, reason: "retake" },
      ],
    };
    const layout = layoutSilenceCutBands(cut)!;
    expect(layout.bands).toHaveLength(1);
    expect(layout.bands[0].reason).toBe("silence");
  });

  test("prefers persisted original_duration_s over inference", () => {
    const cut: SilenceCut = {
      removed: [
        { start_s: 0.5, end_s: 1.5, reason: "silence" },
        { start_s: 20, end_s: 25, reason: "retake" },
      ],
      time_saved_s: 6,
      original_duration_s: 40,
      version: 1,
    };
    // Inference would give 8 + 6 = 14s; the persisted true duration wins.
    const layout = layoutSilenceCutBands(cut, 8)!;
    expect(layout.originalDurationS).toBeCloseTo(40);
    // Last band no longer flush to 100% — the clip ends after the last cut.
    const last = layout.bands[1];
    expect(last.leftPct).toBeCloseTo(50);
    expect(last.widthPct).toBeCloseTo(12.5);
    expect(last.leftPct + last.widthPct).toBeLessThan(100);
  });

  test("ignores original_duration_s when null, non-finite, or smaller than the last cut", () => {
    const removed = [{ start_s: 2, end_s: 4, reason: "silence" }];
    // Old blobs (no field) and null both fall back to the old inference:
    // 8s cut output + 2s removed = 10s.
    expect(
      layoutSilenceCutBands({ removed, original_duration_s: null }, 8)!
        .originalDurationS,
    ).toBeCloseTo(10);
    expect(
      layoutSilenceCutBands({ removed, original_duration_s: Number.NaN }, 8)!
        .originalDurationS,
    ).toBeCloseTo(10);
    // Too small to contain the last cut (4s end) → inference wins too.
    expect(
      layoutSilenceCutBands({ removed, original_duration_s: 3 }, 8)!
        .originalDurationS,
    ).toBeCloseTo(10);
  });

  test("defaults a missing reason to 'unknown'", () => {
    const layout = layoutSilenceCutBands({ removed: [{ start_s: 0, end_s: 1 }] })!;
    expect(layout.bands[0].reason).toBe("unknown");
  });
});

describe("readSilenceCut", () => {
  test("null for non-object variants and missing/garbage keys", () => {
    expect(readSilenceCut(null)).toBeNull();
    expect(readSilenceCut("v")).toBeNull();
    expect(readSilenceCut({ variant_id: "v1" })).toBeNull();
    expect(readSilenceCut({ silence_cut: "bad" })).toBeNull();
  });

  test("passes through an object blob", () => {
    const sc = { removed: [], time_saved_s: 1 };
    expect(readSilenceCut({ silence_cut: sc })).toBe(sc);
  });
});

describe("SilenceCutStrip", () => {
  test("renders nothing when the variant has no silence_cut", () => {
    const { container } = render(
      <SilenceCutStrip variant={{ variant_id: "v1", text_mode: "none" }} />,
    );
    expect(container.firstChild).toBeNull();
  });

  test("renders the header with 'no cuts made' when the blob exists but removed nothing", () => {
    render(
      <SilenceCutStrip
        variant={{ variant_id: "v1", silence_cut: { removed: [], time_saved_s: 0 } }}
      />,
    );
    expect(screen.getByTestId("silence-cut-strip")).toBeTruthy();
    expect(screen.getByText("no cuts made")).toBeTruthy();
    expect(screen.queryAllByTestId("silence-cut-band")).toHaveLength(0);
    expect(screen.queryAllByTestId("silence-cut-legend")).toHaveLength(0);
  });

  test("renders one band per removed range plus the saved header", () => {
    render(
      <SilenceCutStrip
        variant={{
          variant_id: "v1",
          silence_cut: {
            removed: [
              { start_s: 0.5, end_s: 1.5, reason: "silence" },
              { start_s: 4.0, end_s: 4.4, reason: "filler_lexical" },
              { start_s: 20.0, end_s: 25.0, reason: "retake" },
            ],
            time_saved_s: 6.3,
            version: 1,
          },
        }}
      />,
    );
    expect(screen.getByTestId("silence-cut-strip")).toBeTruthy();
    expect(screen.getAllByTestId("silence-cut-band")).toHaveLength(3);
    expect(screen.getByText(/saved 6\.3s · 3 cuts/)).toBeTruthy();
    // Hover detail carries reason + exact seconds.
    expect(
      screen.getByTitle("retake · 20.00–25.00s · 5.00s removed"),
    ).toBeTruthy();
    // Inline legend names every reason present (color not the only channel).
    expect(
      screen.getAllByTestId("silence-cut-legend").map((el) => el.textContent),
    ).toEqual(["silence", "filler_lexical", "retake"]);
    // The strip is an image with an aria-label enumerating the cuts.
    expect(
      screen.getByRole("img", {
        name: "Silence cut: silence 0.5–1.5s, filler_lexical 4.0–4.4s, retake 20.0–25.0s, saved 6.3s",
      }),
    ).toBeTruthy();
  });

  test("legend lists each reason once even with repeated cuts", () => {
    render(
      <SilenceCutStrip
        variant={{
          variant_id: "v1",
          silence_cut: {
            removed: [
              { start_s: 0, end_s: 1, reason: "silence" },
              { start_s: 5, end_s: 6, reason: "silence" },
              { start_s: 8, end_s: 9, reason: "retake" },
            ],
          },
        }}
      />,
    );
    expect(
      screen.getAllByTestId("silence-cut-legend").map((el) => el.textContent),
    ).toEqual(["silence", "retake"]);
  });
});
