/**
 * Typed client for the dev-loop video-review admin endpoints (T6).
 *
 * All calls go through the Next.js admin proxy (`/api/admin/[...]`) so the
 * admin token never reaches the browser bundle. Backend route source:
 *   src/apps/api/app/routes/admin_review.py
 *
 * Keep these types in sync with the Pydantic response models in that file
 * (ReviewItem / ListReviewResponse / LabelRequest / LabelResponse).
 */

const ADMIN_PROXY = "/api/admin";

// ── Shapes (mirror app/routes/admin_review.py) ───────────────────────────────

export interface ReviewItem {
  run_id: string;
  job_id: string | null;
  band: string;
  avg: number;
  confidence: number;
  risk_tag: string;
  reasoning: string;
  summary_line: string;
  /** Per-dimension rubric scores (hook / legibility / filmed-not-templated / ...). */
  scores: Record<string, number>;
  /** Signed still + playback URLs for the rendered clip (null when unavailable). */
  thumbnail_url: string | null;
  video_url: string | null;
  created_at: string | null;
  /** True once a calibration label exists for this job. */
  labeled: boolean;
}

export interface ListReviewResponse {
  items: ReviewItem[];
  total: number;
}

/** The human's call on an escalated video. The UI sends the explicit forms. */
export type ReviewVerdict = "auto_pass" | "auto_reject" | "agree" | "disagree";

export interface LabelResponse {
  run_id: string;
  job_id: string | null;
  verdict: string;
  ok: boolean;
}

// ── Calls ─────────────────────────────────────────────────────────────────────

async function _adminJson<T>(path: string): Promise<T> {
  const res = await fetch(`${ADMIN_PROXY}${path}`);
  if (!res.ok) {
    let detail = `Request failed: ${res.status}`;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : detail;
    } catch {
      // ignore
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export async function adminListReview(limit = 50): Promise<ListReviewResponse> {
  const qs = new URLSearchParams({ limit: String(limit) });
  return _adminJson<ListReviewResponse>(`/review?${qs.toString()}`);
}

export async function adminLabelReview(
  runId: string,
  verdict: ReviewVerdict,
  note?: string,
): Promise<LabelResponse> {
  const res = await fetch(`${ADMIN_PROXY}/review/${encodeURIComponent(runId)}/label`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ verdict, note: note ?? null }),
  });
  if (!res.ok) {
    let detail = `Label failed: ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // ignore
    }
    throw new Error(detail);
  }
  return (await res.json()) as LabelResponse;
}
