const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export interface PlatformCopy {
  tiktok: { hook: string; caption: string; hashtags: string[] };
  instagram: { hook: string; caption: string; hashtags: string[] };
  youtube: { title: string; description: string; tags: string[] };
}

/** Normalise video MIME types for GCS signed uploads.
 *
 * GCS presigned URLs are signed against the exact content-type declared at
 * request time. If the browser reports "video/quicktime" (common for .mov and
 * some .mp4 files on macOS/iOS) or an empty string, but the presigned URL was
 * signed for "video/mp4", the PUT returns 403 SignatureDoesNotMatch — which
 * the browser surfaces as a fetch() TypeError ("Cannot reach the server").
 * Normalising to "video/mp4" keeps the signed header in sync with what we send.
 */
export function normaliseMimeType(mime: string | undefined): string {
  if (!mime || mime === "video/quicktime") return "video/mp4";
  return mime;
}

export async function uploadFileToGcs(uploadUrl: string, file: File): Promise<void> {
  const contentType = normaliseMimeType(file.type);
  let res: Response;
  try {
    res = await fetch(uploadUrl, {
      method: "PUT",
      headers: { "Content-Type": contentType },
      body: file,
    });
  } catch {
    throw new Error("Upload failed — network error connecting to storage.");
  }
  if (!res.ok) throw new Error(`GCS upload failed: ${res.status}`);
}

// ── Template job API ────────────────────────────────────────────────────────

export interface TemplateJobCreateResponse {
  job_id: string;
  status: string;
  template_id: string;
}

export type TemplateJobStatus =
  | "queued"
  | "processing"
  | "template_ready"
  | "processing_failed"
  // Admin-initiated cancel via POST /admin/jobs/{id}/cancel.
  // Rendered as a distinct screen on /template-jobs/[id].
  | "cancelled";

export interface AssemblyPlanData {
  // Optional because single_video templates produce no slot-step array —
  // only multi-clip templates have slots. The result page guards on
  // `steps?.length` and skips the timeline + breakdown sections when
  // empty.
  steps?: Array<{
    slot: { position: number; target_duration_s: number; slot_type: string; priority?: number };
    clip_id: string;
    moment: { start_s: number; end_s: number; energy: number; description: string };
  }>;
  output_url?: string;
  base_output_url?: string;
  platform_copy?: PlatformCopy;
  copy_status?: string;
  // single_video plans carry these instead of `steps`
  template_kind?: string;
  body_window?: { start_s: number; end_s: number };
  audio_health?: string[];
  intro_duration_s?: number;
}

// Structured failure taxonomy from the API. Mirrors FAILURE_REASON_*
// constants in src/apps/api/app/tasks/template_orchestrate.py. Frontend
// uses this to choose a specific user-facing message instead of falling
// back to error_detail or "Something went wrong".
export type JobFailureReason =
  | "template_misconfigured"
  | "template_assets_missing"
  | "user_clip_download_failed"
  | "user_clip_unusable"
  | "ffmpeg_failed"
  | "gemini_analysis_failed"
  | "copy_generation_failed"
  | "output_upload_failed"
  | "timeout"
  | "unknown";

/** One completed pipeline phase. Appended to TemplateJobStatusResponse.phase_log
 *  by the worker. The frontend rolls these into a progress bar so the user
 *  sees motion during the multi-second render.
 *
 *  Sub-phases (e.g. per-clip gemini_upload timings inside analyze_clips) are
 *  recorded as entries with a `parent` field set to the parent phase name.
 *  Entries without `parent` are top-level phases. */
export interface PhaseLogEntry {
  name: string;
  elapsed_ms: number | null;
  t_offset_ms: number | null;
  ts: string;
  /** Parent phase name when this entry is a sub-phase (e.g. "analyze_clips"). */
  parent?: string | null;
  /** Free-form detail map (e.g. {clip_idx, clip_path}). */
  detail?: Record<string, unknown> | null;
}

export interface TemplateJobStatusResponse {
  job_id: string;
  status: TemplateJobStatus;
  template_id: string | null;
  assembly_plan: AssemblyPlanData | null;
  error_detail: string | null;
  failure_reason: JobFailureReason | null;
  // Live pipeline phase tracking (migration 0015). Null/[] on legacy rows
  // and during the brief queued → first-phase window.
  current_phase?: string | null;
  phase_log?: PhaseLogEntry[];
  started_at?: string | null;
  finished_at?: string | null;
  created_at: string;
  updated_at: string;
}

export async function getTemplateJobStatus(jobId: string): Promise<TemplateJobStatusResponse> {
  const res = await fetch(`${API_URL}/template-jobs/${jobId}/status`);
  if (!res.ok) throw new Error(`Status fetch failed: ${res.status}`);
  return res.json();
}

/** URL for the SSE events stream. Consumed by `useJobStream`. */
export function getTemplateJobEventsUrl(jobId: string): string {
  return `${API_URL}/template-jobs/${jobId}/events`;
}

// ── Template gallery API ────────────────────────────────────────────────────

export interface RequiredInput {
  key: string;
  label: string;
  placeholder: string;
  max_length: number;
  required: boolean;
}

export async function uploadTemplatePhoto(params: {
  templateId: string;
  slotPosition: number;
  file: File;
}): Promise<{ gcs_path: string; duration_s: number }> {
  const fd = new FormData();
  fd.append("template_id", params.templateId);
  fd.append("slot_position", String(params.slotPosition));
  fd.append("file", params.file);

  let res: Response;
  try {
    res = await fetch(`${API_URL}/uploads/template-photo`, {
      method: "POST",
      body: fd,
    });
  } catch {
    throw new Error("Cannot reach the server. Check your connection and try again.");
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? `Photo upload failed: ${res.status}`);
  }
  return res.json();
}

export interface PlaybackUrlResponse {
  url: string;
  expires_in_s: number;
}

export async function getTemplatePlaybackUrl(
  templateId: string
): Promise<PlaybackUrlResponse> {
  const res = await fetch(`${API_URL}/templates/${templateId}/playback-url`);
  if (!res.ok) throw new Error(`Failed to get playback URL: ${res.status}`);
  return res.json();
}

// ── Reroll + Job list API ───────────────────────────────────────────────────

export async function rerollTemplateJob(
  jobId: string
): Promise<TemplateJobCreateResponse> {
  const res = await fetch(`${API_URL}/template-jobs/${jobId}/reroll`, {
    method: "POST",
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? `Reroll failed: ${res.status}`);
  }
  return res.json();
}

export interface TemplateJobListItem {
  job_id: string;
  status: string;
  template_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface TemplateJobListResponse {
  jobs: TemplateJobListItem[];
  total: number;
}

export async function listTemplateJobs(
  limit = 50,
  offset = 0
): Promise<TemplateJobListResponse> {
  const res = await fetch(
    `${API_URL}/template-jobs?limit=${limit}&offset=${offset}`
  );
  if (!res.ok) throw new Error(`Failed to fetch jobs: ${res.status}`);
  return res.json();
}
