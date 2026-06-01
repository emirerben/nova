/**
 * API client for the per-user library (/me/jobs).
 *
 * Same pattern as plan-api.ts: RELATIVE URLs through the same-origin /api/me
 * proxy, which injects the NextAuth session's X-User-Id + the server-only
 * INTERNAL_API_KEY before forwarding to FastAPI. The browser never sees the key.
 * A 401 means "not signed in" — callers should route to sign-in.
 */

import { NotAuthenticatedError } from "./plan-api";

// Re-exported so library consumers import the auth-error type from one place.
export { NotAuthenticatedError };

const ME_BASE = "/api/me";

export type LibraryJobStatus = "ready" | "generating" | "failed";

/** The three mutually-exclusive thumb reactions on a video (a `note` is separate). */
export type FeedbackSignal = "up" | "down" | "more_like_this";

export interface LibraryJob {
  id: string;
  /** generative | content_plan | template | music | auto_music | default */
  mode: string;
  status: LibraryJobStatus;
  raw_status: string;
  output_url: string | null;
  created_at: string;
  /** Set once the video has been pinned to a plan day. */
  content_plan_item_id: string | null;
  /** The thumb the user left on this video, or null. */
  feedback_signal: FeedbackSignal | null;
}

export interface LibraryPage {
  jobs: LibraryJob[];
  next_cursor: string | null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${ME_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (res.status === 401) throw new NotAuthenticatedError();
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // non-JSON error body; keep the generic message
    }
    throw new Error(detail);
  }
  // 204 No Content (e.g. DELETE) has no body to parse.
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** The signed-in user's videos, newest first. Pass the prior page's next_cursor for more. */
export function listMyJobs(opts?: { limit?: number; cursor?: string }): Promise<LibraryPage> {
  const qs = new URLSearchParams();
  if (opts?.limit) qs.set("limit", String(opts.limit));
  if (opts?.cursor) qs.set("cursor", opts.cursor);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<LibraryPage>(`/jobs${suffix}`);
}

/** Pin a standalone video onto a day in the signed-in user's content plan. */
export function addJobToPlan(jobId: string, dayIndex: number): Promise<LibraryJob> {
  return request<LibraryJob>(`/jobs/${jobId}/add-to-plan`, {
    method: "POST",
    body: JSON.stringify({ day_index: dayIndex }),
  });
}

export interface FeedbackResponse {
  id: string;
  signal: string;
  job_id: string | null;
  content_plan_id: string | null;
}

/**
 * Leave feedback on a video (a thumb or a note) or a plan-level steer note.
 * Pass exactly one of `jobId` / `contentPlanId`. A `note` signal requires `note`
 * text; the three thumbs are mutually exclusive per video (server keeps one).
 */
export function sendFeedback(opts: {
  signal: FeedbackSignal | "note";
  jobId?: string;
  contentPlanId?: string;
  note?: string;
}): Promise<FeedbackResponse> {
  return request<FeedbackResponse>(`/feedback`, {
    method: "POST",
    body: JSON.stringify({
      signal: opts.signal,
      job_id: opts.jobId ?? null,
      content_plan_id: opts.contentPlanId ?? null,
      note: opts.note ?? null,
    }),
  });
}

/** Remove a feedback row the caller owns (e.g. toggle a thumb off). */
export async function clearFeedback(feedbackId: string): Promise<void> {
  await request<void>(`/feedback/${feedbackId}`, { method: "DELETE" });
}
