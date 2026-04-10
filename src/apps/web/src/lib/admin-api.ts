/**
 * Admin API client with X-Admin-Token injection from sessionStorage.
 *
 * All admin endpoints require the token; the backend validates it.
 * Token is stored in sessionStorage (clears on tab close).
 */

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const TOKEN_KEY = "nova_admin_token";

export function getAdminToken(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem(TOKEN_KEY);
}

export function setAdminToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token);
}

export function clearAdminToken(): void {
  sessionStorage.removeItem(TOKEN_KEY);
}

function adminHeaders(): Record<string, string> {
  const token = getAdminToken();
  if (!token) throw new Error("Not authenticated");
  return {
    "Content-Type": "application/json",
    "X-Admin-Token": token,
  };
}

async function adminFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = { ...adminHeaders(), ...init?.headers };
  const res = await fetch(`${API_URL}${path}`, { ...init, headers });
  if (res.status === 401) {
    clearAdminToken();
    throw new Error("Invalid admin token");
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? `Request failed: ${res.status}`);
  }
  return res;
}

// ── Types ──────────────────────────────────────────────────────────────────────

export interface AdminTemplate {
  id: string;
  name: string;
  gcs_path: string;
  analysis_status: string;
  required_clips_min: number;
  required_clips_max: number;
  published_at: string | null;
  archived_at: string | null;
  description: string | null;
  source_url: string | null;
  thumbnail_gcs_path: string | null;
  created_at: string;
}

export interface AdminTemplateListItem {
  id: string;
  name: string;
  analysis_status: string;
  published_at: string | null;
  archived_at: string | null;
  description: string | null;
  thumbnail_gcs_path: string | null;
  job_count: number;
  created_at: string;
}

export interface AdminTemplateListResponse {
  templates: AdminTemplateListItem[];
  total: number;
}

export interface TemplateMetrics {
  template_id: string;
  total_jobs: number;
  successful_jobs: number;
  failed_jobs: number;
  last_job_at: string | null;
}

export interface RecipeVersionItem {
  id: string;
  trigger: string;
  created_at: string;
  slot_count: number;
  total_duration_s: number;
}

export interface RecipeHistoryResponse {
  versions: RecipeVersionItem[];
  total: number;
}

export interface TestJobResponse {
  job_id: string;
  status: string;
  template_id: string;
}

export interface PresignedUploadResponse {
  upload_url: string;
  gcs_path: string;
}

// ── API calls ──────────────────────────────────────────────────────────────────

export async function adminListTemplates(
  limit = 50,
  offset = 0,
): Promise<AdminTemplateListResponse> {
  const res = await adminFetch(`/admin/templates?limit=${limit}&offset=${offset}`);
  return res.json();
}

export async function adminGetTemplate(id: string): Promise<AdminTemplate> {
  const res = await adminFetch(`/admin/templates/${id}`);
  return res.json();
}

export async function adminUpdateTemplate(
  id: string,
  data: {
    name?: string;
    description?: string;
    source_url?: string;
    required_clips_min?: number;
    required_clips_max?: number;
    publish?: boolean;
    archive?: boolean;
  },
): Promise<AdminTemplate> {
  const res = await adminFetch(`/admin/templates/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
  return res.json();
}

export async function adminCreateTemplate(data: {
  name: string;
  gcs_path: string;
  required_clips_min?: number;
  required_clips_max?: number;
  description?: string;
  source_url?: string;
}): Promise<AdminTemplate> {
  const res = await adminFetch("/admin/templates", {
    method: "POST",
    body: JSON.stringify(data),
  });
  return res.json();
}

export async function adminCreateTemplateFromUrl(data: {
  name: string;
  url: string;
  required_clips_min?: number;
  required_clips_max?: number;
  description?: string;
}): Promise<AdminTemplate> {
  const res = await adminFetch("/admin/templates/from-url", {
    method: "POST",
    body: JSON.stringify(data),
  });
  return res.json();
}

export async function adminReanalyzeTemplate(id: string): Promise<AdminTemplate> {
  const res = await adminFetch(`/admin/templates/${id}/reanalyze`, {
    method: "POST",
  });
  return res.json();
}

export async function adminGetMetrics(id: string): Promise<TemplateMetrics> {
  const res = await adminFetch(`/admin/templates/${id}/metrics`);
  return res.json();
}

export async function adminGetRecipeHistory(
  id: string,
  limit = 20,
  offset = 0,
): Promise<RecipeHistoryResponse> {
  const res = await adminFetch(
    `/admin/templates/${id}/recipe-history?limit=${limit}&offset=${offset}`,
  );
  return res.json();
}

export async function adminCreateTestJob(
  templateId: string,
  data: {
    clip_gcs_paths: string[];
    selected_platforms?: string[];
    subject?: string;
  },
): Promise<TestJobResponse> {
  const res = await adminFetch(`/admin/templates/${templateId}/test-job`, {
    method: "POST",
    body: JSON.stringify(data),
  });
  return res.json();
}

export async function adminGetPresignedUpload(
  filename: string,
  contentType = "video/mp4",
): Promise<PresignedUploadResponse> {
  const res = await adminFetch("/admin/upload-presigned", {
    method: "POST",
    body: JSON.stringify({ filename, content_type: contentType }),
  });
  return res.json();
}

// ── Latest test job API ───────────────────────────────────────────────────────

export interface LatestTestJob {
  job_id: string;
  output_url: string | null;
  clip_paths: string[];
  created_at: string;
}

export async function adminGetLatestTestJob(
  templateId: string,
): Promise<LatestTestJob | null> {
  const token = getAdminToken();
  if (!token) return null;
  const res = await fetch(`${API_URL}/admin/templates/${templateId}/latest-test-job`, {
    headers: { "Content-Type": "application/json", "X-Admin-Token": token },
  });
  if (res.status === 404) return null;
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? `Request failed: ${res.status}`);
  }
  return res.json();
}

// ── Recipe editor API ─────────────────────────────────────────────────────────

export interface RecipeResponse {
  recipe: Record<string, unknown>;
  version_id: string;
  version_number: number;
}

export async function adminGetRecipe(id: string): Promise<RecipeResponse> {
  const res = await adminFetch(`/admin/templates/${id}/recipe`);
  return res.json();
}

export async function adminSaveRecipe(
  id: string,
  data: { recipe: Record<string, unknown>; base_version_id: string | null },
): Promise<RecipeResponse> {
  const res = await adminFetch(`/admin/templates/${id}/recipe`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
  return res.json();
}

/** Validate admin token by making a lightweight API call. */
export async function adminValidateToken(): Promise<boolean> {
  try {
    await adminFetch("/admin/templates?limit=1&offset=0");
    return true;
  } catch {
    return false;
  }
}
