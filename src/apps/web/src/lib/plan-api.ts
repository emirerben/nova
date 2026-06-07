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

const PLAN_BASE = "/api/plan";

export class NotAuthenticatedError extends Error {
  constructor() {
    super("Not authenticated");
    this.name = "NotAuthenticatedError";
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
  // The single most revealing thing the creator said in the chat interview —
  // shown verbatim as "You said: '...'" on the persona reveal. Empty for
  // personas generated from the old flat-field questionnaire.
  signature_quote?: string;
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

export interface PersonaResponse {
  id: string;
  persona_status: PersonaStatus;
  questionnaire: PersonaQuestionnaire | null;
  persona: PersonaContent | null;
  error_detail: string | null;
  tiktok_profile?: TikTokProfile | null;
  generation_started_at?: string | null;
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
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // non-JSON error body; keep the generic message
    }
    throw new Error(detail);
  }
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

/** Derived server-side from the linked Job.status — never a stored column. */
export type PlanItemStatus =
  | "idea"
  | "awaiting_clips"
  | "generating"
  | "ready"
  | "failed"
  | "rerolling";

/** One concrete shot in a filming guide. */
export interface FilmingShot {
  what: string;
  how: string;
  duration_s: number;
}

export interface PlanItem {
  id: string;
  day_index: number;
  theme: string;
  idea: string;
  filming_suggestion: string | null;
  // The AI's "why this works" — shown read-only. null for items made before
  // this field shipped (the UI hides the line).
  rationale: string | null;
  // Structured shot list generated at plan time. Empty for items made before
  // this field shipped; the UI falls back to filming_suggestion in that case.
  filming_guide: FilmingShot[];
  clip_gcs_paths: string[];
  status: PlanItemStatus;
  current_job_id: string | null;
  user_edited: boolean;
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

export function updatePlanItem(
  id: string,
  edit: { theme?: string; idea?: string; filming_suggestion?: string },
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${id}`, {
    method: "PATCH",
    body: JSON.stringify(edit),
  });
}

export function rerollPlanItem(id: string): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${id}/reroll`, { method: "POST" });
}

// ── Themed uploads + per-item generation (Phase 5) ────────────────────────────

export function getPlanItem(id: string): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${id}`);
}

interface UploadUrl {
  upload_url: string;
  gcs_path: string;
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

/** PUT a file straight to GCS (direct, not through the proxy — avoids buffering bytes). */
export async function uploadToGcs(uploadUrl: string, file: File): Promise<void> {
  const res = await fetch(uploadUrl, {
    method: "PUT",
    headers: { "Content-Type": file.type },
    body: file,
  });
  if (!res.ok) throw new Error(`Upload failed (${res.status})`);
}

export function attachClips(itemId: string, clipGcsPaths: string[]): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/clips`, {
    method: "POST",
    body: JSON.stringify({ clip_gcs_paths: clipGcsPaths }),
  });
}

export function generatePlanItem(itemId: string): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/generate`, { method: "POST" });
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
export interface PlanItemVariant {
  variant_id: string;
  output_url: string | null;
  render_status: string | null;
  // Edit controls: swap-song is hidden when music_track_id is null (the
  // original-audio variant has no song), and the style picker reflects style_set_id.
  text_mode?: "lyrics" | "agent_text" | "none";
  music_track_id?: string | null;
  track_title?: string | null;
  style_set_id?: string | null;
  // Agent-decided (or user-pinned) intro size — drives the ±size stepper.
  intro_text_size_px?: number | null;
  intro_size_source?: "computed" | "user" | null;
  render_started_at?: string | null;
  render_finished_at?: string | null;
  error_class?: string | null;
}

export async function getPlanItemVariants(jobId: string): Promise<PlanItemVariant[]> {
  const res = await request<{ variants: PlanItemVariant[] }>(`/generative-jobs/${jobId}/status`);
  return res.variants ?? [];
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
  current_phase?: string | null;
  phase_log?: Array<{ name: string; ts: string; elapsed_ms?: number }> | null;
  started_at?: string | null;
  finished_at?: string | null;
  expected_phase_durations?: Record<string, number> | null;
  created_at?: string | null;
}

export async function getPlanItemJobStatus(jobId: string): Promise<PlanItemJobStatus> {
  const res = await request<PlanItemJobStatus>(`/generative-jobs/${jobId}/status`);
  return res;
}

// ── Creator Agent M1: Per-user style ─────────────────────────────────────────
// Gated behind USER_STYLE_ENABLED on the backend (returns 404 when disabled).
// Frontend: render StyleCard only when the style API returns non-404.

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

export interface StyleResponse {
  style: UserStyle | null;
  status: "deriving" | "ready" | "edited" | "failed" | "absent";
  style_set_preview?: StyleSetPreview | null;
  font_preview?: FontPreview | null;
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
