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
