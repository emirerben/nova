/**
 * Tests for lib/plan-clip-promotion.ts — the pure merge behind "Use in edit".
 * attach_clips is a full-set replace, so the invariant under test is: existing
 * assignments survive promotion byte-for-byte, and no-op cases return null.
 */

import { buildPromotedAssignments } from "@/lib/plan-clip-promotion";

const POOL = "users/u1/plan/i1/pool/rec.mp4";

describe("buildPromotedAssignments", () => {
  it("appends the pool path as an unassigned clip, preserving shot_id and user_note", () => {
    const current = [
      { gcs_path: "users/u1/plan/i1/a.mp4", shot_id: "s1", user_note: "keep the intro" },
      { gcs_path: "users/u1/plan/i1/b.mp4", shot_id: null, user_note: "" },
    ];
    const next = buildPromotedAssignments(current, POOL);
    expect(next).toEqual([
      { gcs_path: "users/u1/plan/i1/a.mp4", shot_id: "s1", user_note: "keep the intro" },
      { gcs_path: "users/u1/plan/i1/b.mp4", shot_id: null, user_note: "" },
      { gcs_path: POOL, shot_id: null, user_note: "" },
    ]);
  });

  it("normalizes null/undefined user_note to empty string on preserved clips", () => {
    const next = buildPromotedAssignments(
      [{ gcs_path: "a.mp4", shot_id: "s1", user_note: null }],
      POOL,
    );
    expect(next?.[0]).toEqual({ gcs_path: "a.mp4", shot_id: "s1", user_note: "" });
  });

  it("returns null when the path is already attached (dedupe)", () => {
    const current = [{ gcs_path: POOL, shot_id: null, user_note: "" }];
    expect(buildPromotedAssignments(current, POOL)).toBeNull();
  });

  it("returns null on a missing pool path (old-API version skew)", () => {
    expect(buildPromotedAssignments([], "")).toBeNull();
    expect(buildPromotedAssignments([], null)).toBeNull();
    expect(buildPromotedAssignments([], undefined)).toBeNull();
  });

  it("works from an empty clip set (first clip via promotion)", () => {
    expect(buildPromotedAssignments([], POOL)).toEqual([
      { gcs_path: POOL, shot_id: null, user_note: "" },
    ]);
  });
});
