const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export interface PresignedResponse {
  upload_url: string;
  job_id: string;
  gcs_path: string;
}

export interface ClipStatus {
  id: string;
  rank: number;
  hook_score: number;
  engagement_score: number;
  combined_score: number;
  start_s: number;
  end_s: number;
  hook_text: string | null;
  render_status: "pending" | "rendering" | "ready" | "failed";
  video_path: string | null;
  thumbnail_path: string | null;
  duration_s: number | null;
  platform_copy: PlatformCopy | null;
  copy_status: "generated" | "generated_fallback" | "edited";
  post_status: Record<string, string> | null;
}

export interface PlatformCopy {
  tiktok: { hook: string; caption: string; hashtags: string[] };
  instagram: { hook: string; caption: string; hashtags: string[] };
  youtube: { title: string; description: string; tags: string[] };
}

export type JobStatus =
  | "queued"
  | "processing"
  | "clips_ready"
  | "clips_ready_partial"
  | "posting"
  | "posting_partial"
  | "done"
  | "posting_failed"
  | "processing_failed";

export interface JobStatusResponse {
  id: string;
  status: JobStatus;
  clips: ClipStatus[];
  error_detail: string | null;
  created_at: string;
  updated_at: string;
}

export async function getPresignedUrl(params: {
  filename: string;
  file_size_bytes: number;
  duration_s: number;
  aspect_ratio: string;
  platforms: string[];
  content_type: string;
}): Promise<PresignedResponse> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/uploads/presigned`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    });
  } catch {
    throw new Error("Cannot reach the server. Make sure the API is running (`docker-compose up`).");
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? `Upload failed: ${res.status}`);
  }
  return res.json();
}

export async function enqueueJob(jobId: string, rawPath: string, platforms: string[]): Promise<void> {
  const res = await fetch(`${API_URL}/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: jobId, raw_storage_path: rawPath, platforms }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? `Enqueue failed: ${res.status}`);
  }
}

export async function getJobStatus(jobId: string): Promise<JobStatusResponse> {
  const res = await fetch(`${API_URL}/jobs/${jobId}/status`);
  if (!res.ok) throw new Error(`Status fetch failed: ${res.status}`);
  return res.json();
}

export async function uploadFileToGcs(uploadUrl: string, file: File): Promise<void> {
  const res = await fetch(uploadUrl, {
    method: "PUT",
    headers: { "Content-Type": file.type },
    body: file,
  });
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
  | "processing_failed";

export interface AssemblyPlanData {
  steps: Array<{
    slot: { position: number; target_duration_s: number; slot_type: string };
    clip_id: string;
    moment: { start_s: number; end_s: number; energy: number; description: string };
  }>;
  output_url?: string;
  platform_copy?: PlatformCopy;
  copy_status?: string;
}

export interface TemplateJobStatusResponse {
  job_id: string;
  status: TemplateJobStatus;
  template_id: string | null;
  assembly_plan: AssemblyPlanData | null;
  error_detail: string | null;
  created_at: string;
  updated_at: string;
}

export async function createTemplateJob(params: {
  template_id: string;
  clip_gcs_paths: string[];
  selected_platforms: string[];
}): Promise<TemplateJobCreateResponse> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/template-jobs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    });
  } catch {
    throw new Error("Cannot reach the server. Make sure the API is running.");
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? `Template job creation failed: ${res.status}`);
  }
  return res.json();
}

export async function getTemplateJobStatus(jobId: string): Promise<TemplateJobStatusResponse> {
  const res = await fetch(`${API_URL}/template-jobs/${jobId}/status`);
  if (!res.ok) throw new Error(`Status fetch failed: ${res.status}`);
  return res.json();
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
  let res: Response;
  try {
    res = await fetch(`${API_URL}/presigned-urls`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ files }),
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

export interface TemplateListItem {
  id: string;
  name: string;
  gcs_path: string;
  analysis_status: string;
  slot_count: number;
  total_duration_s: number;
  copy_tone: string;
  thumbnail_url: string | null;
}

export async function listTemplates(): Promise<TemplateListItem[]> {
  const res = await fetch(`${API_URL}/templates`);
  if (!res.ok) throw new Error(`Failed to fetch templates: ${res.status}`);
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
