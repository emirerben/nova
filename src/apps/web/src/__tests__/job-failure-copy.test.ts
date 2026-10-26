import { jobFailureCopy } from "@/lib/job-failure-copy";
import { variantFailureCopy } from "@/lib/variant-failure-copy";

describe("jobFailureCopy", () => {
  it.each([
    ["user_clip_unusable", "review_media"],
    ["clip_read_error", "review_media"],
    ["user_clip_download_failed", "retry_upload"],
    ["output_upload_failed", "retry_upload"],
    ["processing_timeout", "retry_render"],
    ["ffmpeg_failed", "retry_render"],
    ["unknown", "contact_support"],
  ] as const)("maps %s to the safe %s recovery", (reason, action) => {
    const copy = jobFailureCopy(reason);
    expect(copy.action).toBe(action);
    expect(copy.actionLabel).toMatch(/^[A-Z][^.!?]+$/);
    expect(`${copy.title} ${copy.detail}`).not.toContain(reason);
  });
});

describe("variantFailureCopy", () => {
  it.each([
    [
      "guided_story_music_missing",
      "The music attached to this edit is no longer available. Choose another song and save again.",
    ],
    [
      "render_enqueue_failed",
      "Your edits were saved, but the render didn't start. Open the editor and retry Save.",
    ],
  ])("uses a specific recovery for %s", (errorClass, expected) => {
    expect(variantFailureCopy(errorClass)).toBe(expected);
  });

  // Failure modes: a spend cap read as "try again" (a retry answers the same), a raw
  // code leaking into copy, and a transient music load read as a support case.
  it("never offers a retry for an exhausted AI quota", () => {
    const job = jobFailureCopy("ai_quota_exhausted");
    expect(job.action).toBe("wait");
    for (const copy of [job.detail, variantFailureCopy("ai_quota_exhausted")]) {
      expect(copy).not.toContain("ai_quota");
      expect(copy).toMatch(/won't help/);
      expect(copy).not.toMatch(/^Try again/);
    }
  });

  it("explains a music load that failed after retries without blaming the edit", () => {
    const copy = variantFailureCopy("guided_story_music_unavailable");
    expect(copy).toMatch(/couldn't be loaded/);
    expect(copy).not.toMatch(/support reference/);
  });

  it("uses shared safe recovery copy for unknown render failures", () => {
    const copy = variantFailureCopy("unknown_backend_failure");

    expect(copy).toBe("Try again. If it keeps happening, send the support reference below so we can trace it.");
    expect(copy).not.toContain("try again above");
  });
});
