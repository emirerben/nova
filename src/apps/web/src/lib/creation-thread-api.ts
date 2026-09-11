import { uploadContentTypeForFile, uploadToGcs } from "@/lib/plan-api";
import type {
  KriaApprovalDecision,
  KriaApprovalSnapshot,
  KriaDelta,
  KriaDraftSnapshot,
  KriaProblem,
  KriaTurnAccepted,
  TurnCancelled,
} from "@/lib/kria-runtime-v2.generated";

export type {
  KriaApprovalDecision,
  KriaApprovalSnapshot,
  KriaDelta,
  KriaDraftSnapshot,
  KriaProblem,
  KriaTurnAccepted,
  TurnCancelled,
} from "@/lib/kria-runtime-v2.generated";

const BASE = "/api/plan/creation-threads";

export type CreationFormat = "montage" | "narrated_planned" | "subtitled" | "slides";
export type CreationAction =
  | "select_format"
  | "select_edit_format"
  | "send_message"
  | "generate"
  | "revise"
  | "retry"
  | "retry_speech_cleanup"
  | "create_without_cleanup"
  | "remove_media"
  | "select_variant"
  | "archive";

export interface CreationThreadEvent {
  id: string;
  sequence: number;
  revision: number;
  role: "user" | "assistant" | "system";
  event_type: string;
  content: string | null;
  payload: Record<string, unknown> | null;
  created_at: string;
}

export interface CreationVariant {
  variant_id?: string;
  render_status?: string;
  render_generation_id?: string | null;
  render_finished_at?: string | null;
  output_url?: string | null;
  poster_url?: string | null;
  failure_reason?: string | null;
  [key: string]: unknown;
}

export interface CreationJob {
  id: string;
  status: string;
  current_phase?: string | null;
  failure_reason?: string | null;
  variants: CreationVariant[];
}

export type CreatorDirectionReceiptStatus =
  | "enforced"
  | "advisory"
  | "unsupported"
  | "conflicted";

export interface CreatorDirectionReceiptRule {
  id?: string | null;
  normalized_key?: string | null;
  label?: string | null;
  display_text?: string | null;
  instruction?: string | null;
  status?: CreatorDirectionReceiptStatus | null;
  enforcement_status?: CreatorDirectionReceiptStatus | null;
  scope?: "account" | "project" | string | null;
  scope_label?: string | null;
  source_label?: string | null;
  reason?: string | null;
  conflict_message?: string | null;
  overridden?: boolean;
}

export interface CreatorDirectionReceipt {
  enabled: boolean;
  memory_revision?: number;
  applied_count: number;
  enforced_count: number;
  advisory_count: number;
  unsupported_count: number;
  conflicted_count: number;
  rules?: CreatorDirectionReceiptRule[] | null;
  /** Future-compatible aliases used by staged backends. */
  applied_rules?: CreatorDirectionReceiptRule[] | null;
  items?: CreatorDirectionReceiptRule[] | null;
}

export type CreationSpeechCleanupAnalysisStatus =
  | "queued"
  | "running"
  | "ready"
  | "no_findings"
  | "failed";

export type CreationSpeechCleanupChoice = "clean" | "keep_original";
export type CreationSpeechCleanupDecision =
  | CreationSpeechCleanupChoice
  | "create_without_cleanup";

export type CreationSpeechCleanupOutcomeStatus =
  | "applied"
  | "checked_no_change"
  | "declined"
  | "bypassed_unchecked"
  | "failed";

export interface CreationSpeechCleanupError {
  code: string;
  retryable: boolean;
}

export interface CreationSpeechCleanupAnalysis {
  id: string;
  status: CreationSpeechCleanupAnalysisStatus;
  detector_version?: string | null;
  has_findings?: boolean | null;
  candidate_count?: number | null;
  category_counts?: Record<string, number> | null;
  estimated_removed_ms?: number | null;
  /** Length of the analyzed narration window. Null on rows without a window. */
  source_duration_ms?: number | null;
  /** What the take becomes if the cleanup is accepted. Null until estimated. */
  result_duration_ms?: number | null;
  error?: CreationSpeechCleanupError | null;
}

export interface CreationSpeechCleanupOutcome {
  job_id: string;
  render_generation_id?: string | null;
  status: CreationSpeechCleanupOutcomeStatus;
  removal_count?: number | null;
  removed_ms?: number | null;
  error?: CreationSpeechCleanupError | null;
}

export interface CreationSpeechCleanupProjection {
  applicable: boolean;
  unavailable_reason?: string | null;
  analysis?: CreationSpeechCleanupAnalysis | null;
  decision?: CreationSpeechCleanupDecision | null;
  requires_choice?: boolean;
  render_blocker?: "video_required" | null;
  outcome?: CreationSpeechCleanupOutcome | null;
}

const PLAYABLE_VARIANT_STATUSES = new Set(["ready", "failed", "error", "render_failed"]);
const FAILED_VARIANT_STATUSES = new Set(["failed", "error", "render_failed"]);

export function creationVariantPlayable(variant: CreationVariant): boolean {
  return Boolean(variant.output_url) && PLAYABLE_VARIANT_STATUSES.has(String(variant.render_status));
}

export function creationVariantFailed(variant: CreationVariant): boolean {
  return FAILED_VARIANT_STATUSES.has(String(variant.render_status));
}

export interface CreationThread {
  id: string;
  /** Immutable controller owner. Omitted only during old-server deploy skew. */
  runtime_version?: 1 | 2;
  /** User-authored project label. Older rows may omit it while they hydrate. */
  title?: string | null;
  status: "active" | "archived" | "failed";
  revision: number;
  state: Record<string, unknown>;
  content_plan_id: string | null;
  active_plan_item_id: string | null;
  active_creator_agent_session_id: string | null;
  active_job_id: string | null;
  creator_agent?: {
    status?: string | null;
    revision?: number;
    summary?: string | null;
    plan_hash?: string | null;
    version?: string | null;
    [key: string]: unknown;
  } | null;
  media_capabilities?: CreationMediaCapabilities | null;
  direction_receipt?: CreatorDirectionReceipt | null;
  /** Detail-only projection. Older APIs and list summaries omit it. */
  speech_cleanup?: CreationSpeechCleanupProjection | null;
  /**
   * Present only when a render-graph edge (PlanItem/CreatorAgentSession/Job
   * ownership) has drifted incoherent and was dropped rather than hiding
   * this whole project. `active_*` ids for a detached edge are already null
   * in that case -- this only names which one and why, for a quiet notice.
   */
  integrity?: {
    status: "degraded";
    detached: string[];
    codes: string[];
  } | null;
  events: CreationThreadEvent[];
  job: CreationJob | null;
  created_at: string;
  updated_at: string;
}

export function creationDirectionReceiptLabel(thread: CreationThread | null): string | null {
  const receipt = thread?.direction_receipt;
  if (!receipt?.enabled || receipt.applied_count < 1) return null;
  const detail = [
    receipt.enforced_count ? `${receipt.enforced_count} enforced` : null,
    receipt.advisory_count ? `${receipt.advisory_count} advisory` : null,
    receipt.unsupported_count ? `${receipt.unsupported_count} unsupported` : null,
    receipt.conflicted_count ? `${receipt.conflicted_count} conflicted` : null,
  ].filter(Boolean).join(", ");
  return `Personalization · ${receipt.applied_count} applied${detail ? ` (${detail})` : ""}`;
}

export interface DirectionOverrideResponse {
  id: string;
  thread_id?: string;
  normalized_key: string;
  instruction: string;
  structured_value?: Record<string, unknown> | null;
  revision: number;
  operation_id?: string;
  undo_expires_at?: string | null;
  direction_receipt?: CreatorDirectionReceipt | null;
}

function directionOverrideIdempotencyKey(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

/** Save a rule override that applies only to this creation-thread project. */
export function setCreationThreadDirectionOverride(
  threadId: string,
  body: {
    normalized_key: string;
    instruction: string;
    structured_value?: Record<string, unknown> | null;
    expected_revision: number;
  },
): Promise<DirectionOverrideResponse> {
  return request<DirectionOverrideResponse>(
    `/${encodeURIComponent(threadId)}/direction-overrides`,
    {
      method: "POST",
      body: JSON.stringify({ ...body, idempotency_key: directionOverrideIdempotencyKey() }),
    },
  );
}

/** Only background work blocks deletion; an open conversation may be idle. */
export function creationProjectDeletionReason(thread: CreationThread): string | null {
  if (thread.job ? !creationJobSettled(thread) :
    [thread.state.job_status, thread.state.render_status].some((value) =>
      typeof value === "string" && ["queued", "processing", "generating", "rendering"].includes(value.toLowerCase()))) {
    return "Wait for the active render before deleting this project.";
  }
  if (["planning", "executing", "rendering", "reviewing", "revising"].includes(
    thread.creator_agent?.status?.toLowerCase() ?? "",
  )) return "Wait for Kria to finish working before deleting this project.";
  return null;
}

/** Remove a project-only override and restore the account preference. */
export function clearCreationThreadDirectionOverride(
  threadId: string,
  normalizedKey: string,
  expectedRevision: number,
): Promise<{
  revision: number;
  operation_id?: string;
  undo_expires_at?: string | null;
  direction_receipt?: CreatorDirectionReceipt | null;
}> {
  return request<{
    revision: number;
    operation_id?: string;
    undo_expires_at?: string | null;
    direction_receipt?: CreatorDirectionReceipt | null;
  }>(
    `/${encodeURIComponent(threadId)}/direction-overrides/${encodeURIComponent(normalizedKey)}`,
    {
      method: "DELETE",
      body: JSON.stringify({
        expected_revision: expectedRevision,
        idempotency_key: directionOverrideIdempotencyKey(),
      }),
    },
  );
}

export interface CreationUploadTarget {
  media_id: string;
  upload_url: string;
  /** Legacy upload targets exposed a path; new targets intentionally keep it opaque. */
  gcs_path?: string;
  content_type: string;
  upload_headers?: Record<string, string>;
}

export interface CreationCapability {
  id: string;
  edit_format: CreationFormat;
  /** The PlanItem clip contract. Older APIs omit this and the canonical
   *  50-clip PlanItem ceiling is used until capabilities are refreshed. */
  max_clips?: number;
  clip_limit?: number;
  limits?: {
    max_clips?: number;
    clips?: number;
    max_visuals?: number;
    visuals?: number;
  };
}

export interface CreationMediaCapabilities {
  clips?: {
    current?: number;
    max?: number;
    server_max?: number;
    max_file_bytes?: number;
    content_types?: string[];
    format?: CreationFormat;
  };
  visuals?: {
    current?: number;
    max?: number;
    max_file_bytes?: { image?: number; video?: number };
    content_types?: string[];
  };
  voiceover?: {
    current?: number;
    max?: number;
    max_file_bytes?: number;
    content_types?: string[];
  };
}

export interface CreationCapabilitiesResponse {
  formats: CreationCapability[];
  media?: CreationMediaCapabilities;
}

export class CreationThreadError extends Error {
  readonly status: number;
  readonly problem?: KriaProblem;
  /**
   * Failure classification, normalized across both wire shapes: the typed
   * `{problem: {code, retryable}}` envelope from KriaFailureRoute (thread
   * load failures), and the flat `{code, retryable}` shape the same-origin
   * proxy (api-proxy.ts) writes for its own boundary failures (offline
   * upstream, missing internal key, 5xx). Prefer `problem` when both are
   * absent so existing call sites reading `.problem` see no change.
   */
  readonly code?: string;
  readonly retryable?: boolean;
  constructor(
    message: string,
    status: number,
    problem?: KriaProblem,
    flat?: { code?: string; retryable?: boolean },
  ) {
    super(message);
    this.name = "CreationThreadError";
    this.status = status;
    this.problem = problem;
    this.code = problem?.code ?? flat?.code;
    this.retryable = problem?.retryable ?? flat?.retryable;
  }
}

function id(prefix: string): string {
  return globalThis.crypto?.randomUUID?.() ?? `${prefix}-${Date.now()}-${Math.random()}`;
}

interface UploadReservation {
  target: CreationUploadTarget;
  uploaded: boolean;
  clientEventId: string;
}

const uploadReservations = new WeakMap<File, UploadReservation>();

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    let problem: KriaProblem | undefined;
    let flat: { code?: string; retryable?: boolean } | undefined;
    try {
      const body = (await response.json()) as {
        detail?: string;
        problem?: KriaProblem;
        code?: string;
        retryable?: boolean;
      };
      problem = body.problem;
      if (typeof body.code === "string") flat = { code: body.code, retryable: body.retryable };
      if (problem?.message) message = problem.message;
      else if (body.detail) message = body.detail;
    } catch {
      // Keep the HTTP status when a proxy returns a non-JSON response.
    }
    throw new CreationThreadError(message, response.status, problem, flat);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function creationFormat(value: unknown): CreationFormat | null {
  return value === "montage"
      || value === "narrated_planned"
      || value === "subtitled"
      || value === "slides"
    ? value
    : null;
}

export function creationFormatLabel(value: CreationFormat | null): string {
  if (value === "narrated_planned") return "Narrated";
  if (value === "subtitled") return "Talking to camera";
  if (value === "slides") return "Photo & video post";
  return "Montage";
}

/** PlanItem's shared upload ceiling. Kept here as a compatibility default for
 * deploy skew; the server capability response wins whenever it provides one. */
export const PLAN_ITEM_CLIP_LIMIT = 50;

export function creationClipLimit(
  capabilities: CreationCapability[],
  format: CreationFormat | null,
): number {
  if (!format) return PLAN_ITEM_CLIP_LIMIT;
  const capability = capabilities.find((item) => item.edit_format === format);
  const candidate = capability?.max_clips
    ?? capability?.clip_limit
    ?? capability?.limits?.max_clips
    ?? capability?.limits?.clips;
  return typeof candidate === "number" && Number.isFinite(candidate) && candidate > 0
    ? Math.floor(candidate)
    : PLAN_ITEM_CLIP_LIMIT;
}

export function creationThreadMediaCount(thread: CreationThread | null): number {
  const persistedCount = thread?.state.media_count;
  if (typeof persistedCount === "number" && Number.isFinite(persistedCount)) return Math.max(0, persistedCount);
  const media = thread?.state.media;
  return Array.isArray(media) ? media.length : 0;
}

export function creationJobReady(thread: CreationThread): boolean {
  return Boolean(thread.job && thread.job.variants.some(creationVariantPlayable));
}

export function creationJobFailed(thread: CreationThread): boolean {
  return Boolean(thread.job && [
    "failed", "processing_failed", "variants_failed", "matching_failed",
    "no_labeled_tracks", "posting_failed", "cancelled", "error",
  ].includes(thread.job.status));
}

export function creationJobPartial(thread: CreationThread): boolean {
  const variants = thread.job?.variants ?? [];
  return variants.some(creationVariantPlayable) && variants.some(creationVariantFailed);
}

export function creationJobSettled(thread: CreationThread): boolean {
  if (!thread.job) return false;
  // A terminal parent failure wins over stale per-variant rendering metadata.
  // Workers can fail between the variant and parent status writes; polling
  // must stop and expose the failure instead of spinning forever.
  if (creationJobFailed(thread)) return true;
  if (thread.job.variants.some((variant) => variant.render_status === "rendering")) return false;
  return [
    "ready", "done", "variants_ready", "variants_ready_partial", "clips_ready",
    "template_ready", "music_ready",
  ].includes(thread.job.status);
}

/** Whether Creator planning failed before a render Job was created. */
export function creationPlanningFailed(thread: CreationThread): boolean {
  if (thread.job) return false;
  const status = thread.creator_agent?.status
    ?? (thread.state.creator_agent && typeof thread.state.creator_agent === "object"
      ? (thread.state.creator_agent as { status?: unknown }).status
      : null);
  if (typeof status === "string" && status) {
    return ["failed", "error"].includes(status.toLowerCase());
  }
  // Legacy/deploy-skew responses may omit the projected Creator status. In
  // that case only the newest relevant event is authoritative: an old error
  // must not poison the fresh session started by a later user direction.
  const planningLifecycleEvents = new Set([
    "user_message",
    "assistant_error",
    "agent_assistant_error",
    "agent_assistant_strategy",
    "agent_assistant_execution",
    "agent_user_confirmation",
    "generation_started",
    "generation_failed",
    "generation_ready",
  ]);
  const latest = [...thread.events]
    .filter((event) => planningLifecycleEvents.has(event.event_type))
    .sort((left, right) => right.sequence - left.sequence)[0];
  return Boolean(latest && ["assistant_error", "agent_assistant_error"].includes(latest.event_type));
}

/** Return the latest user-authored direction for the composer recovery action. */
export function latestCreationDirection(thread: CreationThread): string {
  const events = [...thread.events].sort((left, right) => right.sequence - left.sequence);
  for (const event of events) {
    if (event.role !== "user" || event.event_type !== "user_message") continue;
    const payload = event.payload ?? {};
    const content = event.content ?? (typeof payload.message === "string" ? payload.message : null);
    if (content?.trim()) return content.trim();
  }
  return "";
}

const CREATOR_PROGRESS_STATES = new Set(["executing", "rendering", "reviewing"]);
const GENERATION_PROGRESS_STATES = new Set(["queued", "rendering"]);

function creatorStatus(thread: CreationThread): string | null {
  const direct = thread.creator_agent?.status;
  if (typeof direct === "string") return direct;
  const projected = thread.state.creator_agent;
  if (projected && typeof projected === "object" && "status" in projected) {
    const status = (projected as { status?: unknown }).status;
    return typeof status === "string" ? status : null;
  }
  return null;
}

function generationStatus(thread: CreationThread): string | null {
  const generation = thread.state.generation;
  if (generation && typeof generation === "object" && "status" in generation) {
    const status = (generation as { status?: unknown }).status;
    return typeof status === "string" ? status : null;
  }
  return null;
}

/** Whether the Creator/renderer still owns an in-flight turn. */
export function creationThreadInProgress(thread: CreationThread): boolean {
  if (creationJobFailed(thread)) return false;
  const jobActive = Boolean(thread.active_job_id && (!thread.job || !creationJobSettled(thread)));
  const variantActive = Boolean(thread.job?.variants.some((variant) =>
    ["queued", "rendering"].includes(String(variant.render_status)),
  ));
  return jobActive
    || variantActive
    || GENERATION_PROGRESS_STATES.has(generationStatus(thread) ?? "")
    || CREATOR_PROGRESS_STATES.has(creatorStatus(thread) ?? "");
}

/** Whether the current automatic speech preflight still needs detail polling. */
export function creationSpeechCleanupPending(thread: CreationThread): boolean {
  const status = thread.speech_cleanup?.analysis?.status;
  return status === "queued" || status === "running";
}

/** Polling transport state, intentionally separate from render/Creator UI state. */
export function creationThreadNeedsPolling(thread: CreationThread): boolean {
  return creationThreadInProgress(thread) || creationSpeechCleanupPending(thread);
}

/** A Creator execution has committed, but its Job may not exist yet. */
export function creationThreadPreparing(thread: CreationThread): boolean {
  return !thread.job && (
    CREATOR_PROGRESS_STATES.has(creatorStatus(thread) ?? "")
    || GENERATION_PROGRESS_STATES.has(generationStatus(thread) ?? "")
  );
}

/** Stable polling dependency; avoids restarting the effect for fresh JSON objects. */
export function creationThreadProgressKey(thread: CreationThread): string {
  const variants = (thread.job?.variants ?? [])
    .map((variant) => [
      variant.variant_id ?? "",
      variant.render_generation_id ?? "",
      variant.render_status ?? "",
      variant.render_finished_at ?? "",
    ].join("/"))
    .sort()
    .join(",");
  const renderKey = [
    thread.active_job_id ?? "",
    thread.job?.status ?? "",
    creatorStatus(thread) ?? "",
    generationStatus(thread) ?? "",
    variants,
  ].join(":");
  const cleanup = thread.speech_cleanup;
  if (!cleanup) return renderKey;
  const analysis = cleanup.analysis;
  const outcome = cleanup.outcome;
  return [
    renderKey,
    analysis?.id ?? "",
    analysis?.status ?? "",
    outcome?.job_id ?? "",
    outcome?.render_generation_id ?? "",
    outcome?.status ?? "",
  ].join(":");
}

/** Only the API's exact optimistic-concurrency conflict gets stale-window copy. */
export function isCreationThreadRevisionConflict(cause: unknown): boolean {
  return cause instanceof CreationThreadError
    && cause.status === 409
    && cause.message === "Creation thread changed";
}

/** Only a generation-pinned cleanup analysis mismatch gets stale-check copy. */
export function isCreationSpeechCleanupStaleConflict(cause: unknown): boolean {
  return cause instanceof CreationThreadError
    && cause.status === 409
    && cause.message === "speech_cleanup_analysis_changed";
}

export async function listCreationThreads(): Promise<CreationThread[]> {
  const result = await request<CreationThread[] | { threads: CreationThread[] }>("");
  return Array.isArray(result) ? result : result.threads;
}

export async function getCreationCapabilities(): Promise<CreationCapabilitiesResponse> {
  const result = await request<{
    formats: CreationCapability[];
    media?: CreationMediaCapabilities;
  }>("/capabilities");
  const serverClipLimit = result.media?.clips?.max;
  return {
    media: result.media,
    formats: result.formats
      .filter((item) => creationFormat(item.edit_format))
      .map((item) => ({
        ...item,
        // Current API exposes the shared PlanItem contract under media.clips;
        // preserve per-format fields too for future capability responses.
        max_clips: item.max_clips ?? item.clip_limit ?? serverClipLimit,
      })),
  };
}

export const kriaRuntimeV2Enabled = process.env.NEXT_PUBLIC_KRIA_RUNTIME_V2_ENABLED === "true";

export function createCreationThread(message?: string): Promise<CreationThread> {
  return request<CreationThread>("", {
    method: "POST",
    body: JSON.stringify({
      client_event_id: id("thread"),
      ...(kriaRuntimeV2Enabled ? { runtime_version: 2 } : {}),
      ...(!kriaRuntimeV2Enabled && message ? { message } : {}),
    }),
  });
}

export function refreshCreationThread(threadId: string, signal?: AbortSignal): Promise<CreationThread> {
  return request<CreationThread>(`/${threadId}?projection=full`, { cache: "no-store", signal });
}

export function creationSpeechCleanupActionId(
  analysisId: string,
  action: CreationSpeechCleanupChoice | "no_findings" | "retry_analysis" | "retry_render" | "bypass",
  revision: number,
): string {
  // A lost response must replay the same mutation, while a later failure of a
  // newly-created Job must remain retryable even when it reuses the same
  // immutable analysis. The thread revision distinguishes those two cases.
  return `speech-cleanup:${analysisId}:r${revision}:${action}`;
}

export function getKriaApproval(
  threadId: string,
  approvalId: string,
): Promise<KriaApprovalSnapshot> {
  return request<KriaApprovalSnapshot>(`/${threadId}/approvals/${approvalId}`, {
    cache: "no-store",
  });
}

export function decideKriaApproval(
  threadId: string,
  approval: KriaApprovalSnapshot,
  decision: "approve" | "deny",
  expectedThreadRevision: number,
): Promise<KriaApprovalDecision> {
  if (approval.draft_revision === null) {
    throw new Error("Kria approval is missing its pinned draft revision");
  }
  return request<KriaApprovalDecision>(
    `/${threadId}/approvals/${approval.approval_id}/${decision}`,
    {
      method: "POST",
      body: JSON.stringify({
        expected_thread_revision: expectedThreadRevision,
        expected_draft_revision: approval.draft_revision,
        expected_approval_fingerprint: approval.approval_fingerprint,
      }),
    },
  );
}

export function sendKriaTurn(
  thread: CreationThread,
  message: string,
  clientEventId = id("turn"),
): Promise<KriaTurnAccepted> {
  return request<KriaTurnAccepted>(`/${thread.id}/turns`, {
    method: "POST",
    body: JSON.stringify({
      message,
      client_event_id: clientEventId,
      expected_thread_revision: thread.revision,
    }),
  });
}

export function cancelKriaTurn(
  threadId: string,
  turnId: string,
  expectedThreadRevision: number,
): Promise<TurnCancelled> {
  return request<TurnCancelled>(`/${threadId}/turns/${turnId}/cancel`, {
    method: "POST",
    body: JSON.stringify({ expected_thread_revision: expectedThreadRevision }),
  });
}

export function getKriaDelta(
  threadId: string,
  afterSequence = -1,
  limit = 100,
): Promise<KriaDelta> {
  const query = new URLSearchParams({
    after_sequence: String(afterSequence),
    limit: String(limit),
  });
  return request<KriaDelta>(`/${threadId}?${query.toString()}`, { cache: "no-store" });
}

export function getKriaDraft(threadId: string): Promise<KriaDraftSnapshot> {
  return request<KriaDraftSnapshot>(`/${threadId}/draft`, { cache: "no-store" });
}

export function undoKriaDraft(
  threadId: string,
  expectedDraftRevision: number,
): Promise<KriaDraftSnapshot> {
  return request<KriaDraftSnapshot>(`/${threadId}/draft/undo`, {
    method: "POST",
    body: JSON.stringify({ expected_draft_revision: expectedDraftRevision }),
  });
}

export interface EditorConversationMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  applied: string[];
  rejected: string[];
  clarification_context?: Record<string, unknown> | null;
  pending_actions?: Array<Record<string, unknown>>;
}

export interface EditorConversationBatch {
  item_id: string;
  variant_id: string;
  generation_id: string | null;
  messages: EditorConversationMessage[];
}

export function openEditorCreationThread(itemId: string, variantId?: string | null): Promise<CreationThread> {
  return request<CreationThread>("/for-editor", {
    method: "POST", body: JSON.stringify({ item_id: itemId, variant_id: variantId ?? null }),
  });
}

export function recordEditorConversation(threadId: string, batch: EditorConversationBatch): Promise<CreationThread> {
  return request<CreationThread>(`/${threadId}/editor-events`, {
    method: "POST", body: JSON.stringify(batch),
  });
}

export function sendCreationMessage(thread: CreationThread, message: string): Promise<CreationThread> {
  return request<CreationThread>(`/${thread.id}/messages`, {
    method: "POST",
    body: JSON.stringify({
      message,
      client_event_id: id("message"),
      expected_revision: thread.revision,
    }),
  });
}

export function applyCreationAction(
  thread: CreationThread,
  action: CreationAction,
  payload: Record<string, unknown> = {},
  clientActionId?: string,
): Promise<CreationThread> {
  return request<CreationThread>(`/${thread.id}/actions`, {
    method: "POST",
    body: JSON.stringify({
      action,
      payload,
      client_action_id: clientActionId ?? id("action"),
      expected_revision: thread.revision,
    }),
  });
}

export async function uploadCreationMedia(
  thread: CreationThread,
  files: File[],
  onAttached?: (thread: CreationThread, file: File) => void,
): Promise<CreationThread> {
  let current = thread;
  for (const file of files) {
    let reservation = uploadReservations.get(file);
    if (!reservation) {
      const reserved = await request<CreationUploadTarget[] | { targets: CreationUploadTarget[] }>(
        `/${thread.id}/upload-urls`,
        {
          method: "POST",
          body: JSON.stringify({
            files: [{
              filename: file.name,
              content_type: uploadContentTypeForFile(file),
              file_size_bytes: file.size,
              client_upload_id: id("upload"),
            }],
          }),
        },
      );
      const targets = Array.isArray(reserved) ? reserved : reserved.targets;
      const target = targets[0];
      if (!target) throw new Error("Upload reservation was not returned.");
      reservation = { target, uploaded: false, clientEventId: id("media") };
      uploadReservations.set(file, reservation);
    }
    if (!reservation.uploaded) {
      try {
        await uploadToGcs(
          reservation.target.upload_url,
          file,
          reservation.target.upload_headers ?? {},
        );
        reservation.uploaded = true;
      } catch (cause) {
        // A failed PUT wrote no authoritative media. Mint a fresh signed target
        // on retry in case this one expired.
        uploadReservations.delete(file);
        throw cause;
      }
    }
    current = await request<CreationThread>(`/${thread.id}/media`, {
      method: "POST",
      body: JSON.stringify({
        media: [{
          media_id: reservation.target.media_id,
          ...(reservation.target.gcs_path ? { gcs_path: reservation.target.gcs_path } : {}),
          filename: file.name,
          content_type: reservation.target.content_type,
          kind: file.type.startsWith("image/")
            ? "image"
            : file.type.startsWith("audio/") ? "audio" : "video",
        }],
        client_event_id: reservation.clientEventId,
        expected_revision: current.revision,
      }),
    });
    uploadReservations.delete(file);
    onAttached?.(current, file);
  }
  return current;
}

export async function archiveCreationThread(thread: CreationThread): Promise<CreationThread> {
  return request<CreationThread>(`/${thread.id}/archive`, {
    method: "POST",
    body: JSON.stringify({
      expected_revision: thread.revision,
      client_event_id: id("archive"),
    }),
  });
}

/** Persist the short label shown in the project rail and workspace header. */
export function renameCreationThread(
  thread: CreationThread,
  name: string,
): Promise<CreationThread> {
  return request<CreationThread>(`/${thread.id}`, {
    method: "PATCH",
    body: JSON.stringify({
      title: name.trim(),
      expected_revision: thread.revision,
      client_event_id: id("rename"),
    }),
  });
}

/** Remove a project from the creator's project list. */
export function deleteCreationThread(thread: CreationThread): Promise<void> {
  const query = new URLSearchParams({ expected_revision: String(thread.revision) });
  return request<void>(`/${thread.id}?${query.toString()}`, {
    method: "DELETE",
  });
}

export type CreationThreadArtifact =
  | "format"
  | "upload"
  | "voiceover"
  | "confirmation"
  | "revision"
  | "draft"
  | "approval"
  | "progress"
  | "result"
  | "failure";

export interface CreationThreadMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  eventType: string;
  artifact?: CreationThreadArtifact;
  /** Stable identity for an append-only event group rendered as one card. */
  artifactKey?: string;
  payload?: Record<string, unknown>;
}

const ASSISTANT_CONVERSATION_EVENTS = new Set([
  "editor_assistant_message",
  "format_prompt",
  "media_prompt",
  "upload_prompt",
  "voiceover_prompt",
  "confirm_generation",
  "confirmation",
  "revision_queued",
  "status_update",
  "assistant_question",
  "assistant_response",
  "draft_applied",
  "assistant_strategy",
  "assistant_review",
  "assistant_error",
  "assistant_render_failed",
  "memory_updated",
  "creator_memory_receipt",
  "agent_assistant_question",
  "agent_assistant_strategy",
  "agent_assistant_review",
  "agent_assistant_error",
  "agent_assistant_render_failed",
]);

const LIFECYCLE_ARTIFACTS = new Set<CreationThreadArtifact>([
  "progress",
  "result",
  "failure",
]);

function normalizedMessage(value: unknown): string {
  return typeof value === "string"
    ? value.trim().replace(/\s+/g, " ").toLowerCase()
    : "";
}

/** Identity for the active Job generation. Status/output fields are excluded so
 * polling updates the existing card instead of remounting a new one. A variant
 * rerender changes the key once the API exposes its render_generation_id. */
export function creationGenerationArtifactKey(thread: CreationThread): string {
  const jobId = thread.job?.id ?? thread.active_job_id ?? "pending";
  const variantGenerations = [...new Set((thread.job?.variants ?? []).flatMap((variant) =>
    typeof variant.render_generation_id === "string" && variant.render_generation_id
      ? [`${variant.variant_id ?? "variant"}:${variant.render_generation_id}`]
      : [],
  ))].sort();
  return variantGenerations.length > 0
    ? `job:${jobId}:generations:${variantGenerations.join(",")}`
    : `job:${jobId}`;
}

function eventGenerationArtifactKey(
  thread: CreationThread,
  event: CreationThreadEvent,
): string {
  const payload = event.payload ?? {};
  const payloadJobId = typeof payload.job_id === "string" ? payload.job_id : null;
  const currentJobId = thread.job?.id ?? thread.active_job_id;
  const jobId = payloadJobId ?? currentJobId;
  const variantId = typeof payload.variant_id === "string" ? payload.variant_id : null;
  const generationId = typeof payload.render_generation_id === "string"
    ? payload.render_generation_id
    : typeof payload.generation_id === "string" ? payload.generation_id : null;
  if (jobId && generationId) {
    return `job:${jobId}:${variantId ? `variant:${variantId}:` : ""}generation:${generationId}`;
  }
  if (jobId && variantId) return `job:${jobId}:variant:${variantId}`;
  if (jobId && jobId === currentJobId) return creationGenerationArtifactKey(thread);
  if (jobId) return `job:${jobId}`;
  return creationGenerationArtifactKey(thread);
}

/** Render append-only server events as conversation rows. */
export function threadMessages(thread: CreationThread): CreationThreadMessage[] {
  // The first strategy is the initial creation direction. A strategy becomes
  // a revision proposal only after durable evidence that the Creator Agent
  // confirmed/started a render; otherwise a ready first cut would replay that
  // initial direction as a bogus "Revision ready" card after refresh. The
  // action events cover newer projections, while the agent events preserve
  // recovered threads whose action projection was not appended.
  const events = [...thread.events].sort((left, right) => left.sequence - right.sequence);
  const latestGenerationSequence = events.reduce(
    (latest, event) => [
      "action_generate",
      "action_confirm_generation",
      "agent_user_confirmation",
      "agent_assistant_execution",
    ].includes(event.event_type)
      ? Math.max(latest, event.sequence)
      : latest,
    -1,
  );
  const latestStrategySequence = events.reduce(
    (latest, event) => event.event_type === "agent_assistant_strategy"
      ? Math.max(latest, event.sequence)
      : latest,
    -1,
  );
  const normalizedIntent = normalizedMessage(thread.state.intent);
  let precedingUserMessage = "";
  const projected = events.flatMap((event): CreationThreadMessage[] => {
    const payload = event.payload ?? {};
    const kind = String(payload.kind ?? event.event_type);
    let content = (event.content
      ?? (typeof payload.message === "string" ? payload.message : undefined))?.trim() ?? "";
    if (!content && event.role === "user") return [];
    let artifact: CreationThreadArtifact | undefined;
    if (["select_format", "select_edit_format", "format_options"].includes(kind)) artifact = "format";
    else if (["collect_media", "upload_prompt"].includes(kind)) artifact = "upload";
    else if (["collect_voiceover", "voiceover_prompt"].includes(kind)) artifact = "voiceover";
    else if (["confirm_generation", "confirmation"].includes(kind)) artifact = "confirmation";
    else if (["confirm_revision", "revision"].includes(kind)) artifact = "revision";
    else if (event.event_type === "draft_applied") artifact = "draft";
    else if (event.event_type === "approval_requested") artifact = "approval";
    else if (event.event_type === "agent_assistant_strategy") {
      // Strategies are durable agent history, not a queue of confirmation
      // buttons. Keep only the newest strategy actionable; older attempts
      // remain transcript content after a failed/retried render.
      if (event.sequence === latestStrategySequence) {
        artifact = latestGenerationSequence >= 0 && event.sequence > latestGenerationSequence
          ? "revision"
          : "confirmation";
      }
    }
    else if (["generation_started", "render_queued", "render_started", "rendering"].includes(event.event_type)
      || (event.event_type === "agent_assistant_execution" && ["started", "rendering", "queued"].includes(String(payload.status)))) artifact = "progress";
    else if (["generation_failed", "render_failed", "assistant_error", "agent_assistant_error"].includes(event.event_type)
      || (event.event_type === "agent_assistant_execution" && ["failed", "error"].includes(String(payload.status)))) artifact = "failure";
    else if (event.event_type === "generation_ready"
      || (event.event_type === "agent_assistant_execution" && ["ready", "completed"].includes(String(payload.status)))) artifact = "result";

    // The transcript is an outcome ledger, not a dump of append-only audit
    // rows. In particular, thread_created contains the initial prompt and
    // agent_user_message mirrors it; rendering callbacks are status-card data.
    const isUserMessage = event.role === "user" && ["user_message", "editor_user_message"].includes(event.event_type);
    const isAssistantMessage = ASSISTANT_CONVERSATION_EVENTS.has(event.event_type);
    if (!isUserMessage && !isAssistantMessage && !artifact) return [];
    if (isUserMessage && content) precedingUserMessage = content;
    const isStateOnlyLifecycle = artifact
      && LIFECYCLE_ARTIFACTS.has(artifact)
      && !["assistant_error", "agent_assistant_error"].includes(event.event_type);
    if (isStateOnlyLifecycle) content = "";
    const echoCandidates = new Set([
      normalizedIntent,
      normalizedMessage(precedingUserMessage),
    ].filter(Boolean));
    if (!isUserMessage && content && echoCandidates.has(normalizedMessage(content))) content = "";
    if (!content && !artifact) return [];

    const artifactKey = artifact && LIFECYCLE_ARTIFACTS.has(artifact)
      ? eventGenerationArtifactKey(thread, event)
      : undefined;
    return [{
      id: artifactKey ? `${thread.id}:generation:${artifactKey}` : event.id,
      role: isUserMessage ? "user" : "assistant",
      content,
      eventType: event.event_type,
      payload,
      ...(artifact ? { artifact } : {}),
      ...(artifactKey ? { artifactKey } : {}),
    }];
  });

  // Append-only worker callbacks for the same exact generation project to one
  // mutable-looking card. Keep its first transcript position so a late callback
  // cannot jump above a newer user message after refresh.
  const lifecycleIndexByKey = new Map<string, number>();
  return projected.reduce<CreationThreadMessage[]>((rows, message) => {
    if (!message.artifactKey) {
      rows.push(message);
      return rows;
    }
    const existingIndex = lifecycleIndexByKey.get(message.artifactKey);
    if (existingIndex === undefined) {
      lifecycleIndexByKey.set(message.artifactKey, rows.length);
      rows.push(message);
    } else {
      rows[existingIndex] = message;
    }
    return rows;
  }, []);
}
