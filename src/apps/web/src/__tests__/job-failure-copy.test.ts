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
  it("uses shared safe recovery copy for unknown render failures", () => {
    const copy = variantFailureCopy("unknown_backend_failure");

    expect(copy).toBe("Try again. If it keeps happening, send the support reference below so we can trace it.");
    expect(copy).not.toContain("try again above");
  });
});
