import type { MotionPresetInstance } from "@nova/motion-runtime";

/**
 * API client for content-plan endpoints (Phase 3+).
 *
 * Calls go through the same-origin Next.js proxy at /api/plan/<path>, which
 * injects the NextAuth session's X-User-Id + the server-only INTERNAL_API_KEY
 * before forwarding to FastAPI. The browser never sees the internal key, and
 * these helpers use RELATIVE URLs (no NEXT_PUBLIC_API_URL) so the request stays
 * same-origin and carries the session cookie.
 *
 * A 401 from the proxy means "not signed in" — callers should send the user to
 * /api/auth/signin (NextAuth's default Google sign-in page).
 */

import type { CarouselMoment, EditVariantPayload } from "@/lib/generative-api";
import type { NovaStep } from "@/lib/job-phases";
// Re-exported so editor components can import the carousel-moment shape
// alongside PlanItemVariant/editPlanItemVariant without a second import line.
export type { CarouselMoment } from "@/lib/generative-api";
import type { ArchetypeFallback } from "@/lib/plan-generate-gate";
import type { CopilotOp, CopilotOutcome } from "@/lib/edit-copilot/ops";
import type { CopilotSnapshot } from "@/lib/edit-copilot/snapshot";
import type { TextMotionConfigV2 } from "@/lib/text-motion-v2";

const PLAN_BASE = "/api/plan";

export class NotAuthenticatedError extends Error {
  constructor() {
    super("Not authenticated");
    this.name = "NotAuthenticatedError";
  }
}

/**
 * The route exists but its server-side feature flag is off.
 *
 * Distinct from a plain failure because retrying never clears it: the fix is a
 * deploy-config change. Callers disable the affordance instead of offering the
 * user a retry they cannot win.
 */
export class FeatureDisabledError extends Error {
  constructor(detail: string) {
    super(detail);
    this.name = "FeatureDisabledError";
  }
}

export class PlanApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly requestId: string | null;
  readonly stage: string | null;
  readonly limit: number | null;
  readonly current: number | null;
  readonly requested: number | null;
  readonly remaining: number | null;

  constructor({
    message,
    status,
    code = "request_failed",
    retryable = false,
    requestId = null,
    stage = null,
    limit = null,
    current = null,
    requested = null,
    remaining = null,
  }: {
    message: string;
    status: number;
    code?: string;
    retryable?: boolean;
    requestId?: string | null;
    stage?: string | null;
    limit?: number | null;
    current?: number | null;
    requested?: number | null;
    remaining?: number | null;
  }) {
    super(message);
    this.name = "PlanApiError";
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.requestId = requestId;
    this.stage = stage;
    this.limit = limit;
    this.current = current;
    this.requested = requested;
    this.remaining = remaining;
  }
}

export interface PersonaQuestionnaire {
  work: string;
  school: string;
  social: string;
  location: string;
  hobbies: string;
  travels: string;
  passions: string;
  tiktok_handle: string;
}

export interface PersonaContent {
  summary: string;
  content_pillars: string[];
  tone: string;
  audience: string;
  posting_cadence: string;
  // Structured post frequency (1-7). Drives how many plan ideas appear per week.
  // Optional: personas generated before this field shipped won't have it; the
  // backend resolve_posts_per_week() falls back to the cadence prose or 7.
  posts_per_week?: number | null;
  sample_topics: string[];
  // The AI's "why this lane" — shown read-only in the dashboard. Optional:
  // personas generated before this field shipped won't have it.
  rationale?: string;
  // "What kind of videos do you make?" onboarding signal.
  // talking_head | montage | day_vlog | mixed
  footage_type_bias?: string[];
}

export type PersonaStatus = "generating" | "ready" | "failed" | "edited" | "chat_pending";

export interface TikTokProfile {
  handle: string;
  follower_count?: number | null;
  video_count?: number | null;
  top_captions?: string[];
  top_hashtags?: string[];
  analyzed_at?: string;
}

// ── Idea seeds (M1 Bring-Your-Own-Ideas) ────────────────────────────────────

export type IdeaSeedStatus = "pending" | "in_plan";

export interface IdeaSeed {
  id: string;
  text: string;
  pillar?: string | null;
  status: IdeaSeedStatus;
}

export interface PersonaResponse {
  id: string;
  persona_status: PersonaStatus;
  questionnaire: PersonaQuestionnaire | null;
  persona: PersonaContent | null;
  error_detail: string | null;
  tiktok_profile?: TikTokProfile | null;
  generation_started_at?: string | null;
  /** M1: user-owned idea seeds, persisted at persona scope. */
  idea_seeds?: IdeaSeed[];
}

// ── Chat interview ────────────────────────────────────────────────────────────

export interface ChatStartResponse {
  persona_id: string;
  question: string;
  suggestions: string[];
  turn_number: number;
  turn_label: string;
  tiktok_context?: TikTokProfile | null;
  persona_status: string;
}

export interface ChatTurnResponse {
  question?: string | null;
  suggestions: string[];
  is_final: boolean;
  turn_number: number;
  turn_label: string;
  persona_status: string;
}

export interface EditCopilotTurn {
  role: "user" | "assistant";
  content: string;
  applied?: string[];
  rejected?: string[];
}

export interface EditCopilotTurnResponse {
  /** Durable identity for the server-proposed operation bundle. Absent only
   * when talking to a pre-receipts backend during a split deployment. */
  receipt_id?: string | null;
  intent: string;
  ops: CopilotOp[];
  confidence: number;
  reply: string;
  suggestions: string[];
  needs_clarification: boolean;
  outcome?: CopilotOutcome;
  rejection_reasons?: Array<{
    op: string;
    reason: "unknown_operation" | "capability_unavailable" | "missing_required" | "invalid_value" | "stale_target";
    detail: string;
  }>;
}

export type EditCopilotExecutionOutcome =
  | "applied"
  | "no_effect"
  | "rejected"
  | "stale"
  | "failed";

export interface EditCopilotExecutionReceiptBody {
  client_event_id: string;
  outcome: EditCopilotExecutionOutcome;
  rejection_reasons: Array<{
    op: string;
    reason: string;
    detail: string;
  }>;
  before_revision_hash: string | null;
  after_revision_hash: string | null;
}

export interface EditCopilotExecutionReceiptResponse {
  receipt_id: string;
  execution_receipt_id: string;
  client_event_id: string;
  recorded: boolean;
}

export function editCopilotTurn(
  itemId: string,
  variantId: string,
  body: {
    message: string;
    turns: EditCopilotTurn[];
    snapshot: CopilotSnapshot;
  },
): Promise<EditCopilotTurnResponse> {
  return request<EditCopilotTurnResponse>(
    `/plan-items/${itemId}/variants/${variantId}/copilot/turn`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

export function executeEditCopilotReceipt(
  itemId: string,
  variantId: string,
  receiptId: string,
  body: EditCopilotExecutionReceiptBody,
): Promise<EditCopilotExecutionReceiptResponse> {
  return request<EditCopilotExecutionReceiptResponse>(
    `/plan-items/${itemId}/variants/${variantId}/copilot/receipts/${receiptId}/execute`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

export type SuggestionCategory =
  | "hook_pacing"
  | "text"
  | "audio"
  | "effect"
  | "transition";

export type SuggestionApplyMode = "instant" | "omni_async" | "server_async";

export interface SpeechCutDirectorOp {
  op: "apply_speech_cut_candidate";
  candidate_id: string;
}

export interface OmniEditorSuggestion {
  action: "generate_insert" | "restyle_segment";
  prompt: string;
  insert_at_s: number;
  duration_s: number;
  source_clip_index?: number | null;
  source_start_s?: number | null;
  source_end_s?: number | null;
  reference_clip_index?: number | null;
  reference_frame_s?: number | null;
}

export interface EditorSuggestion {
  id: string;
  category: SuggestionCategory;
  title: string;
  rationale: string;
  expected_benefit: string;
  confidence: number;
  start_s: number;
  end_s: number;
  apply_mode: SuggestionApplyMode;
  ops: Array<CopilotOp | SpeechCutDirectorOp>;
  omni?: OmniEditorSuggestion | null;
}

export interface EditDirectorSuggestionsResponse {
  suggestions: EditorSuggestion[];
  snapshot_revision: string;
  requested_model: string;
  model_used: string;
  fallback_reason?: string | null;
}

export function editDirectorSuggestions(
  itemId: string,
  variantId: string,
  body: {
    snapshot: CopilotSnapshot;
    snapshot_revision: string;
    dismissed_suggestion_ids: string[];
    omni_enabled: boolean;
  },
  signal?: AbortSignal,
): Promise<EditDirectorSuggestionsResponse> {
  return request<EditDirectorSuggestionsResponse>(
    `/plan-items/${itemId}/variants/${variantId}/director/suggestions`,
    { method: "POST", body: JSON.stringify(body), signal },
  );
}

export async function editDirectorFeedback(
  itemId: string,
  variantId: string,
  body: {
    suggestion_id: string;
    action: "accepted" | "dismissed";
    category: SuggestionCategory;
    model_used: string;
  },
): Promise<void> {
  await request<unknown>(
    `/plan-items/${itemId}/variants/${variantId}/director/feedback`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export interface SpeechCutDispatchResponse {
  status: "rendering";
  request: SpeechCutOperation;
}

export interface SpeechCutOperation {
  operation: "apply_speech_cut_candidate" | "restore_original_timing";
  operation_id?: string;
  revision: string;
  candidate_id?: string;
  removed?: { start_s: number; end_s: number; reason: string };
  time_saved_s?: number;
  restored_s?: number;
  render_generation_id?: string;
  status?: "applied";
}

export function applySpeechCutCandidate(
  itemId: string,
  variantId: string,
  candidateId: string,
  expectedRevision: string,
): Promise<SpeechCutDispatchResponse> {
  return request<SpeechCutDispatchResponse>(
    `/plan-items/${itemId}/variants/${variantId}/speech-cuts/${candidateId}/apply`,
    { method: "POST", body: JSON.stringify({ expected_revision: expectedRevision }) },
  );
}

export function restoreOriginalSpeechTiming(
  itemId: string,
  variantId: string,
  expectedRevision: string,
): Promise<SpeechCutDispatchResponse> {
  return request<SpeechCutDispatchResponse>(
    `/plan-items/${itemId}/variants/${variantId}/speech-cuts/restore`,
    { method: "POST", body: JSON.stringify({ expected_revision: expectedRevision }) },
  );
}

export interface OmniAssetResponse {
  asset_id: string;
  status:
    | "queued"
    | "generating"
    | "normalizing"
    | "ready"
    | "failed"
    | "cancellation_requested"
    | "cancelled";
  progress: number;
  model: string;
  error?: string | null;
  operation?:
    | Extract<CopilotOp, { op: "insert_generated_asset" }>
    | Extract<CopilotOp, { op: "replace_generated_segment" }>
    | null;
}

export function startOmniAsset(
  itemId: string,
  variantId: string,
  body: {
    suggestion_id: string;
    draft_revision: string;
    action: OmniEditorSuggestion["action"];
    prompt: string;
    insert_at_s: number;
    duration_s: number;
    source_clip_index?: number | null;
    source_start_s?: number | null;
    source_end_s?: number | null;
    reference_clip_index?: number | null;
    reference_frame_s?: number | null;
  },
): Promise<OmniAssetResponse> {
  return request<OmniAssetResponse>(
    `/plan-items/${itemId}/variants/${variantId}/omni-assets`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function getOmniAsset(
  itemId: string,
  variantId: string,
  assetId: string,
): Promise<OmniAssetResponse> {
  return request<OmniAssetResponse>(
    `/plan-items/${itemId}/variants/${variantId}/omni-assets/${assetId}`,
  );
}

export function cancelOmniAsset(
  itemId: string,
  variantId: string,
  assetId: string,
): Promise<OmniAssetResponse> {
  return request<OmniAssetResponse>(
    `/plan-items/${itemId}/variants/${variantId}/omni-assets/${assetId}/cancel`,
    { method: "POST" },
  );
}

export function claimOmniAsset(
  itemId: string,
  variantId: string,
  assetId: string,
  draftRevision: string,
): Promise<OmniAssetResponse> {
  return request<OmniAssetResponse>(
    `/plan-items/${itemId}/variants/${variantId}/omni-assets/${assetId}/claim`,
    {
      method: "POST",
      body: JSON.stringify({ draft_revision: draftRevision }),
    },
  );
}

/** Accept a TikTok handle; fires async scrape and returns the persona row. */
export function tiktokScrape(handle: string): Promise<PersonaResponse> {
  return request<PersonaResponse>("/personas/tiktok-scrape", {
    method: "POST",
    body: JSON.stringify({ handle }),
  });
}

/** Start (or resume) the onboarding chat interview; returns the first unanswered Q. */
export function chatStart(): Promise<ChatStartResponse> {
  return request<ChatStartResponse>("/personas/chat/start", { method: "POST" });
}

/** Submit a chat answer; returns the next Q or is_final=true when done. */
export function chatTurn(personaId: string, answer: string): Promise<ChatTurnResponse> {
  return request<ChatTurnResponse>("/personas/chat/turn", {
    method: "POST",
    body: JSON.stringify({ persona_id: personaId, answer }),
  });
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${PLAN_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (res.status === 401) throw new NotAuthenticatedError();
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    let code = "request_failed";
    let retryable = res.status >= 500;
    let requestId: string | null = null;
    let stage: string | null = null;
    let limit: number | null = null;
    let current: number | null = null;
    let requested: number | null = null;
    let remaining: number | null = null;
    try {
      requestId = res.headers.get("x-request-id");
    } catch {
      // Minimal fetch shims in tests/embedded clients may omit Headers.
    }
    try {
      const body = (await res.json()) as {
        detail?:
          | string
          | {
              detail?: string;
              message?: string;
              code?: string;
              retryable?: boolean;
              stage?: string;
              limit?: number;
              current?: number;
              requested?: number;
              remaining?: number;
            };
        code?: string;
        retryable?: boolean;
        request_id?: string;
        stage?: string;
        limit?: number;
        current?: number;
        requested?: number;
        remaining?: number;
      };
      const nested = typeof body?.detail === "object" ? body.detail : null;
      if (typeof body?.detail === "string") detail = body.detail;
      else if (nested?.detail || nested?.message) detail = nested.detail ?? nested.message ?? detail;
      if (body?.code || nested?.code) code = body.code ?? nested?.code ?? code;
      if (typeof (body?.retryable ?? nested?.retryable) === "boolean") {
        retryable = body?.retryable ?? nested?.retryable ?? retryable;
      }
      if (body?.request_id) requestId = body.request_id;
      if (body?.stage || nested?.stage) stage = body.stage ?? nested?.stage ?? null;
      if (typeof (body?.limit ?? nested?.limit) === "number") limit = body?.limit ?? nested?.limit ?? null;
      if (typeof (body?.current ?? nested?.current) === "number") current = body?.current ?? nested?.current ?? null;
      if (typeof (body?.requested ?? nested?.requested) === "number") requested = body?.requested ?? nested?.requested ?? null;
      if (typeof (body?.remaining ?? nested?.remaining) === "number") remaining = body?.remaining ?? nested?.remaining ?? null;
    } catch {
      // non-JSON error body; keep the generic message
    }
    // A 404 whose detail ends in `_not_enabled` is the backend's feature-flag
    // gate, not a missing resource. Kept deliberately narrow: ordinary 404s on
    // the same routes carry real details ("Plan item not found", "No render to
    // edit yet", "Variant not found") and must stay retryable errors.
    if (res.status === 404 && detail.endsWith("_not_enabled")) {
      throw new FeatureDisabledError(detail);
    }
    const safeDetail =
      res.status >= 500 && code === "request_failed"
        ? "Kria couldn't complete that request. Retry in a moment."
        : detail;
    throw new PlanApiError({
      message: safeDetail,
      status: res.status,
      code,
      retryable,
      requestId,
      stage,
      limit,
      current,
      requested,
      remaining,
    });
  }
  // Successful DELETE endpoints return no JSON body.
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** Submit the onboarding questionnaire; creates/replaces the persona and enqueues generation. */
export function createPersona(
  questionnaire: Partial<PersonaQuestionnaire>,
): Promise<PersonaResponse> {
  return request<PersonaResponse>("/personas", {
    method: "POST",
    body: JSON.stringify(questionnaire),
  });
}

/** Soft-reset onboarding: deletes persona/plan/feedback, keeps rendered videos. */
export function resetPersona(): Promise<{ reset: boolean }> {
  return request<{ reset: boolean }>("/personas/reset", { method: "POST" });
}

/** Fetch the current user's persona, or null if they haven't started onboarding. */
export async function getPersona(): Promise<PersonaResponse | null> {
  try {
    return await request<PersonaResponse>("/personas");
  } catch (err) {
    if (err instanceof Error && err.message.includes("(404)")) return null;
    if (err instanceof Error && /No persona yet/i.test(err.message)) return null;
    throw err;
  }
}

/** Hand-edit persona fields (also unblocks onboarding if generation failed). */
export function updatePersona(
  id: string,
  edit: Partial<PersonaContent>,
): Promise<PersonaResponse> {
  return request<PersonaResponse>(`/personas/${id}`, {
    method: "PATCH",
    body: JSON.stringify(edit),
  });
}

/**
 * Persist the "what kind of videos do you make" onboarding answer.
 * Stored in persona.footage_type_bias — no USER_STYLE_ENABLED gate.
 * Values: ["talking_head"] | ["montage"] | ["day_vlog"] | ["mixed"]
 */
export function patchPersonaFootageType(
  personaId: string,
  footage_type_bias: string[],
): Promise<PersonaResponse> {
  return request<PersonaResponse>(`/personas/${personaId}`, {
    method: "PATCH",
    body: JSON.stringify({ footage_type_bias }),
  });
}

/**
 * Replace the user's idea seeds list (M1 Bring-Your-Own-Ideas).
 * The server stamps missing ids and sanitizes text/pillar. Returns the updated
 * PersonaResponse with the server-stamped seeds (idempotent: call on every edit).
 */
export function patchPersonaIdeas(
  personaId: string,
  seeds: IdeaSeed[],
): Promise<PersonaResponse> {
  // Existing seeds carry their server-stamped ids so they are stable across saves
  // (add/remove, no id churn). New seeds sent with id:"" get a fresh uuid from the
  // server. The server uses s.get("id") or "" → new uuid if empty/absent.
  return request<PersonaResponse>(`/personas/${personaId}`, {
    method: "PATCH",
    body: JSON.stringify({ idea_seeds: seeds }),
  });
}

/**
 * Re-tune the persona from the user's feedback (feedback loop, Phase 2). Returns
 * the persona in `generating` status; callers re-poll getPersona. A hand-edited
 * persona is authoritative and rejected with 409 (the caller should disable the
 * action and explain rather than call this).
 */
export function retunePersonaFromFeedback(id: string): Promise<PersonaResponse> {
  return request<PersonaResponse>(`/personas/${id}/retune-from-feedback`, { method: "POST" });
}

// ── Content plan ─────────────────────────────────────────────────────────────

export type PlanStatus = "generating" | "ready" | "failed" | "edited";
export type MontagePreset = "classic" | "masonry" | "polaroid_wall";
export type RenderedMontagePreset = Exclude<MontagePreset, "classic">;

/** Derived server-side from the linked Job.status — never a stored column. */
export type PlanItemStatus =
  | "idea"
  | "draft"
  | "awaiting_clips"
  | "generating"
  | "ready"
  | "failed"
  | "rerolling";

/** One concrete shot in a filming guide. */
export interface FilmingShot {
  /** Stable server-assigned uuid; null for pre-0052 rows (backfilled by migration). */
  shot_id: string | null;
  what: string;
  how: string;
  duration_s: number;
  /** How many clips the creator should film for this shot (default 1). */
  clip_count?: number;
}

/** One clip assignment — shot_id=null means extra-footage pool. */
export interface ClipAssignment {
  gcs_path: string;
  shot_id: string | null;
}

export interface PlanItem {
  id: string;
  /** Idea-centric (0055+): nullable. Use `position` for sort order. */
  day_index: number | null;
  /** Idea-centric (0055+): nullable until AI expands the item. */
  theme: string | null;
  idea: string;
  /** User-controlled sort order (0055+). Use this instead of day_index for ordering. */
  position: number;
  /** ISO date string (YYYY-MM-DD) or null. */
  scheduled_date?: string | null;
  notes?: string | null;
  scenes?: SceneBlock[];
  filming_suggestion: string | null;
  // The AI's "why this works" — shown read-only. null for items made before
  // this field shipped (the UI hides the line).
  rationale: string | null;
  // Structured shot list generated at plan time. Empty for items made before
  // this field shipped; the UI falls back to filming_suggestion in that case.
  filming_guide: FilmingShot[];
  clip_gcs_paths: string[];
  /** Per-shot clip assignments (since migration 0052). Empty for new items. */
  clip_assignments?: ClipAssignment[];
  status: PlanItemStatus;
  current_job_id: string | null;
  /** True pipeline completion time for the current render job. */
  finished_at?: string | null;
  user_edited: boolean;
  /** Render archetype assigned at plan-gen time (e.g. "montage", "talking_head"). Null for legacy items. */
  edit_format?: string | null;
  /** Per-video Smart Captions choice. A stored true never bypasses backend rollout gates. */
  smart_captions_enabled?: boolean;
  /** Keep Smart visual/caption intelligence but disable automatic SFX. */
  smart_sound_design_enabled?: boolean;
  /** Server-computed from format, feature gate, and creator-style assignment. */
  /** Null/absent on aggregate responses that do not enrich creator capability. */
  smart_captions_available?: boolean | null;
  smart_captions_unavailable_reason?: string | null;
  speech_cleanup_enabled?: boolean;
  speech_cleanup_available?: boolean;
  speech_cleanup_unavailable_reason?:
    | "no_committed_clip"
    | "unsupported_format"
    | "replacement_voiceover"
    | "renderer_disabled"
    | "engine_disabled"
    | "rollout_disabled"
    | string
    | null;
  speech_cleanup_notice?: { id: string; reason: string } | null;
  /** Montage visual preset. "classic" keeps the sequential montage; collage presets render a visual wall. */
  montage_preset?: MontagePreset;
  /** Per-item/persona content-mode resolved by the API for upload flow selection. */
  content_mode?: "existing_footage" | "create_new" | "mixed";
  /** Narrated-walkthrough voiceover GCS key (0056+). Null = no voiceover recorded yet. */
  voiceover_gcs_path?: string | null;
  /** Server-authoritative soundtrack selection for the next render. */
  audio_mode?: "kria" | "original" | "voiceover";
  /**
   * Landscape-clip fit preference.
   * "fit"  = letterbox (full-width, black bars top & bottom, never enlarged) — default.
   * "fill" = center-crop to fill the 9:16 frame (old behavior).
   * Only affects clips where width > height; portrait/square always crop.
   */
  landscape_fit: "fit" | "fill";
  /** Original-audio bed level for narrated. 0 = voice only, 1 = loudest. Null = Kria's default. */
  voiceover_bed_level?: number | null;
  /** Narrated caption style: "sentence" (sentence blocks) or "word" (one word at a time). Null = "sentence". */
  voiceover_caption_style?: string | null;
  /** BYO-Ideas provenance (M1 T5). Null = market-bank origin or pre-T5 item. */
  source_idea_seed_id?: string | null;
  source_idea_seed_text?: string | null;
}

export interface SceneBlock {
  id?: string;
  text: string;
  transition_after?: string | null;
}

export interface IdeaExpandProposal {
  theme: string;
  filming_suggestion: string;
  filming_guide: FilmingShot[];
  rationale: string;
}

/** Activation seed (T8) lifecycle: none→seeding→activating→activated|activated_empty|failed. */
export type ActivationStatus =
  | "none"
  | "seeding"
  | "activating"
  | "activated"
  | "activated_empty"
  | "failed";

export interface ContentPlan {
  id: string;
  plan_status: PlanStatus;
  horizon_days: number;
  events: { text?: string } | null;
  items: PlanItem[];
  activation_status: ActivationStatus;
  seed_clip_count: number;
  generation_started_at?: string | null;
  start_date?: string | null;
  /** BYO-Ideas (M1): idea seeds from the linked persona, included in the plan GET
   *  so the workspace sidebar can show them without a separate persona call. */
  idea_seeds?: IdeaSeed[];
}

/** Create a plan from the user's ready persona + optional events; generation runs async. */
export function createContentPlan(events: string, horizonDays = 30): Promise<ContentPlan> {
  return request<ContentPlan>("/content-plans", {
    method: "POST",
    body: JSON.stringify({ events, horizon_days: horizonDays }),
  });
}

/** The user's latest plan with items, or null if none exists yet. */
export async function getContentPlan(): Promise<ContentPlan | null> {
  try {
    return await request<ContentPlan>("/content-plans");
  } catch (err) {
    if (err instanceof Error && /\(404\)|No content plan yet/i.test(err.message)) return null;
    throw err;
  }
}

/**
 * Regenerate the plan with the user's feedback (feedback loop, Phase 2). Returns
 * the plan in `generating` status; callers re-poll getContentPlan. Days the user
 * hand-edited or already started rendering are preserved server-side.
 */
export function regenerateContentPlan(planId: string): Promise<ContentPlan> {
  return request<ContentPlan>(`/content-plans/${planId}/regenerate`, { method: "POST" });
}

/** Idea-centric: append AI-generated ideas to the plan (opt-in, never auto-runs). */
export function generateIdeasWithAI(planId: string): Promise<ContentPlan> {
  return request<ContentPlan>(`/content-plans/${planId}/generate-ideas`, { method: "POST" });
}

/** Add a bare idea to the plan immediately (no AI). Returns the new PlanItem. */
export function addIdea(planId: string, idea: string, sourceIdeaSeedId?: string): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items?plan_id=${encodeURIComponent(planId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ idea, source_idea_seed_id: sourceIdeaSeedId ?? null }),
  });
}

/** Delete a plan item (refuses if active job or clips attached). */
export function deleteIdea(itemId: string): Promise<void> {
  return request<void>(`/plan-items/${itemId}`, { method: "DELETE" });
}

/** Reorder all plan items atomically. itemIds = full ordered list of item IDs. */
export function reorderItems(planId: string, itemIds: string[]): Promise<ContentPlan> {
  return request<ContentPlan>(`/content-plans/${planId}/reorder`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ item_ids: itemIds }),
  });
}

/** Propose an AI expansion for a bare idea (propose-only, never writes DB). */
export function expandIdea(
  itemId: string,
  input: { creator_context?: string | null } = {},
): Promise<IdeaExpandProposal> {
  return request<IdeaExpandProposal>(`/plan-items/${itemId}/expand`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function updatePlanItem(
  id: string,
  edit: {
    theme?: string;
    idea?: string;
    filming_suggestion?: string;
    notes?: string;
    scenes?: SceneBlock[];
    scheduled_date?: string | null;
    edit_format?: string | null;
    smart_captions_enabled?: boolean;
    smart_sound_design_enabled?: boolean;
    montage_preset?: MontagePreset;
    filming_guide?: FilmingShot[];
    landscape_fit?: "fit" | "fill";
    speech_cleanup_enabled?: boolean;
    speech_cleanup_notice_ack_id?: string;
    /** Per-item content_mode override (montage plan-vs-have toggle, 0058+). */
    content_mode?: "existing_footage" | "create_new" | "mixed";
    audio_mode?: "kria" | "original" | "voiceover";
  },
  options?: { signal?: AbortSignal },
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${id}`, {
    method: "PATCH",
    body: JSON.stringify(edit),
    signal: options?.signal,
  });
}

export function rerollPlanItem(id: string): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${id}/reroll`, { method: "POST" });
}

// ── Themed uploads + per-item generation (Phase 5) ────────────────────────────

export function getPlanItem(id: string): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${id}`);
}

export function getPlanItemFresh(id: string): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${id}`, { cache: "no-store" });
}

interface UploadUrl {
  upload_url: string;
  gcs_path: string;
}

// Single source of truth for the declared upload content type. The signing
// request (requestUploadUrls) and the PUT header MUST both go through this
// function — a divergence becomes a GCS 403 SignatureDoesNotMatch that Safari
// surfaces as a bare fetch TypeError.
export function uploadContentTypeForFile(file: File): string {
  if (file.type) return file.type;
  const name = file.name.toLowerCase();
  if (name.endsWith(".jpg") || name.endsWith(".jpeg")) return "image/jpeg";
  if (name.endsWith(".png")) return "image/png";
  if (name.endsWith(".webp")) return "image/webp";
  if (name.endsWith(".heic")) return "image/heic";
  if (name.endsWith(".heif")) return "image/heif";
  if (name.endsWith(".mov")) return "video/quicktime";
  return "video/mp4";
}

/** Ask the API for signed PUT URLs (lands under users/{uid}/plan/{itemId}/). */
export async function requestUploadUrls(
  itemId: string,
  files: { filename: string; content_type: string; file_size_bytes: number }[],
): Promise<UploadUrl[]> {
  const res = await request<{ urls: UploadUrl[] }>(`/plan-items/${itemId}/upload-urls`, {
    method: "POST",
    body: JSON.stringify({ files }),
  });
  return res.urls;
}

/** Signed upload for a voice note addressed to Kria (never a final voiceover). */
export function requestDirectionAudioUploadUrl(
  itemId: string,
  file: { filename: string; content_type: string; file_size_bytes: number },
): Promise<UploadUrl> {
  return request<UploadUrl>(`/plan-items/${itemId}/direction-audio/upload-url`, {
    method: "POST",
    body: JSON.stringify(file),
  });
}

/** Transcribe an uploaded direction note into PlanItem.notes. */
export function transcribeDirectionAudio(
  itemId: string,
  gcsPath: string,
): Promise<{ notes: string }> {
  return request<{ notes: string }>(`/plan-items/${itemId}/direction-audio/transcribe`, {
    method: "POST",
    body: JSON.stringify({ gcs_path: gcsPath }),
  });
}

/** PUT a file straight to GCS (direct, not through the proxy — avoids buffering bytes). */
export async function uploadToGcs(
  uploadUrl: string,
  file: File,
  uploadHeaders: Record<string, string> = {},
  correlationId?: string,
  signal?: AbortSignal,
): Promise<void> {
  try {
    const res = await fetch(uploadUrl, {
      method: "PUT",
      headers: { "Content-Type": uploadContentTypeForFile(file), ...uploadHeaders },
      body: file,
      signal,
    });
    if (!res.ok) throw new Error(`Upload failed (${res.status})`);
  } catch (err) {
    // A fetch TypeError is ambiguous: bucket CORS blocking this origin (any
    // localhost) OR a network drop mid-PUT (iOS tab suspension, blip). Only
    // relay where the relay can actually succeed — the Next proxy buffers the
    // whole body in memory and Vercel caps request bodies at ~4.5MB, so
    // relaying a real video is a guaranteed cryptic 413/504 after paying the
    // upload cost twice.
    if (err instanceof TypeError) {
      if (canRelayFallback(file)) {
        await relaySignedUpload(uploadUrl, file, uploadHeaders, correlationId, signal);
        return;
      }
      throw new Error(UPLOAD_INTERRUPTED_MESSAGE);
    }
    throw err;
  }
}

// The Next proxy relay buffers the full body in memory and Vercel caps request
// bodies at ~4.5MB — the relay is only viable for small files, or on local dev
// where the proxy is local and uncapped.
const RELAY_FALLBACK_MAX_BYTES = 4 * 1024 * 1024;

// Single source for the interrupted-upload copy — thrown by both uploaders and
// asserted by tests; editing one copy but not the other would silently diverge.
export const UPLOAD_INTERRUPTED_MESSAGE = "Upload interrupted. Check your connection and retry.";

// Local-dev origins (incl. testing from a phone against a dev box over LAN /
// tailscale): the relay proxies to the SAME origin's `next dev`, which buffers
// locally with no Vercel body cap — any size may relay. Vercel previews are
// deliberately NOT matched (their proxy IS capped).
function isLocalDevHost(host: string): boolean {
  if (host === "localhost" || host === "127.0.0.1" || host === "[::1]") return true;
  if (host.endsWith(".local") || host.endsWith(".ts.net")) return true;
  if (/^10\./.test(host) || /^192\.168\./.test(host)) return true;
  if (/^172\.(1[6-9]|2\d|3[01])\./.test(host)) return true;
  return false;
}

function canRelayFallback(file: File): boolean {
  const host = typeof window !== "undefined" ? window.location.hostname : "";
  if (isLocalDevHost(host)) return true;
  return file.size <= RELAY_FALLBACK_MAX_BYTES;
}

/** Server-side PUT of `file` to `signedUrl` via the API relay (bucket-CORS bypass). */
async function relaySignedUpload(
  signedUrl: string,
  file: File,
  uploadHeaders: Record<string, string> = {},
  correlationId?: string,
  signal?: AbortSignal,
): Promise<void> {
  const form = new FormData();
  form.append("file", file, file.name);
  form.append("signed_url", signedUrl);
  form.append("content_type", uploadContentTypeForFile(file));
  form.append("file_size_bytes", String(file.size));
  const ifGenerationMatch = uploadHeaders["x-goog-if-generation-match"];
  if (ifGenerationMatch) form.append("if_generation_match", ifGenerationMatch);
  const res = await fetch(`${PLAN_BASE}/uploads/relay`, {
    method: "POST",
    body: form,
    headers: correlationId ? { "X-Correlation-Id": correlationId } : undefined,
    signal,
  });
  if (res.status === 401) throw new NotAuthenticatedError();
  if (!res.ok) {
    let detail = `Upload failed (${res.status})`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // non-JSON error body; keep the generic message
    }
    throw new Error(detail);
  }
}

/**
 * PUT a file to GCS with progress reporting and abort support (XHR-based).
 *
 * onProgress is called with a value 0–1 as bytes are sent. The second arg is
 * true only while the relay fallback is in flight — fetch-multipart has no
 * byte events, so that phase is indeterminate (render shimmer, not a number).
 * Pass an AbortSignal to cancel mid-upload (slot returns to idle). NOTE: a
 * cancelled/partial object persists under users/… — that prefix has NO
 * lifecycle delete rule; cleanup is tracked in TODOS.md ("Upload follow-ups").
 */
// No byte movement for this long aborts the transfer as interrupted — a dead
// mobile connection with no RST would otherwise freeze the card at N% forever
// and pin an upload slot. Deliberately activity-based, not a total timeout:
// large videos on slow links are fine as long as bytes keep flowing.
const UPLOAD_STALL_TIMEOUT_MS = 60_000;
const UPLOAD_STALL_CHECK_MS = 10_000;

export function uploadToGcsWithProgress(
  uploadUrl: string,
  file: File,
  onProgress: (fraction: number, indeterminate?: boolean) => void,
  signal?: AbortSignal,
): Promise<void> {
  if (signal?.aborted) {
    // A listener registered after the abort event never fires — reject now or
    // the full file uploads anyway.
    return Promise.reject(new DOMException("Upload cancelled", "AbortError"));
  }
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let lastActivityAt = Date.now();
    let stalled = false;
    const stallTimer = setInterval(() => {
      if (Date.now() - lastActivityAt > UPLOAD_STALL_TIMEOUT_MS) {
        stalled = true;
        xhr.abort();
      }
    }, UPLOAD_STALL_CHECK_MS);
    const settle = <T extends unknown[]>(fn: (...args: T) => void) => {
      return (...args: T) => {
        clearInterval(stallTimer);
        fn(...args);
      };
    };

    xhr.upload.addEventListener("progress", (e) => {
      lastActivityAt = Date.now();
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    });
    xhr.addEventListener(
      "load",
      settle(() => {
        if (xhr.status >= 200 && xhr.status < 300) resolve();
        else reject(new Error(`Upload failed (${xhr.status})`));
      }),
    );
    // Network-level failure is ambiguous: bucket CORS blocked this origin
    // (any localhost) OR the connection dropped mid-PUT. Relay only where the
    // relay can succeed (see canRelayFallback); progress becomes indeterminate
    // (0.5) since fetch-multipart has no upload progress events.
    xhr.addEventListener(
      "error",
      settle(() => {
        if (signal?.aborted) {
          reject(new DOMException("Upload cancelled", "AbortError"));
          return;
        }
        if (!canRelayFallback(file)) {
          reject(new Error(UPLOAD_INTERRUPTED_MESSAGE));
          return;
        }
        // Indeterminate: never surface a made-up percent (DESIGN.md D6).
        onProgress(0.5, true);
        relaySignedUpload(uploadUrl, file, {}, undefined, signal)
          .then(() => {
            onProgress(1);
            resolve();
          })
          .catch(reject);
      }),
    );
    xhr.addEventListener(
      "abort",
      settle(() => {
        reject(
          stalled
            ? new Error(UPLOAD_INTERRUPTED_MESSAGE)
            : new DOMException("Upload cancelled", "AbortError"),
        );
      }),
    );

    if (signal) {
      signal.addEventListener("abort", () => xhr.abort(), { once: true });
    }

    xhr.open("PUT", uploadUrl);
    xhr.setRequestHeader("Content-Type", uploadContentTypeForFile(file));
    xhr.send(file);
  });
}

/**
 * Tell the API which clips are now attached to this item.
 *
 * When assignments are provided (shot-slot uploader), the backend validates
 * shot_ids and derives clip_gcs_paths via set_item_clips.
 * When assignments are omitted (legacy/uninstructed), the API treats all clips
 * as pool (unchanged behavior).
 */
export function attachClips(
  itemId: string,
  clipGcsPaths: string[],
  assignments?: ClipAssignment[],
  signal?: AbortSignal,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/clips`, {
    method: "POST",
    body: JSON.stringify({
      clip_gcs_paths: clipGcsPaths,
      ...(assignments !== undefined ? { assignments } : {}),
    }),
    signal,
  });
}

// ── Hidden manual-editor draft lifecycle (Slice 3) ──────────────────────

export interface ManualDraftResponse {
  plan_item_id: string;
  job_id: string;
  variant_id: string | null;
  status: "draft";
}

export interface ManualDraftMedia {
  gcs_path: string;
  duration_s: number;
  kind: "video" | "image";
}

/** Create the caller's manual draft, or return their latest unexported one. */
export function createOrResumeManualDraft(title?: string): Promise<ManualDraftResponse> {
  return request<ManualDraftResponse>("/plan-items/manual-drafts", {
    method: "POST",
    body: JSON.stringify({ ...(title?.trim() ? { title: title.trim() } : {}) }),
  });
}

/** Seed the canonical editor variant from the draft's attached media order. */
export function initializeManualDraft(
  itemId: string,
  media: ManualDraftMedia[],
): Promise<ManualDraftResponse> {
  return request<ManualDraftResponse>(`/plan-items/${itemId}/manual-draft/initialize`, {
    method: "POST",
    body: JSON.stringify({ media }),
  });
}

export function generatePlanItem(
  itemId: string,
  input?: { speech_cleanup_action?: "retry_required" | "disable_and_create"; expected_job_id?: string },
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/generate`, {
    method: "POST",
    ...(input ? { body: JSON.stringify(input) } : {}),
  });
}

export type CreatorAgentSessionStatus =
  | "briefing"
  | "planning"
  | "awaiting_confirmation"
  | "executing"
  | "rendering"
  | "reviewing"
  | "awaiting_feedback"
  | "revising"
  | "completed"
  | "failed"
  | "cancelled";

export interface CreatorAgentEvent {
  id: string;
  role: "user" | "assistant" | "system";
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface CreatorAgentPlanPreview {
  version: number;
  plan_hash: string;
  summary: string;
  creative_rationale?: string;
  edit_format?: string;
  audio_strategy?: string;
  story_structure?: string[];
  caption_style?: string | null;
  intro_hook?: string | null;
}

export interface CreatorAgentSession {
  id: string;
  status: CreatorAgentSessionStatus;
  revision: number;
  render_attempts: number;
  max_render_attempts: number;
  can_render: boolean;
  pending_plan: CreatorAgentPlanPreview | null;
  current_job_id: string | null;
  events: CreatorAgentEvent[];
  created_at: string;
  updated_at: string;
}

export function getCreatorAgentSession(itemId: string): Promise<CreatorAgentSession | null> {
  return request<CreatorAgentSession | null>(`/plan-items/${itemId}/creator-agent/session`);
}

export function startCreatorAgentSession(
  itemId: string,
  message: string,
  clientEventId: string,
): Promise<CreatorAgentSession> {
  return request<CreatorAgentSession>(`/plan-items/${itemId}/creator-agent/session`, {
    method: "POST",
    body: JSON.stringify({ message, client_event_id: clientEventId }),
  });
}

export function turnCreatorAgentSession(
  itemId: string,
  sessionId: string,
  message: string,
  expectedRevision: number,
  clientEventId: string,
): Promise<CreatorAgentSession> {
  return request<CreatorAgentSession>(`/plan-items/${itemId}/creator-agent/turn`, {
    method: "POST",
    body: JSON.stringify({
      session_id: sessionId,
      message,
      expected_revision: expectedRevision,
      client_event_id: clientEventId,
    }),
  });
}

export function confirmCreatorAgentPlan(
  itemId: string,
  session: CreatorAgentSession,
  clientEventId: string,
): Promise<CreatorAgentSession> {
  if (!session.pending_plan) throw new Error("No creator plan is ready to confirm");
  return request<CreatorAgentSession>(`/plan-items/${itemId}/creator-agent/confirm`, {
    method: "POST",
    body: JSON.stringify({
      session_id: session.id,
      expected_revision: session.revision,
      plan_version: session.pending_plan.version,
      plan_hash: session.pending_plan.plan_hash,
      client_event_id: clientEventId,
    }),
  });
}

export function cancelCreatorAgentSession(
  itemId: string,
  sessionId: string,
  expectedRevision: number,
): Promise<CreatorAgentSession> {
  return request<CreatorAgentSession>(`/plan-items/${itemId}/creator-agent/cancel`, {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId, expected_revision: expectedRevision }),
  });
}

export function draftEditProposal(
  itemId: string,
  brief: {
    direction: EditProposalDirection;
    goal: string;
    pace: EditProposalPace;
    duration_s: number;
  },
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/edit-proposal/draft`, {
    method: "POST",
    body: JSON.stringify(brief),
  });
}

export function editProposalConversationTurn(
  itemId: string,
  expectedProposalVersion: number,
  message: string,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/edit-proposal/conversation`, {
    method: "POST",
    body: JSON.stringify({
      expected_proposal_version: expectedProposalVersion,
      message,
    }),
  });
}

export function updateEditProposal(
  itemId: string,
  expectedProposalVersion: number,
  snapshot: EditProposalSnapshot,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/edit-proposal`, {
    method: "PATCH",
    body: JSON.stringify({
      expected_proposal_version: expectedProposalVersion,
      snapshot,
    }),
  });
}

export function approveEditProposal(
  itemId: string,
  expectedProposalVersion: number,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/edit-proposal/approve`, {
    method: "POST",
    body: JSON.stringify({ expected_proposal_version: expectedProposalVersion }),
  });
}

export interface ConfirmDirectionOverrides {
  direction?: EditProposalDirection;
  pace?: EditProposalPace;
  duration_s?: number;
}

export function confirmEditDirection(
  itemId: string,
  expectedProposalVersion: number,
  fingerprint: string,
  overrides: ConfirmDirectionOverrides = {},
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/edit-proposal/confirm-direction`, {
    method: "POST",
    body: JSON.stringify({
      expected_proposal_version: expectedProposalVersion,
      fingerprint,
      ...overrides,
    }),
  });
}

/** Patch one shot in the filming guide (editable text, duration, clip_count). */
export function updatePlanItemShot(
  itemId: string,
  shotId: string,
  patch: { what?: string; how?: string; duration_s?: number; clip_count?: number },
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/shots/${shotId}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

/** Generate a fresh filming guide for an item whose guide is currently empty. */
export function generatePlanItemGuide(itemId: string): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/generate-guide`, { method: "POST" });
}

/** Attach or clear the narrated-walkthrough voiceover GCS path on a plan item. */
export function setItemVoiceover(
  itemId: string,
  voiceoverGcsPath: string | null,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/voiceover`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ voiceover_gcs_path: voiceoverGcsPath }),
  });
}

/** Caption style for a caption-capable variant (narrated or talking-to-camera). */
export type VoiceoverCaptionStyle = "sentence" | "word";

/**
 * Set the background-sound (voice/bed) level for a NARRATED variant post-generation
 * (re-renders, async — NOT the removed generate-time item-scoped setter). Talking-
 * to-camera has no bed to mix, so this route 422s for any other archetype.
 */
export function setPlanItemNarratedBedLevel(
  itemId: string,
  variantId: string,
  bedLevel: number,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/variants/${variantId}/bed-level`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bed_level: bedLevel }),
  });
}

/**
 * Set sentence/word caption style for a caption variant (no re-render — the editor
 * previews it locally; Apply reburns in the chosen style).
 */
export function setPlanItemVariantCaptionStyle(
  itemId: string,
  variantId: string,
  captionStyle: VoiceoverCaptionStyle,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/variants/${variantId}/caption-style`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ caption_style: captionStyle }),
  });
}

/**
 * Subtitles on/off for a caption variant, independent of stored cue count (no
 * re-render — off always yields the caption-free burn on Apply; toggling back on
 * reburns the ORIGINAL cues with no re-transcription).
 */
export function setPlanItemCaptionsEnabled(
  itemId: string,
  variantId: string,
  enabled: boolean,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/variants/${variantId}/captions-enabled`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export function generateFirstWeek(
  planId: string,
): Promise<{ enqueued: number; skipped_no_clips: number }> {
  return request(`/content-plans/${planId}/generate-first-week`, { method: "POST" });
}

/**
 * Variant output for a plan item's render — fetched via the generative status proxy.
 * Mirrors the subset of generative `GenerativeVariant` the plan editor needs: the
 * status endpoint already returns these fields, this type just surfaces them.
 */
// ── Media-overlay types ───────────────────────────────────────────────────────

/**
 * One timed, positioned image/video overlay card on a plan-item variant.
 * Mirrors `MediaOverlay` in app/agents/_schemas/media_overlay.py.
 */
export interface MediaOverlay {
  id: string;
  /** Generated-bundle provenance. Manual/legacy cards omit both fields. */
  source?: string | null;
  effect_group_id?: string | null;
  kind: "image" | "video";
  src_gcs_path: string;
  /** Fresh-signed read URL for the card asset — added by _variants_for_response on every
   *  status read so the browser can show existing applied cards as a live CSS overlay
   *  without re-uploading. Absent on legacy/unsigned cards. */
  preview_url?: string | null;
  /** Optional browser-displayable preview object, e.g. JPEG converted from HEIC. */
  preview_gcs_path?: string | null;
  position: "top" | "center" | "bottom" | "custom";
  x_frac: number;
  y_frac: number;
  scale: number;
  /** Plan 009: "fullscreen" = cover-crop takeover of the whole frame for the
   *  card's window (position/scale ignored at render but preserved for
   *  toggle-back). Absent/unknown coerces to "pip" on the server. */
  display_mode?: "pip" | "fullscreen";
  /** Optional exit treatment. Absent/unknown coerces to "none" on the server. */
  exit_token?: "none" | "dissolve-out";
  /** When the overlay is visible on the main video timeline. */
  start_s: number;
  end_s: number;
  /** Trim bounds within the uploaded clip itself (video cards only). */
  clip_trim_start_s?: number;
  clip_trim_end_s?: number;
  /** Source clip total duration (video cards). Persisted so trim UI survives Apply/reload. */
  clip_duration_s?: number;
  z: number;
}

/**
 * Mirrors `SoundEffectPlacement` in app/agents/_schemas/sound_effect.py.
 */
export interface SoundEffectPlacement {
  id: string;
  /** Glossary effect ID, when picked from the admin-curated list. */
  sound_effect_id?: string | null;
  /** Resolved GCS path (always present after backend validation). */
  src_gcs_path: string;
  /** When in the video to play this effect (seconds). */
  at_s: number;
  /** Volume multiplier 0.0–2.0 (default 1.0). */
  gain: number;
  /** Optional trim bounds within the effect file itself. */
  trim_start_s?: number | null;
  trim_end_s?: number | null;
  /** Total effect file duration (informational, for trim UI). */
  duration_s?: number | null;
  /** Human label for the UI (e.g. "Fah"). */
  label?: string | null;
  /** Optional provenance for generated placements; manual placements use source="user"/"manual". */
  source?: string | null;
  effect_group_id?: string | null;
  smart_role?: string | null;
  smart_event_id?: string | null;
  transcript_hash?: string | null;
}

// NOTE: `PlanItemVariant` is kept structurally assignable to the shared
// `EditableVariant` (lib/variant-editor/types.ts) so the 0-latency instant
// editor (IntroTextPreview + EditToolbar + useVariantEditSession) drives plan-item
// variants exactly as it does generative ones. The fields the shared machinery
// reads (text_mode, style_set_id, intro_text_size_px, render_status,
// base_video_url, intro_layout, intro_mode) must mirror EditableVariant's types
// — keep them in lockstep.
/**
 * Spoken-word timing map (assembled-time), derived server-side from the
 * variant's persisted speech source (sequence transcript or caption-cue
 * words). Feeds the copilot's SPEECH WORDS / PAUSE MARKS sections — word- and
 * pause-precise SFX/text/overlay placement. Keys are compact on purpose
 * (server payload shape): w=word, s=start_s, e=end_s.
 */
export interface VariantSpeechMap {
  source: string;
  words: { w: string; s: number; e: number }[];
  pauses: { s: number; e: number; after: string | null }[];
}

/**
 * Advisory SFX placement proposed by the server's auto sound-design pass
 * (dark-flagged). Stale-filtered server-side against the current transcript
 * hash — anything served is realizable as an ordinary add_sfx.
 */
export interface PendingSfxSuggestion {
  id?: string | null;
  effect_id: string;
  /** Display name of the effect, resolved server-side. */
  name?: string | null;
  at_s: number;
  gain?: number | null;
  reason?: string | null;
  /** Mark-type provenance ("pause" | "word_start" | ...) — debug only. */
  anchor?: string | null;
  transcript_hash?: string | null;
}

/** One editable caption line: display text held over [start_s, end_s] (assembled-time). */
export interface CaptionCue {
  text: string;
  start_s: number;
  end_s: number;
  /**
   * Optional real per-word timings for the word-by-word subtitled style. The editor
   * only edits `text`, but round-trips `words` untouched so the reburn can re-pop the
   * SAME words at their real times; when the user changes the text they no longer spell
   * the words and the server re-synthesizes them. Absent for sentence-style captions.
   */
  words?: {
    text: string;
    start_s: number;
    end_s: number;
    /** Smart chunker alignment confidence — round-tripped untouched. */
    timing_quality?: "aligned" | "segment_estimate" | "unsafe" | null;
  }[] | null;
  /** Server-authored semantic style; preserved when an unchanged cue is applied. */
  smart_style?: "hook" | "context" | "list_item" | "example" | "payoff" | "cta" | null;
  /**
   * Smart Captions v2 provenance (planner role + source transcript word ids).
   * The editor never reads these — it round-trips them untouched (spread) so a
   * text edit doesn't strip them from the persisted cues.
   */
  smart_role?: "hook" | "context_shift" | "list_item" | "example" | "payoff" | "cta" | null;
  smart_word_ids?: string[] | null;
  /**
   * Plan 011/012 provenance, also round-tripped untouched via the spread:
   * `smart_emphasis` marks a named-entity cue isolated for emphasis, and
   * `smart_keep_together` holds the line-layout adjacency pairs the reburn honors.
   * Stripping them on a text edit would silently lose the emphasis/layout look.
   */
  smart_emphasis?: boolean | null;
  smart_keep_together?: number[][] | null;
  /**
   * Per-cue style overrides (Lane PR-A "This caption" section) — distinct from
   * the variant-level "All captions" globals (voiceover_caption_font,
   * caption_size_px, caption_text_color, ...). Absent/undefined on every field
   * means the cue inherits those variant defaults; a set value overrides ONLY
   * this cue. `font_family` is a font-registry KEY, same contract as the
   * variant-level `voiceover_caption_font`. Mirrors
   * `app/routes/generative_jobs.py` CaptionCue.
   */
  font_family?: string | null;
  text_color?: string | null;
  size_px?: number | null;
}

/**
 * One timed text block in the editorial/authoring layer.
 * Mirrors `TextElement` in app/agents/_schemas/text_element.py — all field names,
 * union literals, and nullability rules are kept in lockstep so round-trips are
 * byte-identical.
 */
export interface TextElement {
  id: string;
  text: string;
  start_s: number;
  end_s: number;
  role: "generative_intro" | "generative_sequence" | "lyric_line";
  visual_block_id?: string | null;
  position?: "top" | "middle" | "bottom" | "custom";
  x_frac?: number | null;
  y_frac?: number | null;
  rotation_deg?: number | null;
  font_family?: string | null;
  size_px?: number | null;
  size_class?: "small" | "medium" | "large" | "xlarge" | "xxlarge" | "jumbo" | null;
  color?: string | null;
  highlight_color?: string | null;
  stroke_width?: number | null;
  shadow_enabled?: boolean | null;
  shadow_style?: "standard" | "high_visibility" | null;
  glow_color?: string | null;
  glow_strength?: number | null;
  alignment?: "left" | "center" | "right" | null;
  effect?:
    | "static"
    | "none"
    | "fade-in"
    | "slide-up"
    | "slide-down"
    | "karaoke-line"
    | "pop-in"
    | "scale-up"
    | "typewriter"
    | "stream-in"
    | "staggered-slice"
    | "ink-reveal"
    | "handwriting"
    | "dissolve-out"
    | "bounce"
    | "slide-in"
    | "smooth-type"
    | null;
  /** Optional v2 motion. Absence is the exact legacy timing contract. */
  motion?: TextMotionConfigV2 | null;
  theme_transition?: {
    type: "giant-title-wipe";
    target_glyph?: string | null;
  } | null;
  /** Display-case transform, resolved at compile/layout time (T11 slice;
   * parity fixture tests/fixtures/text-element-parity/text_case.json). */
  text_case?: "none" | "upper" | "lower" | "title" | null;
  /** Tracking in em (× font size), clamped [-0.05, 0.5] server-side (T11;
   * parity fixture letter_spacing.json). */
  letter_spacing?: number | null;
  /** Line-height multiplier, clamped [0.5, 3.0]; null = renderer default 1.15
   * (T11; parity fixture line_spacing.json). */
  line_spacing?: number | null;
  /** Maximum wrap-box width as a frame-width fraction, clamped [0.2, 1.0];
   * null = renderer default 0.9 (parity fixture max_width_frac.json). */
  max_width_frac?: number | null;
  fade_out_ms?: number | null;
  reveal_s?: number | null;
  z?: number | null;
  word_timings?: Record<string, unknown>[] | null;
  source_params?: Record<string, unknown> | null;
  removed?: boolean;
  /** Occlude this text behind the moving subject (text-behind-subject feature).
   * Render-only compositing flag — NOT in PARITY_VERIFIED_FIELDS, since the
   * browser CSS preview cannot segment the subject to show it. */
  behind_subject?: boolean;
}

export interface VisualCrop {
  x_frac: number;
  y_frac: number;
  scale: number;
}

export interface VisualShot {
  id: string;
  asset_id: string;
  src_gcs_path: string;
  kind: "image" | "video";
  start_offset_s: number;
  duration_s: number;
  trim_start_s?: number | null;
  crop: VisualCrop;
  motion: "none" | "zoom_in" | "zoom_out" | "pan_left" | "pan_right";
  sync_anchor?: {
    type: "sentence" | "keyword" | "beat" | "manual";
    time_s: number;
    label?: string | null;
  } | null;
}

export interface VisualBlockBase {
  version: 1;
  id: string;
  start_s: number;
  end_s: number;
  timing_mode: "auto" | "manual";
  origin: "ai" | "user";
  rationale?: string | null;
  transition_in: "cut" | "fade";
  transition_out: "cut" | "fade";
  audio_policy: {
    base: "continue" | "mute";
    sfx: "continue" | "mute";
  };
}

export interface MediaTransform {
  fit_mode: "contain" | "cover";
  focal_x: number;
  focal_y: number;
  zoom: number;
}

export interface MediaVisualBlock extends VisualBlockBase {
  kind: "media";
  asset_id: string;
  src_gcs_path: string;
  preview_gcs_path?: string | null;
  media_kind: "image" | "video";
  source_duration_s?: number | null;
  trim_start_s?: number | null;
  trim_end_s?: number | null;
  display_mode: "fullscreen" | "overlay";
  transform: MediaTransform;
  x_frac: number;
  y_frac: number;
  scale: number;
  z: number;
  source?: string | null;
  effect_group_id?: string | null;
}

export interface MontageVisualBlock extends VisualBlockBase {
  kind: "montage";
  shots: VisualShot[];
}

export type TextCardBackground =
  | { type: "solid"; color: string }
  | { type: "gradient"; from: string; to: string; angle_deg: number }
  | { type: "blur_previous"; blur_px: number }
  | { type: "asset"; shot: VisualShot };

export interface TextCardVisualBlock extends VisualBlockBase {
  kind: "text_card";
  style_preset_id?: string | null;
  background: TextCardBackground;
}

export type VisualBlock = MontageVisualBlock | TextCardVisualBlock | MediaVisualBlock;

/**
 * Plan 009 ARCH-4: variant-level apply receipt — mirrors the dict written by
 * `overlay_apply.py` / the zero-click autoplace task into
 * `variants[i]["overlay_apply_receipt"]`. All fields optional (the two writers
 * populate different subsets); `reason` is "hook"/"intro" for intro-protection
 * demotions, other strings (e.g. "overlap") otherwise.
 */
export interface OverlayApplyReceipt {
  dropped?: number;
  demoted?: number;
  reason?: string;
  at?: string;
}

export interface CameraEffect {
  id: string;
  token?: "semantic_crop_pulse" | string;
  start_s: number;
  end_s: number;
  intensity: number;
  easing: "sine_pulse";
  source: "smart_captions" | "user" | string;
  effect_group_id?: string | null;
  event_id?: string | null;
  role?: string | null;
}

export type EditorOperationCapability =
  | boolean
  | { editable: boolean; reason?: string | null };

/**
 * Per-variant editor capability map — mirrors `_editor_capabilities` in
 * app/routes/generative_jobs.py. All-false ⇒ the editor shell is read-only;
 * per-section false gates that tool with its honest `*_reason`.
 */
export interface EditorCapabilities {
  /** Upload protocol selected by the server for this editor session. */
  overlay_upload_mode?: "legacy" | "pool";
  /** V2 guided-story operation gates. Absent on legacy variants. */
  clips?: {
    add?: EditorOperationCapability;
    remove?: EditorOperationCapability;
    reorder?: EditorOperationCapability;
    split?: EditorOperationCapability;
    trim?: EditorOperationCapability;
    transitions?: EditorOperationCapability;
    looks?: EditorOperationCapability;
    edit_wide_looks?: EditorOperationCapability;
  };
  music_operations?: {
    swap?: EditorOperationCapability;
    remove?: EditorOperationCapability;
    level?: EditorOperationCapability;
    window?: EditorOperationCapability;
  };
  /** Story-native timed-lane operation gates. Existing top-level booleans are
   * retained below for compatibility with older web builds. */
  lanes?: {
    text?: EditorOperationCapability;
    sfx?: EditorOperationCapability;
    overlays?: EditorOperationCapability;
    visual_blocks?: EditorOperationCapability;
    motion_scenes?: EditorOperationCapability;
    orientation?: EditorOperationCapability;
  };
  nova?: {
    trim_clip_start?: EditorOperationCapability;
    trim_output_start?: EditorOperationCapability;
    remove_music?: EditorOperationCapability;
  };
  text_elements?: boolean;
  timeline?: boolean;
  split_clips?: boolean;
  automatic_cut?: boolean;
  automatic_cut_reason?: string | null;
  mix?: boolean;
  sfx?: boolean;
  overlays?: boolean;
  visual_blocks?: boolean;
  motion_scenes?: boolean;
  /** Backend half of the dual Evolving Type rollout gate. New insertion and
   * editing require this plus NEXT_PUBLIC_EVOLVING_TYPE_ENABLED; persisted
   * blocks remain visible/removable independently. */
  evolving_type?: boolean;
  motion_runtime_hash?: string | null;
  motion_required_runtime_hash?: string | null;
  camera_effects?: boolean;
  background_music?: boolean;
  /** Whether the existing primary song may be swapped/removed in this editor. */
  swap_song?: boolean;
  /** Whether legacy item-title controls may rewrite the rendered intro. */
  intro_controls?: boolean;
  /** AI overlay suggestions inside the editor's Overlays drawer (plans/005-010).
   *  Deliberately does NOT check pool assets — the drawer owns the empty-pool state. */
  suggestions?: boolean;
  /** Carousel-as-a-moment (see CarouselMoment / PlanItemVariant.carousel_moment). */
  carousel?: boolean;
  carousel_reason?: string | null;
  reason?: string;
  sfx_reason?: string | null;
  overlays_reason?: string | null;
  visual_blocks_reason?: string | null;
  motion_scenes_reason?: string | null;
  camera_effects_reason?: string | null;
  /** "autoplace_disabled" | "song_or_lyric_variant" | "caption_archetype"
   *  | inherited overlay reasons. */
  suggestions_reason?: string | null;
  lyrics?: {
    editable: boolean;
    enabled: boolean;
    can_toggle_on: boolean;
    reason: "disabled" | "no_track" | "no_renderable_lyrics" | null;
    /**
     * "elements" = lyrics-optional model: lyrics are NOT baked into the
     * render; the Lyrics toggle inserts/removes ordinary `role: "lyric_line"`
     * text_elements client-side (instant, no render round-trip). "baked" (or
     * absent, on rows minted before this field shipped) = legacy model: lyrics
     * are burned into the base video; the toggle only affects local bar
     * visibility and edits persist via `lyrics.line_overrides`.
     */
    lyrics_model?: "elements" | "baked";
  };
  orientation?: {
    editable: boolean;
    value: string;
    reason: "disabled" | "orientation_unsupported" | string | null;
  };
  music_window?: {
    editable: boolean;
    preserve_available: boolean;
    video_duration_s: number;
    track_duration_s: number;
    recommended_start_s: number;
    beat_timestamps_s: number[];
    reason:
      | "track_unavailable"
      | "video_duration_unknown"
      | "track_duration_unknown"
      | "song_shorter_than_video"
      | "timing_metadata_unavailable"
      | null;
    preserve_reason: "linear_timeline_unavailable" | null;
  };
}

export interface TextPlacementCandidate {
  source: "clip_safe_zone" | "masonry_whitespace" | string;
  x_frac: number;
  y_frac: number;
  max_width_frac: number;
  rotation_deg?: number | null;
  masonry_motion?: Record<string, unknown> | null;
  confidence?: number | null;
}

export interface PlanItemVariant {
  variant_id: string;
  output_url: string | null;
  download_url?: string | null;
  duration_s?: number | null;
  // Literal union (not bare string) to match EditableVariant — every plan
  // consumer compares against these literals, so this is non-breaking.
  render_status: "draft" | "ready" | "rendering" | "failed" | null;
  /** Hidden manual-editor lifecycle. The first Save exports this variant. */
  manual_draft?: boolean;
  // Edit controls: swap-song is hidden when music_track_id is null (the
  // original-audio variant has no song), and the style picker reflects style_set_id.
  text_mode: "lyrics" | "agent_text" | "none";
  orientation?: "portrait" | "landscape";
  lyrics_enabled?: boolean;
  lyric_line_overrides?: Record<string, unknown> | null;
  music_track_id?: string | null;
  track_title?: string | null;
  /**
   * Fresh-signed preview URL (+ best-section offset) for the matched track,
   * minted on every status read — present even for unpublished tracks, which
   * the public /music-tracks gallery filters out. The editor's virtual
   * preview falls back to these when the gallery has no entry for the track.
   */
  music_preview_url?: string | null;
  music_preview_start_s?: number | null;
  background_music?: {
    track_id: string;
    title: string;
    artist?: string | null;
    preview_url: string;
    src_gcs_path: string;
    start_s: number;
    end_s: number;
    duration_s: number;
    track_duration_s: number;
    gain_db: number;
    muted: boolean;
    enabled: boolean;
  } | null;
  style_set_id: string | null;
  // Agent-decided (or user-pinned) intro size — drives the ±size stepper.
  intro_text_size_px: number | null;
  intro_size_source?: "computed" | "user" | null;
  // Persisted intro text + effective layout — drive the Classic/Editorial pick
  // (cluster needs a 3-6 word hook, so the chip gates on intro_text length).
  intro_text?: string | null;
  intro_layout?: "linear" | "cluster" | null;
  // Intro rendering mode (D6/D19). "sequence" = transcript-synced typographic
  // sequence — text edits are server-rejected (422); size nudge + layout
  // opt-out stay allowed. Absent on legacy variants.
  intro_mode?: "sequence" | "cluster" | "linear" | null;
  // Convenience flag from the backend: true iff intro_mode === "sequence".
  sequence_synced?: boolean | null;
  // Whether the AI-intro overlay is occluded behind the moving subject
  // (text-behind-subject feature). Absent/false on legacy variants and when
  // the backend flag is off. See `text_behind_subject` on EditVariantPayload.
  intro_behind_subject?: boolean | null;
  // Instant editor: fresh-signed playback URL + GCS key of the text-free
  // fast-reburn base. The API's `_variants_for_response` already signs these for
  // plan-item renders (the plan flow just discarded them before); their presence
  // is what makes a variant instant-edit-eligible. Absent on lyrics/legacy.
  base_video_url?: string | null;
  base_video_path?: string | null;
  /** Approved guided-story timeline and strict publication evidence. */
  story_timeline?: Array<Record<string, unknown>> | null;
  proposal_version?: number | null;
  media_digest?: string | null;
  render_receipt?: Record<string, unknown> | null;
  motion_scenes?: MotionPresetInstance[] | null;
  motion_runtime_hash?: string | null;
  motion_applied_runtime_hash?: string | null;
  motion_cache_stale?: boolean;
  // Narrated on-video caption editor: editable cues over the caption-free base.
  // Present only on narrated variants; null otherwise.
  caption_cues?: CaptionCue[] | null;
  // Spoken-word + pause timing map for copilot speech-synced placement.
  // Absent when the variant has no persisted word-level speech source.
  speech_map?: VariantSpeechMap | null;
  speech_cut_candidates?: Array<{
    candidate_id: string;
    start_s: number;
    end_s: number;
    reason: string;
    preview: string;
    status: "pending";
    revision: string;
  }> | null;
  speech_cut_revision?: string | null;
  speech_cut_in_flight?: {
    operation: "apply_speech_cut_candidate" | "restore_original_timing";
    candidate_id?: string;
    desired_forced_removals: Array<{
      start_s: number;
      end_s: number;
      reason: string;
      candidate_id?: string;
    }>;
    desired_disabled: boolean;
  } | null;
  speech_cut_last_receipt?: SpeechCutOperation | null;
  speech_cut_last_error?: {
    operation_id?: string | null;
    message: string;
  } | null;
  silence_cut?: {
    removed?: Array<{ start_s: number; end_s: number; reason: string }>;
    time_saved_s?: number;
    outcome?: "applied" | "no_change" | "insufficient_source_speech";
  } | null;
  silence_cut_outcome?: "applied" | "no_change" | "insufficient_source_speech" | null;
  speech_cleanup_failure_reason?: string | null;
  // Advisory SFX placements from the auto sound-design pass (dark-flagged).
  // null = freshness unverifiable right now (hold prior state); [] = verified,
  // none fresh. Distinct on purpose.
  pending_sfx_suggestions?: PendingSfxSuggestion[] | null;
  // Subtitles on/off, independent of caption_cues length — off always yields the
  // caption-free burn on Apply. Null/absent on legacy variants ⇒ treat as enabled
  // (matches the render-time default). See setPlanItemCaptionsEnabled.
  captions_enabled?: boolean | null;
  // "sentence" (full lines) or "word" (one word at a time). Present on narrated
  // + talking-to-camera variants. See setPlanItemVariantCaptionStyle.
  voiceover_caption_style?: VoiceoverCaptionStyle | null;
  // Background-sound (voice/bed) level — narrated only. Null = Kria's render-time
  // default. See setPlanItemNarratedBedLevel / BackgroundSoundControl.
  voiceover_bed_level?: number | null;
  // Generic rendered bed level returned by editor-capable variants (voiceover +
  // montage). Older narrated rows may only carry voiceover_bed_level.
  mix?: number | null;
  // Caption font (font-registry key) for narrated captions. Null = default (TikTok
  // Sans). Editable in the on-video caption editor; the reburn honors it.
  voiceover_caption_font?: string | null;
  // Caption appearance overrides for caption archetypes. Null/absent means the
  // renderer's legacy defaults.
  caption_size_px?: number | null;
  caption_text_color?: string | null;
  caption_highlight_color?: string | null;
  caption_stroke_width?: number | null;
  caption_shadow_enabled?: boolean | null;
  // ASS MarginV for captioned variants after the caption-position control is used.
  // Null/absent means legacy default: subtitled 384, narrated 180.
  caption_margin_v?: number | null;
  // Language the subtitled captions were transcribed in ("en" | "tr"). Shown as the
  // editor chip; changing it re-transcribes (setPlanItemCaptionLanguage). Absent for
  // narrated/montage variants.
  caption_language?: string | null;
  // What actually rendered. "narrated" → captions are edited via CaptionEditor and
  // the hero shows the burned output, so it is NOT instant-edit-eligible. Absent
  // on legacy/montage variants. See isInstantEditEligible (variant-editor/eligibility).
  resolved_archetype?: string | null;
  /** Montage preset selected at generation time. Present only for non-classic presets. */
  montage_preset?: RenderedMontagePreset | null;
  /** Visual assembler that actually rendered; collage presets disable clip-timeline editing. */
  montage_preset_rendered?: RenderedMontagePreset | null;
  /** Best-effort fallback reason when a selected preset rendered via classic montage. */
  montage_preset_fallback?: string | null;
  render_generation_id?: string | null;
  render_started_at?: string | null;
  render_finished_at?: string | null;
  error_class?: string | null;
  /**
   * Assigned shot clips that couldn't be placed in this variant.
   * Absent on pool-only / legacy jobs and when all assigned shots landed.
   * reason: "song_too_short" | "unusable_footage"
   */
  unplaced_shots?: Array<{
    clip_id: string;
    gcs_path: string | null;
    shot_index: number;
    reason: "song_too_short" | "unusable_footage";
  }> | null;
  intro_font_family?: string | null;
  intro_effect?: string | null;
  intro_text_color?: string | null;
  intro_cluster_hero_font?: string | null;
  intro_cluster_body_font?: string | null;
  intro_cluster_accent_font?: string | null;
  intro_cluster_hero_size_px?: number | null;
  intro_cluster_body_size_px?: number | null;
  intro_cluster_accent_size_px?: number | null;
  /**
   * T6: Synthesized (or user-edited) text elements for this variant.
   * Null when text_mode === "lyrics" or the variant has no text layer.
   * Populated lazily by the read adapter (`text_elements_for_variant`) on
   * the first status read after T1 lands; absent on legacy variants until
   * they are first fetched.
   */
  text_elements?: TextElement[] | null;
  /** Smart text placement candidates computed from composition/layout whitespace. */
  text_placement_candidates?: TextPlacementCandidate[] | null;
  /**
   * T6: True once the user has applied a PUT text-elements edit. The flag
   * prevents the read adapter from overwriting user edits on re-render.
   */
  text_elements_user_edited?: boolean;
  /** Media-overlay cards applied on top of this variant (slice 1). */
  media_overlays?: MediaOverlay[] | null;
  /** Full-frame replacement blocks rendered below authored text and captions. */
  visual_blocks?: VisualBlock[] | null;
  /** Read-time signed preview URLs keyed by persisted media visual-block id. */
  visual_block_preview_urls?: Record<string, string> | null;
  /** Editable semantic camera emphasis applied only to the base video layer. */
  camera_effects?: CameraEffect[] | null;
  /** Cached text-free visual-block composite used for fast text reburns. */
  visual_blocks_base_path?: string | null;
  /** GCS key of the clean (un-carded) variant before the first overlay apply-pass. */
  pre_media_overlay_video_path?: string | null;
  /** Desired overlay metadata differs from the last successful render. */
  media_overlays_render_dirty?: boolean;
  /** Fresh-signed playback URL for `pre_media_overlay_video_path`, added by
   *  `_variants_for_response` on every status read. Present only once a card
   *  burn has captured the clean base (absent when no burn ever happened).
   *  Drives the hero's live-edit mode: the base plays under a live CSS card
   *  layer so timeline edits preview instantly without an FFmpeg re-burn. */
  pre_overlay_video_url?: string | null;
  /** Sound-effect placements applied as the outermost audio layer. */
  sound_effects?: SoundEffectPlacement[] | null;
  /** GCS key of the clean (sfx-free) variant before the first SFX apply-pass. */
  pre_sfx_video_path?: string | null;
  /**
   * Plan 007: autoplace suggestion-run state on this variant.
   * "matching" keeps the page polling (the zero-click chain runs server-side
   * after variants_ready); "ready"/"zero"/"failed" are rail states.
   */
  overlay_suggest_status?: "matching" | "ready" | "zero" | "failed" | null;
  /**
   * Plan 009 ARCH-4 ("never silent"): apply-time guardrail receipt. Written by
   * the apply path / zero-click task when suggestions were demoted to pip or
   * dropped for overlap; cleared (null) on the next apply/clear. T5 renders it
   * as a quiet zinc line; it must disappear when null.
   */
  overlay_apply_receipt?: OverlayApplyReceipt | null;
  /**
   * PR-D: Scene timings for sequence variants. Each entry is one synced scene
   * with its start/end in assembled-video seconds. Absent on non-sequence variants.
   */
  scene_timings?: Array<{
    text: string;
    start_s: number | null;
    end_s: number | null;
  }> | null;
  /**
   * PR-E: Intro overlay timing in assembled-video seconds.
   * Present on agent_text/agent_text variants after PR-C lands intro timing
   * in the polled payload. Used to seed the generative_intro bar timing
   * and gate the setPlanItemIntroTiming save path.
   */
  intro_start_s?: number | null;
  intro_end_s?: number | null;
  /** Editor-shell capability map (see EditorCapabilities). Absent on legacy reads. */
  editor_capabilities?: EditorCapabilities | null;
  /**
   * Carousel-as-a-moment: current state of the variant's carousel, if any.
   * null/absent = no carousel configured. Set via setVariantCarouselMoment,
   * which drives the same full-render dispatch as intro_layout.
   */
  carousel_moment?: CarouselMoment | null;
}

export function retimeVisualBlock(
  itemId: string,
  variantId: string,
  visualBlock: VisualBlock,
): Promise<{ visual_block: VisualBlock }> {
  return request<{ visual_block: VisualBlock }>(
    `/plan-items/${itemId}/variants/${variantId}/visual-blocks/retime`,
    {
      method: "POST",
      body: JSON.stringify({ visual_block: visualBlock }),
    },
  );
}

export async function getPlanItemVariants(jobId: string): Promise<PlanItemVariant[]> {
  const res = await request<{ variants: PlanItemVariant[] }>(`/generative-jobs/${jobId}/status`);
  return res.variants ?? [];
}

// ── Lyrics-optional "elements" model ────────────────────────────────────────

export interface LyricSeedsResponse {
  elements: TextElement[];
}

export type LyricSeedsErrorReason = "not_found" | "no_lyrics";

/**
 * Thrown by getLyricSeeds on the two documented non-2xx outcomes: 404 (flag
 * off / not an elements-model variant — treat as feature-unavailable) and 422
 * (the variant's matched track has no renderable lyrics). Any other failure
 * throws a plain Error, same as `request()`.
 */
export class LyricSeedsError extends Error {
  reason: LyricSeedsErrorReason;
  constructor(reason: LyricSeedsErrorReason, detail?: string) {
    super(
      detail ??
        (reason === "no_lyrics"
          ? "This song doesn't have synced lyrics"
          : "Lyrics aren't available for this edit"),
    );
    this.name = "LyricSeedsError";
    this.reason = reason;
  }
}

/**
 * Beat-synced lyric lines for an "elements"-model variant, projected as
 * TextElement-shaped dicts (id "lyr-L<n>", role "lyric_line", server-locked
 * start_s/end_s). Fetched once (callers should cache per variant) when the
 * Lyrics toggle is switched on — inserting the result is a normal client-side
 * bar mutation, not a render round-trip.
 */
export async function getLyricSeeds(
  itemId: string,
  variantId: string,
): Promise<LyricSeedsResponse> {
  const res = await fetch(`${PLAN_BASE}/plan-items/${itemId}/variants/${variantId}/lyric-seeds`);
  if (res.status === 401) throw new NotAuthenticatedError();
  if (res.status === 404 || res.status === 422) {
    let detail: string | undefined;
    try {
      detail = ((await res.json()) as { detail?: string })?.detail;
    } catch {
      /* non-JSON body — LyricSeedsError falls back to its default copy */
    }
    throw new LyricSeedsError(res.status === 404 ? "not_found" : "no_lyrics", detail);
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // non-JSON error body; keep the generic message
    }
    throw new Error(detail);
  }
  return (await res.json()) as LyricSeedsResponse;
}

// ── Per-variant editing (swap song / edit text / change style) ────────────────
// These POST through the authenticated /api/plan proxy (it injects X-User-Id +
// the server-only INTERNAL_API_KEY), so mutation is ownership-checked server-side.
// All three return the refreshed PlanItem.

export function swapPlanItemSong(
  itemId: string,
  variantId: string,
  newTrackId: string,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/variants/${variantId}/swap-song`, {
    method: "POST",
    body: JSON.stringify({ new_track_id: newTrackId }),
  });
}

export function retextPlanItem(
  itemId: string,
  variantId: string,
  opts: { text?: string; remove?: boolean },
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/variants/${variantId}/retext`, {
    method: "POST",
    body: JSON.stringify({ text: opts.text ?? null, remove: opts.remove ?? false }),
  });
}

/**
 * Persist hand-edited narrated caption cues (no re-render — the player overlays
 * them instantly). Call as the creator types (debounced). Apply reburns them.
 */
export function setPlanItemCaptions(
  itemId: string,
  variantId: string,
  cues: CaptionCue[],
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/variants/${variantId}/captions`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cues }),
  });
}

/**
 * Set the caption font for a narrated variant (no re-render — the editor previews
 * it; Apply reburns in the chosen font). Applies to both sentence and word styles.
 * `font` is a font-registry key (e.g. "Montserrat Bold"); null resets to default.
 */
export function setPlanItemCaptionFont(
  itemId: string,
  variantId: string,
  font: string | null,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/variants/${variantId}/caption-font`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ caption_font: font }),
  });
}

/**
 * Set caption vertical position and reburn the captioned variant.
 * `yFrac` is normalized from the top of the 9:16 frame.
 */
export function setPlanItemCaptionPosition(
  itemId: string,
  variantId: string,
  yFrac: number,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/variants/${variantId}/caption-position`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ y_frac: yFrac }),
  });
}

/** Reburn the edited caption cues onto the caption-free base (async re-render). */
export function applyPlanItemCaptions(itemId: string, variantId: string): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/variants/${variantId}/captions/apply`, {
    method: "POST",
  });
}

/** Caption languages the subtitled style can transcribe into. */
export type CaptionLanguage = "en" | "tr";

/**
 * Change a subtitled variant's caption language → re-transcribe its own audio in that
 * language and reburn (async). REPLACES the current cues + any hand-edits — confirm
 * with the user first. Subtitled-only (422 otherwise).
 */
export function setPlanItemCaptionLanguage(
  itemId: string,
  variantId: string,
  language: CaptionLanguage,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/variants/${variantId}/caption-language`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ language }),
  });
}

export function editPlanItemVariant(
  itemId: string,
  variantId: string,
  payload: EditVariantPayload,
): Promise<PlanItem> {
  // Combined batch-edit endpoint — mirrors the public generative /edit byte-for-byte
  // (the backend route reuses the SAME `EditVariantRequest` model + `dispatch_edit_variant`
  // render path, see src/apps/api/app/routes/plan_items.py edit_item_variant). Drives
  // the plan page's instant editor (one /edit per "Done" commit) AND the legacy
  // Classic/Editorial layout pick. text/remove_text are mutually exclusive; size is
  // rounded to match the server's int field.
  return request<PlanItem>(`/plan-items/${itemId}/variants/${variantId}/edit`, {
    method: "POST",
    body: JSON.stringify({
      ...payload,
      text_size_px:
        payload.text_size_px !== undefined ? Math.round(payload.text_size_px) : undefined,
    }),
  });
}

/**
 * Carousel-as-a-moment: add/update (partial merges over the current moment
 * server-side) or remove (pass `null`) the variant's carousel. Thin wrapper
 * over editPlanItemVariant — same combined batch-edit endpoint, same full
 * re-render lifecycle as the Classic/Editorial intro_layout switch. `null` is
 * sent as a literal JSON `null` (remove); omitting the call entirely is the
 * only way to leave the moment unchanged, since editPlanItemVariant always
 * serializes whatever `config` value it's given.
 */
export function setVariantCarouselMoment(
  itemId: string,
  variantId: string,
  config: CarouselMoment | null,
): Promise<PlanItem> {
  return editPlanItemVariant(itemId, variantId, { carousel_moment: config });
}

export function changePlanItemStyle(
  itemId: string,
  variantId: string,
  styleSetId: string,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/variants/${variantId}/change-style`, {
    method: "POST",
    body: JSON.stringify({ style_set_id: styleSetId }),
  });
}

/**
 * Raw EffectSpec wire shape (see src/apps/api/app/pipeline/custom_effects.py) —
 * the server owns the filter whitelist and param bounds; this type documents
 * the request shape only and does not re-validate it client-side.
 */
export interface CustomEffectSpec {
  id: string;
  label: string;
  filters: Array<{ name: string; params?: Record<string, number | string> }>;
  start_s: number;
  end_s: number;
  target: "full_frame";
}

/**
 * Apply Nova's sandboxed effect language (agent-authored FFmpeg filter chain
 * from a validated whitelist, PR6 of the effect-language train) to a
 * variant's video (async re-render). v1: a single active custom effect —
 * each call REPLACES any previously-applied one, never stacks. Dark behind
 * CUSTOM_EFFECTS_ENABLED (404 when off).
 *
 * Accepts `CustomEffectSpec` OR a loosely-typed `Record<string, unknown>` —
 * the copilot's `apply_custom_effect` op carries the model-authored spec as
 * an untyped object (ops.ts deliberately doesn't duplicate the filter
 * whitelist in TS); the server is the single source of truth and validates
 * either shape identically via `validate_effect_spec`.
 */
export function applyPlanItemCustomEffect(
  itemId: string,
  variantId: string,
  effect: CustomEffectSpec | Record<string, unknown>,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/variants/${variantId}/custom-effect`, {
    method: "POST",
    body: JSON.stringify({ effect }),
  });
}

export function setPlanItemIntroSize(
  itemId: string,
  variantId: string,
  textSizePx: number,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/variants/${variantId}/intro-size`, {
    method: "POST",
    body: JSON.stringify({ text_size_px: Math.round(textSizePx) }),
  });
}

export function setPlanItemIntroTiming(
  itemId: string,
  variantId: string,
  startS: number,
  endS: number,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/variants/${variantId}/intro-timing`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ start_s: startS, end_s: endS }),
  });
}

export interface SceneTimingPatch {
  scene_index: number;
  start_s: number;
  end_s: number;
}

export function patchPlanItemSceneTiming(
  itemId: string,
  variantId: string,
  overrides: SceneTimingPatch[],
): Promise<PlanItem> {
  return request<PlanItem>(
    `/plan-items/${itemId}/variants/${variantId}/scene-timing`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ overrides }),
    },
  );
}

// ── Media-overlay card upload + apply ─────────────────────────────────────────

/**
 * Request signed PUT URLs for media-overlay card assets.
 * Assets land under users/{uid}/plan/{itemId}/overlays/ (persistent, not 24h-swept).
 */
export async function requestOverlayUploadUrls(
  itemId: string,
  files: { filename: string; content_type: string; file_size_bytes: number }[],
): Promise<UploadUrl[]> {
  const res = await request<{ urls: UploadUrl[] }>(
    `/plan-items/${itemId}/overlay-upload-urls`,
    {
      method: "POST",
      body: JSON.stringify({ files }),
    },
  );
  return res.urls;
}

export interface OverlayUploadConfirmResult {
  gcs_path: string;
  preview_gcs_path?: string | null;
  preview_url?: string | null;
}

export async function confirmOverlayUploads(
  itemId: string,
  files: { gcs_path: string; content_type: string }[],
): Promise<OverlayUploadConfirmResult[]> {
  const res = await request<{ files: OverlayUploadConfirmResult[] }>(
    `/plan-items/${itemId}/overlay-upload-confirm`,
    {
      method: "POST",
      body: JSON.stringify({ files }),
    },
  );
  return res.files;
}

/** Legacy manual-overlay protocol kept for mixed-version rollout compatibility. */
export async function uploadMediaOverlayFiles(
  itemId: string,
  files: { file: File; filename: string; content_type: string; file_size_bytes: number }[],
  signal?: AbortSignal,
): Promise<OverlayUploadConfirmResult[]> {
  const urls = await requestOverlayUploadUrls(
    itemId,
    files.map(({ filename, content_type, file_size_bytes }) => ({
      filename,
      content_type,
      file_size_bytes,
    })),
  );
  await Promise.all(
    urls.map((target, index) =>
      uploadToGcs(target.upload_url, files[index].file, {}, undefined, signal),
    ),
  );
  return confirmOverlayUploads(
    itemId,
    urls.map((target, index) => ({
      gcs_path: target.gcs_path,
      content_type: files[index].content_type,
    })),
  );
}

/**
 * Full-replace the media-overlay card list on a variant.
 * Send an empty array to clear all cards and restore the clean variant.
 * Returns the updated PlanItem (variant flips to render_status="rendering").
 */
export function setVariantMediaOverlays(
  itemId: string,
  variantId: string,
  overlays: MediaOverlay[],
  options?: { render?: boolean },
): Promise<PlanItem> {
  return request<PlanItem>(
    `/plan-items/${itemId}/variants/${variantId}/media-overlays`,
    {
      method: "PUT",
      body: JSON.stringify({ overlays, render: options?.render ?? true }),
    },
  );
}

/**
 * Full-replace the text-element list on a variant (T6).
 * When render=true (default), triggers an async re-render via the fast-reburn path.
 * PUT /plan-items/{planItemId}/variants/{variantId}/text-elements
 * Returns { ok: boolean } from the backend (T4 route).
 */
export function putTextElements(
  planItemId: string,
  variantId: string,
  elements: TextElement[],
  render = true,
): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(
    `/plan-items/${planItemId}/variants/${variantId}/text-elements`,
    {
      method: "PUT",
      body: JSON.stringify({ elements, render }),
    },
  );
}

// ── Sound-effect placement: upload + apply ─────────────────────────────────────

export interface SfxUploadUrl {
  filename: string;
  upload_url: string;
  gcs_path: string;
}

/**
 * Request signed PUT URLs for user-uploaded SFX assets.
 * Assets land under users/{uid}/plan/{itemId}/sfx/ (persistent, not 24h-swept).
 */
export async function requestSfxUploadUrls(
  itemId: string,
  files: { filename: string; content_type: string; file_size_bytes: number }[],
): Promise<SfxUploadUrl[]> {
  const res = await request<{ urls: SfxUploadUrl[] }>(
    `/plan-items/${itemId}/sfx-upload-urls`,
    {
      method: "POST",
      body: JSON.stringify({ files }),
    },
  );
  return res.urls;
}

/**
 * Full-replace the sound-effect placement list on a variant.
 * Persists placements to DB without triggering a render.
 * Returns the updated PlanItem.
 */
export function setVariantSoundEffects(
  itemId: string,
  variantId: string,
  placements: SoundEffectPlacement[],
): Promise<PlanItem> {
  return request<PlanItem>(
    `/plan-items/${itemId}/variants/${variantId}/sound-effects`,
    {
      method: "PUT",
      body: JSON.stringify({ placements }),
    },
  );
}

/**
 * Trigger the FFmpeg SFX burn-in pass for a variant that has persisted placements.
 * Called by the Download button when sound_effects are set and unrendered.
 * Returns the updated PlanItem immediately (render runs async).
 */
export function renderVariantSfx(
  itemId: string,
  variantId: string,
): Promise<PlanItem> {
  return request<PlanItem>(
    `/plan-items/${itemId}/variants/${variantId}/render-sfx`,
    { method: "POST" },
  );
}

/**
 * Return a short-lived signed GET URL for a user-uploaded SFX file.
 * Only allows paths under users/{user_id}/ — server rejects any other prefix.
 */
export async function getSfxAudioUrl(itemId: string, gcsPath: string): Promise<string> {
  const res = await request<{ url: string }>(
    `/plan-items/${itemId}/sfx-audio-url?gcs_path=${encodeURIComponent(gcsPath)}`,
  );
  return res.url;
}

// ── Activation seed: upload recent clips → auto-match → instant first video (T8) ──

/** Signed PUT URLs for the seed batch (lands under users/{uid}/plan/{planId}/seed/). */
export async function requestSeedUploadUrls(
  planId: string,
  files: { filename: string; content_type: string; file_size_bytes: number }[],
): Promise<UploadUrl[]> {
  const res = await request<{ urls: UploadUrl[] }>(`/content-plans/${planId}/seed-upload-urls`, {
    method: "POST",
    body: JSON.stringify({ files }),
  });
  return res.urls;
}

/** Record the uploaded seed batch on the plan (flips activation_status to "seeding"). */
export function attachSeedClips(planId: string, clipGcsPaths: string[]): Promise<ContentPlan> {
  return request<ContentPlan>(`/content-plans/${planId}/seed-clips`, {
    method: "POST",
    body: JSON.stringify({ clip_gcs_paths: clipGcsPaths }),
  });
}

/** Kick off clip→item matching + auto-generation for the uploaded seed batch. */
export function activatePlan(planId: string): Promise<ContentPlan> {
  return request<ContentPlan>(`/content-plans/${planId}/activate`, { method: "POST" });
}

export interface ActivationState {
  activation_status: ActivationStatus;
  seed_clip_count: number;
  generating_item_ids: string[];
  ready_item_ids: string[];
  activation_phase?: string | null;
  activation_started_at?: string | null;
  expected_phase_durations?: Record<string, number> | null;
}

/** Poll target while activation runs. */
export function getActivation(planId: string): Promise<ActivationState> {
  return request<ActivationState>(`/content-plans/${planId}/activation`);
}

// ── Plan-item job status (for ProgressTheater on the item page) ────────────────

export interface PlanItemJobStatus {
  status: string | null;
  variants: PlanItemVariant[];
  failure_reason?: string | null;
  speech_cleanup_failure_reason?: string | null;
  current_phase?: string | null;
  phase_log?: Array<{ name: string; ts: string; elapsed_ms?: number }> | null;
  started_at?: string | null;
  finished_at?: string | null;
  expected_phase_durations?: Record<string, number> | null;
  created_at?: string | null;
  /** Style-downgrade explanation persisted by the orchestrator when the declared
   *  edit_format fell back to montage (e.g. self-narration found no speech).
   *  Null when the declared format rendered. Drives the item-page banner.
   *  Mirrors ArchetypeFallbackOut in routes/generative_jobs.py — the single TS
   *  definition lives in plan-generate-gate.ts. */
  archetype_fallback?: ArchetypeFallback | null;
  /** True while the render attempt died silently and is awaiting automatic
   *  retry (stale worker heartbeat). Optional — older API builds omit it. */
  retrying?: boolean;
  /** Nova AI steps activity feed (PR1 `nova_steps` projection). Optional —
   *  gated server-side by NOVA_STEPS_FEED_ENABLED; older API builds and the
   *  flag-off case both omit it. NovaActivityFeed falls back to
   *  PhaseChipRow whenever this is absent/empty. */
  steps?: NovaStep[] | null;
}

export async function getPlanItemJobStatus(jobId: string): Promise<PlanItemJobStatus> {
  const res = await request<PlanItemJobStatus>(`/generative-jobs/${jobId}/status`);
  return res;
}

export async function getPlanItemJobStatusFresh(jobId: string): Promise<PlanItemJobStatus> {
  const res = await request<PlanItemJobStatus>(`/generative-jobs/${jobId}/status`, {
    cache: "no-store",
  });
  return res;
}

// ── Creator Agent M1: Per-user style ─────────────────────────────────────────
// Gated behind USER_STYLE_ENABLED on the backend (returns 404 when disabled).
// Frontend: render style surfaces only when the style API returns non-404.

export interface StyleKnobs {
  font_family?: string | null;
  text_size_px?: number | null;
  position?: string | null;
  position_x_frac?: number | null;
  position_y_frac?: number | null;
  text_anchor?: string | null;
  text_color?: string | null;
  highlight_color?: string | null;
  stroke_width?: number | null;
  cycle_fonts?: boolean | null;
}

export interface UserStyle {
  style_set_id?: string;
  knobs?: StyleKnobs;
  footage_type_bias?: string[];
  preferred_edit_format_mix?: Record<string, number>;
  instruction_level?: "full" | "light" | "none";
  status?: "deriving" | "ready" | "edited" | "failed";
  derived_from?: Record<string, unknown>;
  style_version?: string;
  rationale?: string;
}

export interface StyleSetPreview {
  id?: string;
  label?: string | null;
  tags?: string[] | null;
  font_family?: string | null;
  css_family?: string | null;
  font_file?: string | null;
  font_weight?: string | null;
  text_color?: string | null;
  highlight_color?: string | null;
  effect?: string | null;
}

export interface FontPreview {
  font_family: string;
  display_name: string;
  css_family: string;
}

export interface StyleProvenance {
  videos_seen: number;
  videos_total: number;
  observed_at?: string | null;
  has_on_screen_text: boolean;
  font_feel?: string | null;
  text_color_hex?: string | null;
  highlight_color_hex?: string | null;
  position?: string | null;
  size_class?: string | null;
  mean_confidence?: number | null;
  confidence_per_field?: Record<string, number>;
}

export interface StyleResponse {
  style: UserStyle | null;
  status: "deriving" | "ready" | "edited" | "failed" | "absent";
  style_set_preview?: StyleSetPreview | null;
  font_preview?: FontPreview | null;
  provenance?: StyleProvenance | null;
}

export interface StyleEdit {
  style_set_id?: string;
  knobs?: Partial<StyleKnobs>;
  footage_type_bias?: string[];
  preferred_edit_format_mix?: Record<string, number>;
  instruction_level?: "full" | "light" | "none";
}

/** GET /personas/style — returns 404 when USER_STYLE_ENABLED=false. */
export function getStyle(): Promise<StyleResponse> {
  return request<StyleResponse>("/personas/style");
}

/** PATCH /personas/style — partial edit; sets status="edited". */
export function patchStyle(edit: StyleEdit): Promise<StyleResponse> {
  return request<StyleResponse>("/personas/style", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(edit),
  });
}

/** POST /personas/style/rederive — re-derives from current persona (overwrites even edited). */
export function rederiveStyle(): Promise<{ queued: boolean; persona_id: string }> {
  return request<{ queued: boolean; persona_id: string }>("/personas/style/rederive", {
    method: "POST",
  });
}

// ── Creator Agent M4: conformance verdict at clip-attach time ─────────────────
// Best-effort, display-only. Never blocks the Generate button.
// Arrives async after attach — poll item until conformance is non-null or timeout.

/** ConformanceFeedbackAgent verdict, stored on plan_items.conformance (nullable). */
export interface ConformanceVerdict {
  verdict: "on_track" | "minor_drift" | "off_brief";
  confidence: number;
  summary: string;
  mismatches: string[];
  suggestions: string[];
}

// M4 fields appended to PlanItem via interface declaration merging (append-only rule).
// instruction_level: "full"|"light"|"none" — drives single-file vs bulk upload split.
// conformance: present after ConformanceFeedbackAgent runs (async, best-effort).
export interface PlanItem {
  instruction_level?: "full" | "light" | "none";
  conformance?: ConformanceVerdict | null;
}

// ── Style Agent conversational interface (Creator Agent M2) ──────────────────
// Append-only — do not edit any existing code above this section.

export interface StyleAgentTurnResponse {
  reply: string;
  suggestions: string[];
  applied: boolean;
  intent: string;
  persona_status: string;
}

/**
 * POST /personas/agent/start — returns a personalised greeting + opening suggestion chips.
 * Returns 404 when STYLE_AGENT_ENABLED=false (the page hides the entry when absent).
 */
export function styleAgentStart(): Promise<StyleAgentTurnResponse> {
  return request<StyleAgentTurnResponse>("/personas/agent/start", { method: "POST" });
}

/**
 * POST /personas/agent/turn — submit a style utterance; returns reply + applied flag.
 * priorTurns is the full conversation history so far (stateless single-shot agent).
 */
export function styleAgentTurn(
  answer: string,
  priorTurns?: unknown[],
): Promise<StyleAgentTurnResponse> {
  return request<StyleAgentTurnResponse>("/personas/agent/turn", {
    method: "POST",
    body: JSON.stringify({ answer, prior_turns: priorTurns ?? [] }),
  });
}

// ── Plan dogfood fixes (2026-06-11): clip notes, conformance trust actions, ────
// Ask Kria advisor, footage pool. Append-only — do not edit code above.

// Clip notes + provisional machine matches (interface merging, append-only rule).
export interface ClipAssignment {
  /** Creator context about the clip ("famous vegan restaurant"). "" = none. */
  user_note?: string;
  /** True = the footage-pool matcher placed this clip (provisional chip). */
  machine_matched?: boolean;
  /** Stable server-owned identity used by guided-edit proposals. */
  media_id?: string | null;
}

export type EditProposalStatus =
  | "briefing"
  | "analyzing"
  | "drafting"
  | "draft"
  | "approved"
  | "stale"
  | "failed";
export type EditProposalDirection = "guided_story" | "fast_montage" | "text_explainer";
export type EditProposalPace = "relaxed" | "balanced" | "fast";
export type EditProposalTextDensity = "minimal" | "moderate" | "dense";
export type EditProposalAudioRole = "music_led" | "original_audio" | "voiceover" | "mixed";

export interface ProposalDirectionHypothesis {
  direction: EditProposalDirection;
  pace: EditProposalPace;
  duration_s: number;
  text_density: EditProposalTextDensity;
  audio_role: EditProposalAudioRole;
  rationale: string;
  buildability_warnings: string[];
}

export interface ProposalGuidance {
  state: "awaiting_direction_confirmation" | "confirmed";
  provenance: "creator_explicit" | "ai_inferred" | "creator_confirmed";
  hypothesis: ProposalDirectionHypothesis;
  fingerprint: string;
}

export interface EditProposalMediaRef {
  lane: "clip" | "asset";
  media_id: string;
  gcs_path: string;
  generation: string;
  kind: "image" | "video";
  source_filename: string;
  duration_s?: number | null;
  aspect?: number | null;
  content_hash?: string | null;
  user_context: string;
  analysis: Record<string, unknown>;
  /** Signed at response time; never persisted or sent as media identity. */
  preview_url?: string | null;
}

export interface EditProposalBeat {
  beat_id: string;
  topic: string;
  thought: string;
  thought_source: "ai_draft" | "user";
  media_ids: string[];
  layout: "fullscreen" | "supporting_card";
  duration_s: number;
}

export interface EditProposalFastCut {
  cut_id: string;
  media_id: string;
  source_start_s: number;
  source_end_s: number;
  output_duration_s: number;
  role: "hook" | "build" | "payoff";
  transition: "none";
  beat_align: boolean;
}

export interface EditProposalSnapshot {
  direction: EditProposalDirection;
  goal: string;
  pace: EditProposalPace;
  duration_s: number;
  title: string;
  media: EditProposalMediaRef[];
  story_beats: EditProposalBeat[];
  fast_cuts?: EditProposalFastCut[] | null;
}

export interface EditProposal {
  schema_version: 1;
  proposal_version: number;
  generation_attempt_id: string;
  media_digest: string | null;
  status: EditProposalStatus;
  guidance?: ProposalGuidance | null;
  brief: {
    direction: EditProposalDirection;
    goal: string;
    pace: EditProposalPace;
    duration_s: number;
  };
  conversation: Array<{
    role: "user" | "agent";
    phase?: "briefing" | "review";
    content: string;
    suggestions: string[];
  }>;
  brief_ready: boolean;
  conversation_in_progress?: boolean;
  conversation_retry_required?: boolean;
  draft: EditProposalSnapshot | null;
  last_approved: {
    proposal_version: number;
    media_digest: string;
    approved_at: string;
    snapshot: EditProposalSnapshot;
  } | null;
  failure: { code: string; message: string; retryable: boolean } | null;
  render_failure?: ProposalRenderFailure | null;
}

/** An APPROVED plan that the strict renderer could not execute. Scoped to the
 * approved proposal_version that failed, so a new approval clears it. */
export interface ProposalRenderFailure {
  proposal_version: number;
  code: string;
  message: string;
  attempts: number;
  failed_at: string;
}

export interface PlanItem {
  edit_proposal?: EditProposal | null;
  guided_edit_available?: boolean;
  guided_edit_conversation_available?: boolean;
  /** GUIDED_AUTO_DESIGN_ENABLED (server flag). Absent/false on an old API —
   * gate the AI-designs-by-default Generate button behavior on
   * `item.guided_edit_auto_design ?? false` so a new web build against an
   * old API deploy keeps today's strict-gate behavior (deploy-skew safe). */
  guided_edit_auto_design?: boolean | null;
}

// Conformance trust fields (echo-back evidence + dismissal/contest state).
export interface ConformanceVerdict {
  /** The theme the judge actually evaluated — rendered as READ AGAINST evidence. */
  evaluated_theme?: string;
  /** Contested + sub-0.8 confidence → never rendered. */
  suppressed?: boolean;
  /** User clicked "Hide this read" — never rendered for this footage. */
  dismissed?: boolean;
  /** User contested once on this footage. */
  contested?: boolean;
  clip_gcs_path?: string;
}

// Mode-aware header copy: how this persona sources content.
export interface PlanItem {
  content_mode?: "existing_footage" | "create_new" | "mixed";
}

// Direction-fork persona fields (interface merging, append-only rule). All
// optional: personas generated before 2026-06-11 won't have them.
export interface PersonaContent {
  goal?: string;
  content_mode?: "existing_footage" | "create_new" | "mixed" | null;
  /** "based in Istanbul; the Argentina footage is a past trip" — the planner's
   * location/temporal anchor, surfaced as the "Planning around" trust line. */
  current_situation?: string;
}

// Onboarding state fields on PersonaQuestionnaire (interface merging, append-only rule).
// These track where the user is in the edits-first footage funnel.
export interface PersonaQuestionnaire {
  // edits-first funnel: chosen path ("existing_footage" | "create_new" | "mixed")
  content_mode?: "existing_footage" | "create_new" | "mixed";
  // optional context the user typed in EditContextStep
  onboarding_topic?: string;
  onboarding_intent?: string;
  // generative job kicked off from the onboarding upload step
  onboarding_edit_job_id?: string;
  // clip GCS paths used for that job
  onboarding_clip_paths?: string[];
  // true once the user has seen and interacted with the payoff screen
  onboarding_payoff_done?: boolean;
}

/** Footage pool lifecycle on the plan. */
export type PoolStatus = "none" | "matching" | "matched" | "matched_empty" | "match_failed";

export interface ContentPlan {
  pool_status?: PoolStatus;
  pool_clip_count?: number;
  pool_matched_count?: number;
}

/** Set/clear the creator's context note on one attached clip (re-runs the brief read). */
export function setClipNote(
  itemId: string,
  gcsPath: string,
  userNote: string,
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/clips/note`, {
    method: "PATCH",
    body: JSON.stringify({ gcs_path: gcsPath, user_note: userNote }),
  });
}

/** "Hide this read" — persist dismissal of the current conformance verdict. */
export function dismissConformance(itemId: string): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/conformance/dismiss`, { method: "POST" });
}

/** "Looks wrong? Tell Kria" — mark the verdict contested (suppresses low-confidence re-reads). */
export function contestConformance(itemId: string): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/conformance/contest`, { method: "POST" });
}

export interface AdvisorTurnResponse {
  reply: string;
  suggestions: string[];
  /** Non-empty = the agent proposes re-reading a clip with this distilled note. */
  suggested_note: string;
}

/**
 * POST /plan-items/{id}/agent/turn — one "Ask Kria" advisor turn for this item.
 * Stateless: priorTurns carries the whole conversation. 404 when the
 * PLAN_ITEM_ADVISOR_ENABLED kill switch is off (the page hides the entry).
 */
export function planItemAdvisorTurn(
  itemId: string,
  answer: string,
  priorTurns?: { role: "agent" | "user"; content: string }[],
): Promise<AdvisorTurnResponse> {
  return request<AdvisorTurnResponse>(`/plan-items/${itemId}/agent/turn`, {
    method: "POST",
    body: JSON.stringify({ answer, prior_turns: priorTurns ?? [] }),
  });
}

/** Signed PUT URLs for the footage pool (users/{uid}/plan-pool/{plan_id}/). */
export function requestPoolUploadUrls(
  planId: string,
  files: { filename: string; content_type: string; file_size_bytes: number }[],
): Promise<{ upload_url: string; gcs_path: string }[]> {
  return request<{ urls: { upload_url: string; gcs_path: string }[] }>(
    `/content-plans/${planId}/pool/upload-urls`,
    { method: "POST", body: JSON.stringify({ files }) },
  ).then((r) => r.urls);
}

/** Add uploaded clips to the pool and start matching them across pending items. */
export function attachPoolClips(planId: string, clipGcsPaths: string[]): Promise<ContentPlan> {
  return request<ContentPlan>(`/content-plans/${planId}/pool/clips`, {
    method: "POST",
    body: JSON.stringify({ clip_gcs_paths: clipGcsPaths }),
  });
}

/** "Match again" — re-run pool matching (e.g. after new items freed up). */
export function rematchPoolClips(planId: string): Promise<ContentPlan> {
  return request<ContentPlan>(`/content-plans/${planId}/pool/match`, { method: "POST" });
}

// ── Visuals pool (overlay auto-placement PR0) ────────────────────────────────
//
// Per-item asset pool that feeds AI overlay auto-placement (plans/005).
// All routes 404 when the backend OVERLAY_AUTOPLACE_ENABLED flag is off — the
// frontend twin is NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED (dual-flag trap:
// keep Fly + Vercel in sync; callers must surface the 404, never swallow it).

export interface PoolAsset {
  id: string;
  kind: "image" | "video";
  status: "uploaded" | "queued" | "analyzing" | "ready" | "failed";
  media_status?: "pending" | "ready" | "unreadable" | "failed";
  preview_status?: "not_needed" | "pending" | "ready" | "failed";
  error_code?: string | null;
  error_detail?: string | null;
  retryable?: boolean;
  source_filename: string | null;
  duration_s: number | null;
  aspect: number | null;
  /** Pixel dims (plan 009 E1) — null on legacy assets until the backfill
   *  re-analyzes them. Feed the fullscreen low-res warning; never faked. */
  width?: number | null;
  height?: number | null;
  subject: string | null;
  /** Creator-authored context, kept separate from Nova's generated analysis. */
  user_context: string;
  /** Nova-generated description fields, source-labeled for the UI. */
  nova_description?: string | null;
  nova_on_screen_text?: string | null;
  /** Brand/mascot identities from analysis (ANALYSIS_VERSION 5, brand-aware
   *  matching) — null/absent on pre-v5 analyses, [] analyzed with none found. */
  brands?: string[] | null;
  display_url: string | null;
  /** Signed browser-safe preview URL (pool asset preview pipeline). Populated
   *  for videos (poster frame) when a preview was generated; null/absent
   *  otherwise — images fold their preview into display_url directly. */
  preview_url?: string | null;
  deduped: boolean;
  /** Object key under users/{uid}/plan/{itemId}/pool/ — already inside
   *  attach_clips' allowed prefix, so "Use in edit" can promote the asset to a
   *  clip via the existing attach flow (no copy, no new endpoint). */
  gcs_path: string;
  /** Provenance for AI-extracted source frames; absent on normal uploads. */
  source_type?: string | null;
  source_clip_index?: number | null;
  source_timestamp_s?: number | null;
}

export interface PoolAssetUploadTarget {
  reservation_id: string;
  client_upload_id: string;
  upload_url: string;
  gcs_path: string;
  expires_at: string;
  upload_headers: Record<string, string>;
}

/** Creator Blocks only decode images whose server analysis established a safe bound. */
export function isBoundedCreatorImageAsset(asset: PoolAsset): boolean {
  return (
    asset.kind === "image" &&
    asset.status === "ready" &&
    typeof asset.width === "number" &&
    typeof asset.height === "number" &&
    asset.width > 0 &&
    asset.height > 0 &&
    asset.width <= 12_000 &&
    asset.height <= 12_000 &&
    asset.width * asset.height <= 25_000_000
  );
}

/** Signed PUT URLs for pool assets (users/{uid}/plan/{itemId}/pool/, persistent). */
export async function requestPoolAssetUploadUrls(
  itemId: string,
  files: {
    filename: string;
    content_type: string;
    file_size_bytes: number;
    client_upload_id: string;
  }[],
  correlationId: string,
): Promise<PoolAssetUploadTarget[]> {
  const res = await request<{ urls: PoolAssetUploadTarget[] }>(
    `/plan-items/${itemId}/assets/upload-urls`,
    {
      method: "POST",
      headers: { "X-Correlation-Id": correlationId },
      body: JSON.stringify({ files }),
    },
  );
  return res.urls;
}

/**
 * Hex SHA-256 of a file's bytes — the pool dedupe key. Mirrors the backend's
 * `hashlib.sha256(bytes).hexdigest()` in the multipart path (routes/plan_items.py
 * upload_pool_asset) so `registerPoolAsset` dedupes identical uploads whether they
 * arrived via the presigned direct-PUT path or the legacy proxy. Returns null when
 * SubtleCrypto is unavailable (non-secure context) — register then skips dedupe,
 * which is a safe degradation (an extra analysis, never a data-loss).
 */
export async function sha256HexOfFile(file: File): Promise<string | null> {
  // The API now dedupes from immutable GCS metadata. Keep this helper for
  // legacy clients, but do not make a 512 MB upload wait on a full-memory hash.
  if (file.size > 25 * 1024 * 1024) return null;
  try {
    if (typeof crypto === "undefined" || !crypto.subtle) return null;
    const buf = await file.arrayBuffer();
    const digest = await crypto.subtle.digest("SHA-256", buf);
    return Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
  } catch {
    return null;
  }
}

/**
 * Register an uploaded pool asset. `deduped=true` on the response means the
 * bytes already existed in this pool — the existing asset is returned as-is.
 */
export function registerPoolAsset(
  itemId: string,
  body: {
    gcs_path: string;
    reservation_id?: string | null;
    content_type: string;
    /** Legacy compatibility only; the server dedupes from GCS metadata. */
    content_hash?: string | null;
    source_filename: string | null;
    user_context?: string | null;
  },
  correlationId?: string,
): Promise<PoolAsset> {
  return request<PoolAsset>(`/plan-items/${itemId}/assets`, {
    method: "POST",
    headers: correlationId ? { "X-Correlation-Id": correlationId } : undefined,
    body: JSON.stringify(body),
  });
}

/**
 * One-shot pool upload through the API proxy (browser → Next → FastAPI → GCS).
 * Sidesteps bucket CORS entirely (a direct browser PUT to storage.googleapis.com
 * fails for origins the bucket doesn't list — e.g. any localhost). The server
 * computes the dedupe hash; `deduped=true` means the bytes already existed.
 *
 * NOT the primary pool-upload path anymore (R1 / review C9+C14): this multipart
 * body buffers through the Next api-proxy and hits Vercel's ~4.5MB serverless
 * request-body cap, so screen recordings fail in prod. AssetPool now uploads via
 * requestPoolAssetUploadUrls → uploadToGcs (direct PUT, relay fallback) →
 * registerPoolAsset. Kept for any caller that still wants the one-shot proxy.
 */
export async function uploadPoolAsset(itemId: string, file: File): Promise<PoolAsset> {
  const form = new FormData();
  form.append("file", file, file.name);
  // No Content-Type header — the browser must set the multipart boundary.
  const res = await fetch(`${PLAN_BASE}/plan-items/${itemId}/assets/upload`, {
    method: "POST",
    body: form,
  });
  if (res.status === 401) throw new NotAuthenticatedError();
  if (!res.ok) {
    let detail = `Upload failed (${res.status})`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // non-JSON error body; keep the generic message
    }
    throw new Error(detail);
  }
  return (await res.json()) as PoolAsset;
}

/** List the item's pool assets + the per-item cap. */
export interface PoolReservationCapacity {
  reservation_id: string;
  release_at: string | null;
}

export interface PoolAssetsResponse {
  assets: PoolAsset[];
  max_assets: number;
  occupied_assets?: number;
  active_reservations?: PoolReservationCapacity[];
}

export function listPoolAssets(
  itemId: string,
): Promise<PoolAssetsResponse> {
  return request<PoolAssetsResponse>(`/plan-items/${itemId}/assets`);
}

/** Remove an asset from the pool. */
export function deletePoolAsset(itemId: string, assetId: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/plan-items/${itemId}/assets/${assetId}`, {
    method: "DELETE",
  });
}

/** Idempotently retry analysis for a failed or legacy pool asset. */
export function reanalyzePoolAsset(itemId: string, assetId: string): Promise<PoolAsset> {
  return request<PoolAsset>(`/plan-items/${itemId}/assets/${assetId}/reanalyze`, {
    method: "POST",
  });
}

/** Set or clear creator context for a pool visual; clears pending suggestions server-side. */
export function updatePoolAssetContext(
  itemId: string,
  assetId: string,
  userContext: string | null,
): Promise<PoolAsset> {
  return request<PoolAsset>(`/plan-items/${itemId}/assets/${assetId}/context`, {
    method: "PATCH",
    body: JSON.stringify({ user_context: userContext }),
  });
}

// ── Overlay auto-placement suggestions (plans/005 PR2) ──────────────────────
//
// Per-variant AI suggestion flow: suggest-overlays kicks off the matcher,
// overlay-suggestions is polled while it runs, apply copies the kept envelopes
// into the variant's real media_overlays/sound_effects through the validated
// dispatch path (the variant flips to render_status="rendering"), dismiss
// clears the pending set. All routes 404 when OVERLAY_AUTOPLACE_ENABLED is off.

export type OverlaySuggestionStatus = "matching" | "ready" | "zero" | "failed";

/**
 * One AI-suggested placement — an ENVELOPE (plans/005 decision 5A) that embeds
 * the existing MediaOverlay + SoundEffectPlacement models verbatim. Accept =
 * unwrap + copy through the existing dispatch; no parallel field copies.
 */
export interface OverlaySuggestion {
  id: string;
  /** Pool asset this suggestion places (thumbnail via listPoolAssets). */
  asset_id: string;
  /** Language carries confidence (10A): "likely" rows ship hedged reason copy. */
  confidence_tier: "confident" | "likely";
  /** One-line reason grounded in the transcript. */
  reason: string;
  transcript_anchor: string;
  overlay: MediaOverlay;
  sfx: SoundEffectPlacement | null;
}

export interface OverlaySuggestionsResponse {
  /** null = never matched for this variant (no pending suggestion set). */
  status: OverlaySuggestionStatus | null;
  suggestions: OverlaySuggestion[];
  /** Zero/partial-match asset wishlist lines, shown verbatim. */
  wishlist: string[];
  /** True when a transcript/duration change just cleared pending suggestions. */
  stale_cleared: boolean;
}

/** Kick off the overlay matcher. 400 with detail when no analyzed assets. */
export function suggestVariantOverlays(
  itemId: string,
  variantId: string,
): Promise<{ status: "matching" }> {
  return request<{ status: "matching" }>(
    `/plan-items/${itemId}/variants/${variantId}/suggest-overlays`,
    { method: "POST" },
  );
}

/** Read the current suggestion set (polled every 2.5s while status="matching"). */
export function getOverlaySuggestions(
  itemId: string,
  variantId: string,
): Promise<OverlaySuggestionsResponse> {
  return request<OverlaySuggestionsResponse>(
    `/plan-items/${itemId}/variants/${variantId}/overlay-suggestions`,
  );
}

/**
 * Apply the kept suggestions (send ONLY the staged ones, with any user edits —
 * e.g. sfx stripped to null). Returns the updated plan item; the variant flips
 * to render_status="rendering" while the burn runs in the background.
 */
export function applyOverlaySuggestions(
  itemId: string,
  variantId: string,
  suggestions: OverlaySuggestion[],
): Promise<PlanItem> {
  return request<PlanItem>(
    `/plan-items/${itemId}/variants/${variantId}/overlay-suggestions/apply`,
    { method: "POST", body: JSON.stringify({ suggestions }) },
  );
}

/** Dismiss the pending suggestion set without applying anything. */
export function dismissOverlaySuggestions(
  itemId: string,
  variantId: string,
): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(
    `/plan-items/${itemId}/variants/${variantId}/overlay-suggestions/dismiss`,
    { method: "POST" },
  );
}

// ── Edits-first onboarding fork (append-only rule) ────────────────────────────

/**
 * POST /personas/onboarding-fork — persist the fork choice and optional
 * context/footage state on the persona's questionnaire. Called at each
 * step of the footage funnel so the server is the source of truth, and
 * the user can resume if they close the tab.
 */
export function recordOnboardingFork(data: {
  content_mode: string;
  topic?: string;
  intent?: string;
  onboarding_clip_paths?: string[];
  onboarding_edit_job_id?: string;
  onboarding_payoff_done?: boolean;
}): Promise<{ persona_id: string; persona_status: string }> {
  return request<{ persona_id: string; persona_status: string }>(
    "/personas/onboarding-fork",
    {
      method: "POST",
      body: JSON.stringify(data),
    },
  );
}
