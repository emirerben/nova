/**
 * Regression: retrying a failed (or any terminal) plan-item render appeared
 * to silently do nothing.
 *
 * Root cause: `current_job_id` is never cleared when a render fails — it
 * only ever gets reassigned forward, asynchronously, once the NEXT job is
 * minted by the Celery task (after the POST /generate response returns).
 * Both the "keep polling until the render registers" window and the
 * Generate-button release effect used to read `!!item.current_job_id` as
 * "the render has registered" — which is true from the very first poll
 * after a retry click, since the OLD (already-failed) job id is still
 * sitting there. The fix (`hasRenderRegistered`) requires the job id to
 * actually CHANGE from whatever was current at click time, not merely be
 * present.
 *
 * First-ever generate (current_job_id starts `null`) was never affected by
 * the bug — these tests cover that path too, to pin that the fix doesn't
 * regress it.
 */

import { hasRenderRegistered } from "@/app/plan/items/[id]/render-registration";

describe("hasRenderRegistered", () => {
  it("first-ever generate: not registered before any job id appears", () => {
    // jobIdBeforeClick is null (nothing was ever attached), and still null.
    expect(hasRenderRegistered({ current_job_id: null, status: "idea" }, null)).toBe(false);
  });

  it("first-ever generate: registered once a job id appears", () => {
    expect(
      hasRenderRegistered({ current_job_id: "job-1", status: "ready" }, null),
    ).toBe(true);
  });

  it("first-ever generate: registered via explicit generating status even with no job id yet", () => {
    expect(hasRenderRegistered({ current_job_id: null, status: "generating" }, null)).toBe(
      true,
    );
  });

  it("REGRESSION — retry after failure: NOT registered while the old job id is still current", () => {
    // This is exactly the bug: current_job_id already points at the failed
    // job at click time. Immediately after clicking Generate again, before
    // the new job has been minted, the id hasn't changed yet.
    expect(
      hasRenderRegistered(
        { current_job_id: "job-old-failed", status: "failed" },
        "job-old-failed",
      ),
    ).toBe(false);
  });

  it("retry after failure: registered once a DIFFERENT (new) job id appears", () => {
    expect(
      hasRenderRegistered(
        { current_job_id: "job-new", status: "ready" },
        "job-old-failed",
      ),
    ).toBe(true);
  });

  it("retry after failure: registered via explicit generating status even if the id hasn't visibly changed yet", () => {
    // Belt-and-suspenders — an explicit status flip is unambiguous evidence
    // regardless of what the job id looks like at that instant.
    expect(
      hasRenderRegistered(
        { current_job_id: "job-old-failed", status: "generating" },
        "job-old-failed",
      ),
    ).toBe(true);
  });

  it("retry after a DIFFERENT terminal status (e.g. cancelled), same shape as failed", () => {
    expect(
      hasRenderRegistered(
        { current_job_id: "job-old-cancelled", status: "failed" },
        "job-old-cancelled",
      ),
    ).toBe(false);
  });
});
