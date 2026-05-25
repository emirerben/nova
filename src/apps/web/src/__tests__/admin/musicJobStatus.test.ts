// @ts-nocheck
import {
  TERMINAL_STATUSES,
  resolveMusicJobOutputUrl,
} from "@/app/admin/music/[id]/components/musicJobStatus";

describe("TERMINAL_STATUSES", () => {
  it("contains exactly music_ready and processing_failed", () => {
    expect(TERMINAL_STATUSES.has("music_ready")).toBe(true);
    expect(TERMINAL_STATUSES.has("processing_failed")).toBe(true);
    expect(TERMINAL_STATUSES.size).toBe(2);
  });

  it("does not classify queued or processing as terminal", () => {
    expect(TERMINAL_STATUSES.has("queued")).toBe(false);
    expect(TERMINAL_STATUSES.has("processing")).toBe(false);
  });
});

describe("resolveMusicJobOutputUrl", () => {
  // ── No-result cases (still rendering / unknown / wrong status) ────────────

  it("returns blank result for a null job", () => {
    expect(resolveMusicJobOutputUrl(null)).toEqual({
      outputUrl: undefined,
      outputLegacy: false,
    });
  });

  it("returns blank result for an undefined job", () => {
    expect(resolveMusicJobOutputUrl(undefined)).toEqual({
      outputUrl: undefined,
      outputLegacy: false,
    });
  });

  it("returns blank result when status is not music_ready (still rendering)", () => {
    expect(
      resolveMusicJobOutputUrl({
        status: "processing",
        output_url: "https://storage.example.com/preview.mp4",
      }),
    ).toEqual({ outputUrl: undefined, outputLegacy: false });
  });

  it("returns blank result when no output_url anywhere", () => {
    expect(
      resolveMusicJobOutputUrl({ status: "music_ready" }),
    ).toEqual({ outputUrl: undefined, outputLegacy: false });
  });

  it("returns blank result when output_url is null and assembly_plan is null", () => {
    expect(
      resolveMusicJobOutputUrl({
        status: "music_ready",
        output_url: null,
        assembly_plan: null,
      }),
    ).toEqual({ outputUrl: undefined, outputLegacy: false });
  });

  // ── Happy path: top-level wins when both top-level and assembly_plan are valid ─

  it("prefers top-level output_url when both top-level and assembly_plan are valid https", () => {
    expect(
      resolveMusicJobOutputUrl({
        status: "music_ready",
        output_url: "https://storage.example.com/top.mp4",
        assembly_plan: { output_url: "https://storage.example.com/plan.mp4" },
      }),
    ).toEqual({
      outputUrl: "https://storage.example.com/top.mp4",
      outputLegacy: false,
    });
  });

  it("accepts http:// as well as https://", () => {
    expect(
      resolveMusicJobOutputUrl({
        status: "music_ready",
        output_url: "http://localhost:8000/preview.mp4",
      }),
    ).toEqual({
      outputUrl: "http://localhost:8000/preview.mp4",
      outputLegacy: false,
    });
  });

  // ── Fallback path: assembly_plan wins when top-level is a legacy GCS path ──

  it("falls back to assembly_plan when top-level is a relative GCS path", () => {
    expect(
      resolveMusicJobOutputUrl({
        status: "music_ready",
        output_url: "music-lyrics-previews/track-1/preview.mp4",
        assembly_plan: { output_url: "https://storage.example.com/plan.mp4" },
      }),
    ).toEqual({
      outputUrl: "https://storage.example.com/plan.mp4",
      outputLegacy: false,
    });
  });

  it("falls back to assembly_plan when top-level is null", () => {
    expect(
      resolveMusicJobOutputUrl({
        status: "music_ready",
        output_url: null,
        assembly_plan: { output_url: "https://storage.example.com/plan.mp4" },
      }),
    ).toEqual({
      outputUrl: "https://storage.example.com/plan.mp4",
      outputLegacy: false,
    });
  });

  it("falls back to assembly_plan when top-level is an empty string", () => {
    expect(
      resolveMusicJobOutputUrl({
        status: "music_ready",
        output_url: "",
        assembly_plan: { output_url: "https://storage.example.com/plan.mp4" },
      }),
    ).toEqual({
      outputUrl: "https://storage.example.com/plan.mp4",
      outputLegacy: false,
    });
  });

  // ── Legacy banner: candidates exist but none are http(s) ──────────────────

  it("flags legacy when only top-level exists and is not http(s)", () => {
    expect(
      resolveMusicJobOutputUrl({
        status: "music_ready",
        output_url: "music-lyrics-previews/track-1/preview.mp4",
      }),
    ).toEqual({ outputUrl: undefined, outputLegacy: true });
  });

  it("flags legacy when only assembly_plan exists and is not http(s)", () => {
    expect(
      resolveMusicJobOutputUrl({
        status: "music_ready",
        assembly_plan: { output_url: "music-jobs/foo/output.mp4" },
      }),
    ).toEqual({ outputUrl: undefined, outputLegacy: true });
  });

  it("flags legacy when BOTH candidates exist but neither is http(s)", () => {
    expect(
      resolveMusicJobOutputUrl({
        status: "music_ready",
        output_url: "music-lyrics-previews/track-1/preview.mp4",
        assembly_plan: { output_url: "music-jobs/foo/output.mp4" },
      }),
    ).toEqual({ outputUrl: undefined, outputLegacy: true });
  });

  it("does NOT flag legacy when an empty string is the only candidate", () => {
    // Empty strings are dropped from the candidate list — they never trigger
    // the legacy banner. The banner is for "this exists but doesn't work",
    // not "the job hasn't surfaced anything yet".
    expect(
      resolveMusicJobOutputUrl({
        status: "music_ready",
        output_url: "",
      }),
    ).toEqual({ outputUrl: undefined, outputLegacy: false });
  });

  // ── Regression guard: the bug rev3 fixed ──────────────────────────────────

  it("regression: top-level legacy + assembly_plan valid → uses valid one (not legacy)", () => {
    // History: rev2 used `fromTop ?? fromPlan`, so any truthy top-level
    // beat the valid assembly_plan URL. Result: outputLegacy was shown even
    // when a working https URL was available on assembly_plan. rev3 fixes
    // this by searching ALL candidates and returning the first valid one.
    const result = resolveMusicJobOutputUrl({
      status: "music_ready",
      output_url: "music-lyrics-previews/track-1/preview.mp4",
      assembly_plan: { output_url: "https://storage.example.com/preview.mp4" },
    });
    expect(result.outputUrl).toBe("https://storage.example.com/preview.mp4");
    expect(result.outputLegacy).toBe(false);
  });
});
