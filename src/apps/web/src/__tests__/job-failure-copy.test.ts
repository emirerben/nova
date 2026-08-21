import { jobFailureCopy } from "@/lib/job-failure-copy";

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
    expect(`${copy.title} ${copy.detail}`).not.toContain(reason);
  });
});
