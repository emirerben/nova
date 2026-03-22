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
  const res = await fetch(`${API_URL}/uploads/presigned`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
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
