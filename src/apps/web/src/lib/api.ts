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

/**
 * Upload a file to GCS with real byte-level progress feedback.
 * The signed URL is already content-type-bound, so the header must match exactly.
 */
export function uploadFileToGcsWithProgress(
  uploadUrl: string,
  file: File,
  onProgress: (fraction: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", uploadUrl, true);
    xhr.setRequestHeader("Content-Type", normaliseMimeType(file.type));
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error(`GCS upload failed: ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("GCS upload network error"));
    xhr.send(file);
  });
}

// ── Google Drive Import API ────────────────────────────────────────────────

export interface DriveImportBatchResponse {
  batch_id: string;
  gcs_paths: string[];
  status: string;
}

export interface DriveImportBatchStatusResponse {
  batch_id: string;
  status: "importing" | "complete" | "partial_failure" | "failed";
  total: number;
  completed: number;
  current_file: string | null;
  gcs_paths: string[];
  errors: string[];
}

export async function importBatchFromDrive(params: {
  files: Array<{
    drive_file_id: string;
    filename: string;
    file_size_bytes: number;
    mime_type: string;
  }>;
  google_access_token: string;
  compress?: boolean;
}): Promise<DriveImportBatchResponse> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/uploads/drive-import-batch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    });
  } catch {
    throw new Error("Cannot reach the server. Make sure the API is running.");
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? `Batch Drive import failed: ${res.status}`);
  }
  return res.json();
}

export async function getDriveImportBatchStatus(
  batchId: string
): Promise<DriveImportBatchStatusResponse> {
  const res = await fetch(`${API_URL}/uploads/drive-import-batch/${batchId}/status`);
  if (!res.ok) throw new Error(`Batch status fetch failed: ${res.status}`);
  return res.json();
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
  // Advisory per-phase duration baselines in ms for the ETA ladder (D18).
  expected_phase_durations?: Record<string, number> | null;
}

/** Structured error detail returned by the upload-time pre-flight when one
 * or more clips exceed the pipeline's empirical cost budget for 10-bit HDR
 * footage. See `app/services/template_validation.py` for the empirical record. */
export interface ClipTooLongFor10BitDetail {
  code: "clip_too_long_for_10bit";
  clip_index: number;
  duration_s: number;
  limit_s: number;
  pix_fmt: string;
  message: string;
  offenders: { clip_index: number; duration_s: number; pix_fmt: string }[];
}

/** Thrown by createTemplateJob when the API returns a structured (dict) error
 * detail. Lets callers inspect `code` and render a per-clip remediation UI
 * instead of dumping `[object Object]` into the error banner. */
export class TemplateJobCreateError extends Error {
  detail: ClipTooLongFor10BitDetail | { code?: string; [k: string]: unknown } | string | null;
  status: number;

  constructor(
    message: string,
    detail: TemplateJobCreateError["detail"],
    status: number,
  ) {
    super(message);
    this.name = "TemplateJobCreateError";
    this.detail = detail;
    this.status = status;
  }
}

export async function createTemplateJob(params: {
  template_id: string;
  clip_gcs_paths: string[];
  // Per-clip durations in seconds (HTMLVideoElement.duration on file select).
  // Backend uses sum to reject submissions that can't fill the template's
  // audio length. Optional for backward-compat.
  clip_durations?: number[];
  selected_platforms: string[];
  inputs?: Record<string, string>;
}): Promise<TemplateJobCreateResponse> {
  const init: RequestInit = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  };
  let res: Response;
  try {
    res = await fetch(`${API_URL}/template-jobs`, init);
  } catch {
    // One retry: covers brief api unavailability (deploys, crash-restarts).
    // 2s gives Fly time to route past a momentarily-unhealthy instance.
    //
    // Trade-off: POST /template-jobs has no idempotency key, so if the
    // first attempt actually reached the server but the response was lost
    // (rare: connection reset mid-response), the retry creates a second
    // job. The dominant failure mode is CORS-preflight failure / connect
    // refused during a Firecracker reboot, where the POST never reaches
    // the server — safe to retry. To close the duplicate-job window,
    // add an idempotency key to CreateTemplateJobRequest in
    // app/routes/template_jobs.py.
    await new Promise((r) => setTimeout(r, 2000));
    try {
      res = await fetch(`${API_URL}/template-jobs`, init);
    } catch {
      throw new Error("Cannot reach the server. Make sure the API is running.");
    }
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = body?.detail ?? null;
    // Structured detail (FastAPI returns a dict for our pre-flight checks).
    // Extract the user-visible message for `Error.message` but keep the full
    // object on `.detail` so the caller can render per-clip UI.
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      const message =
        (detail as { message?: string }).message ??
        `Template job creation failed: ${res.status}`;
      throw new TemplateJobCreateError(message, detail, res.status);
    }
    const message =
      typeof detail === "string"
        ? detail
        : `Template job creation failed: ${res.status}`;
    throw new TemplateJobCreateError(message, detail, res.status);
  }
  return res.json();
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

/** Fire-and-forget: kick off pre-emptive Gemini analysis for a clip that
 *  just finished its presigned PUT. The server returns 202 immediately and
 *  runs the upload+analyse in the background. By the time the user clicks
 *  Generate, the result is already in Redis and the orchestrator skips
 *  Gemini entirely for this clip.
 *
 *  Errors are swallowed by design — prefetch is an optimisation, not a
 *  correctness step. If it fails, the orchestrator does the same work
 *  on the critical path (same behaviour as before this hook existed).
 */
export function prefetchClipAnalyze(
  gcsPath: string,
  templateId: string,
): void {
  void fetch(`${API_URL}/clips/prefetch-analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ gcs_path: gcsPath, template_id: templateId }),
    // keepalive lets the request survive a tab-close mid-fire so the server
    // still kicks off the prefetch. The user might Cmd+Tab away the second
    // their last clip finishes uploading; we still want the analysis warm
    // when they come back.
    keepalive: true,
  }).catch(() => {
    // Intentionally silent. Logging here would spam the user's console
    // for failures they can't act on. The backend logs prefetch_* events
    // for observability.
  });
}

// ── Batch presigned + template gallery API ──────────────────────────────────

export interface BatchPresignedFile {
  filename: string;
  content_type: string;
  file_size_bytes: number;
}

export interface BatchPresignedUrl {
  upload_url: string;
  gcs_path: string;
}

export interface BatchPresignedResponse {
  urls: BatchPresignedUrl[];
}

export async function getBatchPresignedUrls(
  files: BatchPresignedFile[]
): Promise<BatchPresignedResponse> {
  // Normalise MIME types before sending — the presigned URL will be signed
  // against exactly this content-type, so it must match what uploadFileToGcs sends.
  const normalisedFiles = files.map((f) => ({
    ...f,
    content_type: normaliseMimeType(f.content_type),
  }));
  let res: Response;
  try {
    res = await fetch(`${API_URL}/presigned-urls`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ files: normalisedFiles }),
    });
  } catch {
    throw new Error("Cannot reach the server. Make sure the API is running.");
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? `Presigned URL generation failed: ${res.status}`);
  }
  return res.json();
}

export interface SlotSummary {
  position: number;
  target_duration_s: number;
  media_type: "video" | "photo";
}

export interface RequiredInput {
  key: string;
  label: string;
  placeholder: string;
  max_length: number;
  required: boolean;
}

export interface TemplateListItem {
  id: string;
  name: string;
  gcs_path: string;
  analysis_status: string;
  slot_count: number;
  total_duration_s: number;
  copy_tone: string;
  thumbnail_url: string | null;
  required_clips_min: number;
  required_clips_max: number;
  slots: SlotSummary[];
  required_inputs: RequiredInput[];
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

export async function listTemplates(): Promise<TemplateListItem[]> {
  const res = await fetch(`${API_URL}/templates`);
  if (!res.ok) throw new Error(`Failed to fetch templates: ${res.status}`);
  return res.json();
}

export class TemplateNotFoundError extends Error {
  constructor(templateId: string) {
    super(`Template not found: ${templateId}`);
    this.name = "TemplateNotFoundError";
  }
}

// Used by /template/[id] for direct/shareable URLs. Throws TemplateNotFoundError
// on 404 so the page can render a friendly fallback instead of bubbling to
// the global error boundary.
export async function getTemplate(templateId: string): Promise<TemplateListItem> {
  const res = await fetch(`${API_URL}/templates/${encodeURIComponent(templateId)}`);
  if (res.status === 404) throw new TemplateNotFoundError(templateId);
  if (!res.ok) throw new Error(`Failed to fetch template: ${res.status}`);
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
