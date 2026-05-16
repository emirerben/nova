/**
 * Typed client for the admin job-debug endpoints.
 *
 * All calls go through the Next.js admin proxy (`/api/admin/[...]`) so the
 * admin token never reaches the browser bundle. Backend route source:
 *   src/apps/api/app/routes/admin_jobs.py
 *
 * Keep these types in sync with the Pydantic response models in that file.
 */

const ADMIN_PROXY = "/api/admin";

// ── Shared shapes ─────────────────────────────────────────────────────────────

export type JobTypeFilter = "all" | "music" | "template" | "auto_music" | "default";

export interface AdminJobListItem {
  job_id: string;
  job_type: string;
  mode: string | null;
  status: string;
  template_id: string | null;
  music_track_id: string | null;
  failure_reason: string | null;
  created_at: string;
  updated_at: string;
  agent_run_count: number;
  failure_count: number;
}

export interface AdminJobListResponse {
  items: AdminJobListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface AgentRunPayload {
  id: string;
  segment_idx: number | null;
  agent_name: string;
  prompt_version: string;
  model: string;
  outcome: string;
  attempts: number;
  tokens_in: number | null;
  tokens_out: number | null;
  cost_usd: number | null;
  latency_ms: number | null;
  error_message: string | null;
  input_json: unknown;
  output_json: unknown;
  raw_text: string | null;
  created_at: string;
}

export interface JobClipPayload {
  id: string;
  rank: number;
  hook_score: number;
  engagement_score: number;
  combined_score: number;
  start_s: number;
  end_s: number;
  hook_text: string | null;
  platform_copy: unknown;
  copy_status: string;
  video_path: string | null;
  render_status: string;
  error_detail: string | null;
  music_track_id: string | null;
  match_score: number | null;
  match_rationale: string | null;
}

export interface PipelineTraceEvent {
  ts: string;
  stage: string;
  event: string;
  data: Record<string, unknown>;
}

export interface JobPayload {
  id: string;
  user_id: string;
  status: string;
  job_type: string;
  mode: string | null;
  template_id: string | null;
  music_track_id: string | null;
  failure_reason: string | null;
  error_detail: string | null;
  current_phase: string | null;
  phase_log: unknown;
  raw_storage_path: string | null;
  selected_platforms: string[] | null;
  probe_metadata: unknown;
  transcript: unknown;
  scene_cuts: unknown;
  all_candidates: unknown;
  assembly_plan: unknown;
  pipeline_trace: PipelineTraceEvent[] | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface TemplateSummary {
  id: string;
  name: string;
  analysis_status: string;
  recipe_cached: unknown;
  audio_gcs_path: string | null;
  error_detail: string | null;
}

export interface MusicTrackSummary {
  id: string;
  title: string;
  artist: string;
  analysis_status: string;
  audio_gcs_path: string | null;
  track_config: unknown;
  recipe_cached: unknown;
  beat_timestamps_s: unknown;
  ai_labels: unknown;
  best_sections: unknown;
  error_detail: string | null;
}

export interface JobDebugResponse {
  job: JobPayload;
  job_clips: JobClipPayload[];
  template: TemplateSummary | null;
  music_track: MusicTrackSummary | null;
  agent_runs: AgentRunPayload[];
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

export interface ListJobsParams {
  jobType?: JobTypeFilter;
  status?: string;
  onlyFailures?: boolean;
  limit?: number;
  offset?: number;
}

export async function adminListJobs(
  params: ListJobsParams = {},
): Promise<AdminJobListResponse> {
  const qs = new URLSearchParams();
  if (params.jobType && params.jobType !== "all") qs.set("job_type", params.jobType);
  if (params.status) qs.set("status", params.status);
  if (params.onlyFailures) qs.set("only_failures", "true");
  qs.set("limit", String(params.limit ?? 50));
  qs.set("offset", String(params.offset ?? 0));
  return _adminJson<AdminJobListResponse>(`/jobs?${qs.toString()}`);
}

export async function adminGetJobDebug(jobId: string): Promise<JobDebugResponse> {
  return _adminJson<JobDebugResponse>(`/jobs/${encodeURIComponent(jobId)}/debug`);
}
