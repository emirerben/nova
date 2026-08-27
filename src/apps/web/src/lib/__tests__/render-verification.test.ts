import { verifyCommittedRenderDuration } from "../render-verification";

describe("verifyCommittedRenderDuration", () => {
  it("accepts a ready receipt matching the committed timeline", () => {
    expect(verifyCommittedRenderDuration({
      expectedDurationS: 10.6,
      expectedGeneration: "g2",
      expectedRevisionHash: "rev-2",
      variant: { output_url: "https://example.test/video.mp4", render_generation_id: "g2", duration_s: 10.6, render_receipt: { revision_hash: "rev-2", expected_duration_s: 10.6, actual_duration_s: 10.59, verified: true } },
    }).ok).toBe(true);
  });

  it("rejects a ready video whose duration disagrees with the staged timeline", () => {
    const result = verifyCommittedRenderDuration({
      expectedDurationS: 10.6,
      variant: { output_url: "https://example.test/video.mp4", duration_s: 24.033, render_receipt: { expected_duration_s: 10.6, actual_duration_s: 24.033, verified: true } },
    });
    expect(result.ok).toBe(false);
    expect(result.detail).toContain("committed timeline");
  });

  it("rejects an explicit renderer verification failure", () => {
    expect(verifyCommittedRenderDuration({
      expectedDurationS: 10.6,
      variant: { output_url: "https://example.test/video.mp4", duration_s: 10.6, render_receipt: { actual_duration_s: 10.6, verified: false } },
    }).ok).toBe(false);
  });
});
