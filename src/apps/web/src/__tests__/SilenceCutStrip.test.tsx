/**
 * Tests for the admin job-debug SilenceCutStrip (plans/010 T9).
 *
 * Coverage focuses on the pure band-layout helper (layoutSilenceCutBands):
 *
 *   - absent/empty/garbage silence_cut → null (old jobs + version skew)
 *   - proportional positions/widths against the inferred original duration
 *     (max removed end when the variant duration is unknown; cut duration +
 *     removed total when it is)
 *   - time_saved_s preferred when present, summed removals otherwise
 *   - overlapping / out-of-range input never escapes 0–100%
 *
 * Plus thin render checks: absent blob renders nothing; populated blob
 * renders one band per removed range and the saved-time header.
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

  test("tolerates overlapping ranges and sorts by start", () => {
    const cut: SilenceCut = {
      removed: [
        { start_s: 3, end_s: 6, reason: "retake" },
        { start_s: 1, end_s: 5, reason: "silence" },
      ],
    };
    const layout = layoutSilenceCutBands(cut)!;
    expect(layout.bands.map((b) => b.reason)).toEqual(["silence", "retake"]);
    expect(layout.originalDurationS).toBeCloseTo(6);
    for (const b of layout.bands) {
      expect(b.leftPct).toBeGreaterThanOrEqual(0);
      expect(b.leftPct + b.widthPct).toBeLessThanOrEqual(100.0001);
    }
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

  test("renders nothing when removed is empty (no-op plan)", () => {
    const { container } = render(
      <SilenceCutStrip
        variant={{ variant_id: "v1", silence_cut: { removed: [], time_saved_s: 0 } }}
      />,
    );
    expect(container.firstChild).toBeNull();
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
  });
});
