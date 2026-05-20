/**
 * Admin API client with X-Admin-Token injection from sessionStorage.
 *
 * All admin endpoints require the token; the backend validates it.
 * Token is stored in sessionStorage (clears on tab close).
 */

import type { AgentRunPayload } from "@/lib/admin-jobs-api";

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
    const detail = err.detail;
    const message = Array.isArray(detail)
      ? detail.map((e: { msg?: string }) => e.msg ?? JSON.stringify(e)).join("; ")
      : detail ?? `Request failed: ${res.status}`;
    throw new Error(message);
  }
  return res;
}

// ── Types ──────────────────────────────────────────────────────────────────────

export interface RequiredInput {
  key: string;
  label: string;
  placeholder?: string;
  max_length?: number;
  required?: boolean;
}

export interface AdminTemplate {
  id: string;
  name: string;
  gcs_path: string | null;
  analysis_status: string;
  required_clips_min: number;
  required_clips_max: number;
  required_inputs: RequiredInput[];
  published_at: string | null;
  archived_at: string | null;
  description: string | null;
  source_url: string | null;
  thumbnail_gcs_path: string | null;
  template_type: string;
  parent_template_id: string | null;
  music_track_id: string | null;
  has_intro_slot: boolean;
  is_agentic: boolean;
  /** Per-template Layer-2 sticky default. null = fall through to the global flag. */
  use_layer2_default: boolean | null;
  error_detail: string | null;
  created_at: string;
  /**
   * Canonical agent names whose live prompt_version no longer matches the
   * snapshot stored when this template's recipe_cached was last written.
   * Empty array = recipe is up to date. Hit reanalyze to refresh.
   */
  recipe_stale_agents: string[];
}

export interface AdminTemplateListItem {
  id: string;
  name: string;
  analysis_status: string;
  published_at: string | null;
  archived_at: string | null;
  description: string | null;
  thumbnail_gcs_path: string | null;
  template_type?: string;
  is_agentic: boolean;
  job_count: number;
  created_at: string;
  /** See AdminTemplate.recipe_stale_agents. */
  recipe_stale_agents: string[];
}

export interface AdminTemplateListResponse {
  templates: AdminTemplateListItem[];
  total: number;
}

export interface StaleTemplateItem {
  id: string;
  name: string;
  is_agentic: boolean;
  template_type: string;
  stale_agents: string[];
}

export interface StaleTemplatesResponse {
  total: number;
  templates: StaleTemplateItem[];
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

export interface TemplateDebugSummary {
  id: string;
  name: string;
  analysis_status: string;
  template_type: string;
  is_agentic: boolean;
  gcs_path: string | null;
  audio_gcs_path: string | null;
  music_track_id: string | null;
  error_detail: string | null;
  recipe_cached_at: string | null;
  created_at: string;
}

export interface TemplateDebugResponse {
  template: TemplateDebugSummary;
  template_agent_runs: AgentRunPayload[];
  recipe_cached: Record<string, unknown> | null;
}

export async function adminGetTemplateDebug(
  id: string,
): Promise<TemplateDebugResponse> {
  const res = await adminFetch(`/admin/templates/${id}/debug`);
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
    required_inputs?: RequiredInput[];
    publish?: boolean;
    archive?: boolean;
    template_type?: string;
    has_intro_slot?: boolean;
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
  is_agentic?: boolean;
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
  is_agentic?: boolean;
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

/**
 * Re-run the full agent stack on an agentic template.
 *
 * `useLayer2` maps to the backend `?use_layer2` query param. Default `true`
 * because Layer-2 is the active text-overlay pipeline; pass `false` to force
 * the legacy Layer-1 path for diffing. Pass `undefined` to let the backend
 * fall back to `template.use_layer2_default`, then `text_overlay_v2_enabled`.
 *
 * The backend always reruns the agent stack on this endpoint (force=True is
 * applied server-side), so each click produces fresh agent_run rows.
 */
export async function adminReanalyzeAgentic(
  id: string,
  useLayer2: boolean | undefined = true,
): Promise<AdminTemplate> {
  const qs = useLayer2 === undefined ? "" : `?use_layer2=${useLayer2 ? "true" : "false"}`;
  const res = await adminFetch(`/admin/templates/${id}/reanalyze-agentic${qs}`, {
    method: "POST",
  });
  return res.json();
}

/**
 * Return every template whose stored recipe_cached_versions snapshot doesn't
 * match the live AgentSpec.prompt_version values. Archived templates excluded
 * by default. Useful right after a prompt rollout — surface what needs to be
 * reanalyzed instead of clicking through templates one by one.
 */
export async function adminListStaleTemplates(
  includeArchived = false,
): Promise<StaleTemplatesResponse> {
  const qs = includeArchived ? "?include_archived=true" : "";
  const res = await adminFetch(`/admin/templates/stale-summary${qs}`);
  return res.json();
}

/**
 * Set or clear the per-template Layer-2 text-overlay sticky default.
 *
 * Resolution priority for reanalyze-agentic:
 *   1. ?use_layer2 query param (if present, wins absolutely)
 *   2. use_layer2_default (this field, if not null)
 *   3. global settings.text_overlay_v2_enabled flag
 *
 * Pass null to clear — reanalysis will fall through to the global flag.
 */
export async function adminSetUseLayer2Default(
  id: string,
  use_layer2_default: boolean | null,
): Promise<AdminTemplate> {
  const res = await adminFetch(`/admin/templates/${id}/use-layer2-default`, {
    method: "PUT",
    body: JSON.stringify({ use_layer2_default }),
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
    // When true the orchestrator skips curtain-close interstitials and
    // generate_copy. Backend default is false; admin test tab passes true.
    preview_mode?: boolean;
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

// ── Font default override (agentic templates) ─────────────────────────────────
//
// Agentic templates lock the full recipe editor — this is the one narrow
// override admins have: pick the template-level `font_default` from the
// CLIP-suggested alternatives (or any registry font). Backend cascades the
// pick to every overlay that inherited the old default; text_designer's
// deliberate per-overlay choices stay.

export interface FontAlternativeItem {
  family: string;
  similarity: number;
}

export interface FontDefaultResponse {
  font_default: string | null;
  alternatives: FontAlternativeItem[];
  registry_families: string[];
}

export async function adminGetFontDefault(
  templateId: string,
): Promise<FontDefaultResponse> {
  const res = await adminFetch(`/admin/templates/${templateId}/font-default`);
  return res.json();
}

export async function adminSetFontDefault(
  templateId: string,
  fontDefault: string,
): Promise<void> {
  await adminFetch(`/admin/templates/${templateId}/font-default`, {
    method: "POST",
    body: JSON.stringify({ font_default: fontDefault }),
  });
}

// ── Latest test job API ───────────────────────────────────────────────────────

export interface LatestTestJob {
  job_id: string;
  output_url: string | null;
  base_output_url: string | null;
  clip_paths: string[];
  has_rerender_data: boolean;
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
    const detail = err.detail;
    const message = Array.isArray(detail)
      ? detail.map((e: { msg?: string }) => e.msg ?? JSON.stringify(e)).join("; ")
      : detail ?? `Request failed: ${res.status}`;
    throw new Error(message);
  }
  return res.json();
}

export async function adminCreateRerenderJob(
  templateId: string,
  sourceJobId: string,
): Promise<TestJobResponse> {
  const res = await adminFetch(`/admin/templates/${templateId}/rerender-job`, {
    method: "POST",
    body: JSON.stringify({ source_job_id: sourceJobId }),
  });
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

// ── Text preview API ─────────────────────────────────────────────────────────

export interface TextPreviewParams {
  subject_text?: string;
  subject_size_px: number;
  subject_y_frac: number;
  subject_color?: string;
  prefix_text?: string;
  prefix_size_px: number;
  prefix_y_frac: number;
  prefix_color?: string;
}

export interface TextPreviewResponse {
  image_base64: string;
  width: number;
  height: number;
}

export async function adminTextPreview(
  templateId: string,
  params: TextPreviewParams,
): Promise<TextPreviewResponse> {
  const res = await adminFetch(`/admin/templates/${templateId}/text-preview`, {
    method: "POST",
    body: JSON.stringify(params),
  });
  return res.json();
}

export async function adminCreateTemplateFromMusicTrack(
  musicTrackId: string,
  name?: string,
): Promise<AdminTemplate> {
  const res = await adminFetch("/admin/templates/from-music-track", {
    method: "POST",
    body: JSON.stringify({ music_track_id: musicTrackId, name }),
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

// ── Music variant (child template) types ────────────────────────────────────

export interface ChildTemplate {
  id: string;
  name: string;
  music_track_id: string;
  track_title: string;
  track_artist: string;
  beat_count: number;
  analysis_status: string;
  published_at: string | null;
  created_at: string;
}

export interface ChildrenListResponse {
  children: ChildTemplate[];
  total: number;
}

// ── Music variant (child template) API calls ────────────────────────────────

export async function adminCreateChildTemplate(
  parentId: string,
  musicTrackId: string,
): Promise<AdminTemplate> {
  const res = await adminFetch(`/admin/templates/${parentId}/children`, {
    method: "POST",
    body: JSON.stringify({ music_track_id: musicTrackId }),
  });
  return res.json();
}

export async function adminListChildren(
  parentId: string,
): Promise<ChildrenListResponse> {
  const res = await adminFetch(`/admin/templates/${parentId}/children`);
  return res.json();
}

export async function adminRemergeChildren(
  parentId: string,
): Promise<{ updated: number }> {
  const res = await adminFetch(`/admin/templates/${parentId}/remerge-children`, {
    method: "POST",
  });
  return res.json();
}

/** List published+ready music tracks for the child template picker. */
export interface MusicTrackPickerItem {
  id: string;
  title: string;
  artist: string;
  duration_s: number | null;
  beat_count: number;
  analysis_status: string;
  published_at: string | null;
}

export async function adminListPublishedMusicTracks(): Promise<MusicTrackPickerItem[]> {
  const res = await adminFetch("/admin/music-tracks?limit=200&offset=0");
  const data = await res.json();
  // Filter to ready + published tracks client-side
  return (data.tracks ?? []).filter(
    (t: MusicTrackPickerItem & { analysis_status: string; published_at: string | null }) =>
      t.analysis_status === "ready" && t.published_at != null,
  );
}

// ── Overlay preview (WYSIWYG editor) ───────────────────────────────────────────

export interface OverlayPreviewParams {
  // Raw recipe overlay dicts — the same shape the export pipeline consumes.
  // Typed loosely on purpose; the editor mirrors the backend's recipe schema
  // and shipping a wider type would lock the two to a single point in time.
  overlays: Array<Record<string, unknown>>;
  slot_duration_s: number;
  time_in_slot_s: number;
  preview_subject?: string;
}

/**
 * Fetch a transparent PNG of the overlay layer at one moment in time.
 *
 * Renders through the same Pillow code path as the export, so the resulting
 * PNG is pixel-identical to the exported video's overlay layer. Caller owns
 * the returned Blob's lifecycle — wrap it with URL.createObjectURL and
 * remember to URL.revokeObjectURL when done.
 */
export async function fetchOverlayPreview(
  params: OverlayPreviewParams,
  init?: { signal?: AbortSignal },
): Promise<Blob> {
  const res = await adminFetch("/admin/overlay-preview", {
    method: "POST",
    body: JSON.stringify(params),
    signal: init?.signal,
  });
  return res.blob();
}
