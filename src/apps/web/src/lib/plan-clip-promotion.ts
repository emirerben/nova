/**
 * plan-clip-promotion — pure assignment-merge logic for "Use in edit".
 *
 * attach_clips is a full-set replace (last writer wins), so the promotion
 * payload must carry EVERY existing assignment unchanged — dropping a
 * shot_id or user_note here silently destroys the creator's slot mapping.
 * Kept pure and unit-tested (plan-clip-promotion.test.ts) for exactly that
 * reason; page.tsx only wires the result to the attach call.
 */

export interface PromotableAssignment {
  gcs_path: string;
  shot_id: string | null;
  user_note: string;
}

/**
 * Build the full assignment set with `poolPath` appended as an unassigned clip.
 * Returns null when there is nothing to do: empty/missing poolPath (old-API
 * version skew) or the path is already attached (dedupe — re-sending would be
 * a harmless but pointless full re-write).
 */
export function buildPromotedAssignments(
  current: Array<{ gcs_path: string; shot_id: string | null; user_note?: string | null }>,
  poolPath: string | null | undefined,
): PromotableAssignment[] | null {
  if (!poolPath) return null;
  const preserved: PromotableAssignment[] = current.map((a) => ({
    gcs_path: a.gcs_path,
    shot_id: a.shot_id,
    user_note: a.user_note ?? "",
  }));
  if (preserved.some((a) => a.gcs_path === poolPath)) return null;
  return [...preserved, { gcs_path: poolPath, shot_id: null, user_note: "" }];
}
