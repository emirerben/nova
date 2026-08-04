// Lives outside page.tsx because Next's generated page-type check forbids
// extra named exports from a page module, so keep pure helpers in separate files.

import type { PlanItem } from "@/lib/plan-api";

/**
 * Has a render actually registered as a RESULT of the most recent Generate
 * click? A truthy `current_job_id` alone is NOT enough: on a retry after a
 * failed (or any terminal) render, `current_job_id` already points at the
 * OLD job before the click even happens, so "a job id is present" can't tell
 * "nothing has happened yet" apart from "the old job is still sitting
 * there". Only a job id that DIFFERS from whichever one was current at click
 * time (snapshotted into `jobIdBeforeClick`), or an explicit "generating"
 * status, means the new render has actually landed.
 *
 * Bug this fixes: retrying a failed item silently appeared to do nothing —
 * both the "keep polling until the render registers" window (isTerminalFn)
 * and the Generate-button release effect read `current_job_id` truthiness
 * as "registered", so on a retry they both fired on the very first tick
 * using the stale, already-terminal job, before the new job had a chance to
 * be created. First-ever generate (current_job_id starts `null`) was never
 * affected — this only breaks for a SECOND-or-later attempt on the item.
 */
export function hasRenderRegistered(
  item: Pick<PlanItem, "current_job_id" | "status">,
  jobIdBeforeClick: string | null,
): boolean {
  if (item.status === "generating") return true;
  return item.current_job_id != null && item.current_job_id !== jobIdBeforeClick;
}
