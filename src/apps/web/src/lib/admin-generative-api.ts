/**
 * Typed client for the admin generative-edits overview endpoint.
 *
 * Calls go through the Next.js admin proxy (`/api/admin/[...]`) so the admin
 * token never reaches the browser bundle. Backend route source:
 *   src/apps/api/app/routes/admin_generative.py
 *
 * Keep these types in sync with the Pydantic response models in that file.
 * Launching/inspecting a generative job is NOT here — launch reuses the public
 * `generative-api.ts` client, and per-job detail reuses the existing
 * /admin/jobs/[id] debug view.
 */

const ADMIN_PROXY = "/api/admin";

export interface AdminGenerativeVariant {
  variant_id: string;
  text_mode: string | null;
  track_title: string | null;
  render_status: string | null;
  ok: boolean | null;
  error: string | null;
}

export interface AdminGenerativeListItem {
  job_id: string;
  status: string;
  created_at: string;
  updated_at: string;
  error_detail: string | null;
  clip_count: number;
  target_duration_s: number | null;
  variants: AdminGenerativeVariant[];
}

export interface AdminGenerativeListResponse {
  items: AdminGenerativeListItem[];
  total: number;
}

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

export async function adminListGenerativeJobs(
  limit = 100,
): Promise<AdminGenerativeListResponse> {
  return _adminJson<AdminGenerativeListResponse>(`/generative?limit=${limit}`);
}
