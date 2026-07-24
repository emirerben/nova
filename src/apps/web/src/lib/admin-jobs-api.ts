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

export type JobTypeFilter =
  | "all"
  | "music"
  | "template"
  | "auto_music"
  | "generative"
  | "default";

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
  /** Pipeline wall-clock start. NULL until the worker picks up the task. */
  started_at: string | null;
  /** Seconds since started_at (server-computed). NULL when terminal or queued. */
  time_in_processing_s: number | null;
  /** Celery task_id (= job_id by convention from app/services/job_dispatch.py). */
  celery_task_id: string | null;
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

// Subset of AgentRunPayload used for template/track context runs. These rows do
// not carry input_json/output_json/raw_text because the backend defers those
// columns to keep popular-template debug payloads under the proxy budget.
export interface AgentRunSummaryPayload {
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

export interface RenderSummary {
  trace_id: string | null;
  total_queue_ms: number | null;
  total_processing_ms: number | null;
  slowest_stages: Array<{
    stage: string;
    elapsed_ms: number;
    status?: string;
    variant_id?: string;
  }>;
  repeated_stages: Array<{ stage: string; count: number }>;
  retries: Array<{
    stage: string;
    status?: string;
    attempt?: number;
    retry?: Record<string, unknown>;
  }>;
  cache: Record<string, Record<string, number>>;
  attempts: Array<{
    trace_id: string | null;
    render_generation_id: string | null;
    stage_count: number;
    elapsed_ms: number;
    variants: string[];
  }>;
  agent_work_ms?: number | null;
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
  celery_task_id: string | null;
}

/**
 * Live Celery/broker state for one job. Drives the admin Worker state panel.
 *
 * - active   : Celery reports the task as currently running on `worker`.
 * - reserved : Sitting in the broker queue, waiting for a worker. `queue_position`
 *              is its index in the queue (0 = next up).
 * - not_found: Not in any worker's active/reserved set AND no broker error —
 *              the worker likely died mid-task. UI should suggest cancel/reaper.
 * - unknown  : inspect() failed (broker unreachable). UI must distinguish this
 *              from not_found — claiming "not_found" without asking the broker
 *              would let an operator cancel a healthy job.
 */
export type JobRuntimeState =
  | "active"
  | "reserved"
  | "not_found"
  | "unknown";

export interface JobRuntimePayload {
  state: JobRuntimeState;
  worker: string | null;
  task_id: string | null;
  queue_position: number | null;
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
  recipe_cached: unknown;
}

export interface JobDebugResponse {
  job: JobPayload;
  job_clips: JobClipPayload[];
  template: TemplateSummary | null;
  music_track: MusicTrackSummary | null;
  /** Agent runs that ran inside this job's Celery task (clip_metadata, text_designer, etc.) */
  agent_runs: AgentRunPayload[];
  /** Agent runs that shaped the linked template's recipe (template_recipe, creative_direction, etc.) */
  template_agent_runs: AgentRunSummaryPayload[];
  /** Agent runs that analyzed the linked music track (song_classifier, song_sections, music_matcher, etc.) */
  track_agent_runs: AgentRunSummaryPayload[];
  template_agent_runs_has_more: boolean;
  track_agent_runs_has_more: boolean;
  context_runs_cap: number;
  runtime: JobRuntimePayload;
  render_summary?: RenderSummary | null;
}

export interface QueueInfoPayload {
  name: string;
  depth: number;
  oldest_pending_job_id: string | null;
}

export interface QueueSnapshotResponse {
  queues: QueueInfoPayload[];
  active_workers: string[];
  /** False when inspect() failed — UI renders "broker unreachable" instead of "0 queued". */
  ok: boolean;
}

export interface CancelJobResponse {
  job_id: string;
  previous_status: string;
  status: string;
  task_id: string | null;
  revoke_sent: boolean;
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

export async function adminGetQueueState(): Promise<QueueSnapshotResponse> {
  return _adminJson<QueueSnapshotResponse>(`/jobs/queue-state`);
}

export async function adminCancelJob(jobId: string): Promise<CancelJobResponse> {
  const res = await fetch(`${ADMIN_PROXY}/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
  });
  if (!res.ok) {
    let detail = `Cancel failed: ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // ignore
    }
    throw new Error(detail);
  }
  return (await res.json()) as CancelJobResponse;
}
