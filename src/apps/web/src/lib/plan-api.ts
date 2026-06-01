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
  sample_topics: string[];
  // The AI's "why this lane" — shown read-only in the dashboard. Optional:
  // personas generated before this field shipped won't have it.
  rationale?: string;
}

export type PersonaStatus = "generating" | "ready" | "failed" | "edited";

export interface PersonaResponse {
  id: string;
  persona_status: PersonaStatus;
  questionnaire: PersonaQuestionnaire | null;
  persona: PersonaContent | null;
  error_detail: string | null;
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
  | "failed";

export interface PlanItem {
  id: string;
  day_index: number;
  theme: string;
  idea: string;
  filming_suggestion: string | null;
  // The AI's "why this works" — shown read-only. null for items made before
  // this field shipped (the UI hides the line).
  rationale: string | null;
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
}

/** Poll target while activation runs. */
export function getActivation(planId: string): Promise<ActivationState> {
  return request<ActivationState>(`/content-plans/${planId}/activation`);
}
