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

import type { EditVariantPayload } from "@/lib/generative-api";
import type { ArchetypeFallback } from "@/lib/plan-generate-gate";

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
  user_edited: boolean;
  /** Render archetype assigned at plan-gen time (e.g. "montage", "talking_head"). Null for legacy items. */
  edit_format?: string | null;
  /** Narrated-walkthrough voiceover GCS key (0056+). Null = no voiceover recorded yet. */
  voiceover_gcs_path?: string | null;
  /**
   * Landscape-clip fit preference.
   * "fit"  = letterbox (full-width, black bars top & bottom, never enlarged) — default.
   * "fill" = center-crop to fill the 9:16 frame (old behavior).
   * Only affects clips where width > height; portrait/square always crop.
   */
  landscape_fit: "fit" | "fill";
  /** Original-audio bed level for narrated. 0 = voice only, 1 = loudest. Null = Nova's default. */
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

export function addIdeasToPlan(planId: string): Promise<ContentPlan> {
  return request<ContentPlan>(`/content-plans/${planId}/add-ideas`, { method: "POST" });
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
export function expandIdea(itemId: string): Promise<IdeaExpandProposal> {
  return request<IdeaExpandProposal>(`/plan-items/${itemId}/expand`, { method: "POST" });
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
    filming_guide?: FilmingShot[];
    landscape_fit?: "fit" | "fill";
    /** Per-item content_mode override (montage plan-vs-have toggle, 0058+). */
    content_mode?: "existing_footage" | "create_new" | "mixed";
  },
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

export function getPlanItemFresh(id: string): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${id}`, { cache: "no-store" });
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
  try {
    const res = await fetch(uploadUrl, {
      method: "PUT",
      headers: { "Content-Type": file.type },
      body: file,
    });
    if (!res.ok) throw new Error(`Upload failed (${res.status})`);
  } catch (err) {
    // "Failed to fetch" (TypeError) = the bucket's CORS config doesn't list this
    // origin (any localhost) — the request never left the browser. Relay the
    // SAME signed URL through the API, where CORS doesn't apply.
    if (err instanceof TypeError) {
      await relaySignedUpload(uploadUrl, file);
      return;
    }
    throw err;
  }
}

/** Server-side PUT of `file` to `signedUrl` via the API relay (bucket-CORS bypass). */
async function relaySignedUpload(signedUrl: string, file: File): Promise<void> {
  const form = new FormData();
  form.append("file", file, file.name);
  form.append("signed_url", signedUrl);
  form.append("content_type", file.type || "application/octet-stream");
  const res = await fetch(`${PLAN_BASE}/uploads/relay`, { method: "POST", body: form });
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
 * onProgress is called with a value 0–1 as bytes are sent.
 * Pass an AbortSignal to cancel mid-upload (slot returns to idle; the orphaned
 * GCS object is cleaned up by the 24h lifecycle rule).
 */
export function uploadToGcsWithProgress(
  uploadUrl: string,
  file: File,
  onProgress: (fraction: number) => void,
  signal?: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();

    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    });
    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error(`Upload failed (${xhr.status})`));
    });
    // Network-level failure = bucket CORS blocked this origin (any localhost).
    // Fall back to the API relay; progress becomes indeterminate (0.5) since
    // fetch-multipart has no upload progress events, then jumps to done.
    xhr.addEventListener("error", () => {
      if (signal?.aborted) {
        reject(new DOMException("Upload cancelled", "AbortError"));
        return;
      }
      onProgress(0.5);
      relaySignedUpload(uploadUrl, file)
        .then(() => {
          onProgress(1);
          resolve();
        })
        .catch(reject);
    });
    xhr.addEventListener("abort", () => reject(new DOMException("Upload cancelled", "AbortError")));

    if (signal) {
      signal.addEventListener("abort", () => xhr.abort(), { once: true });
    }

    xhr.open("PUT", uploadUrl);
    xhr.setRequestHeader("Content-Type", file.type);
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
): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/clips`, {
    method: "POST",
    body: JSON.stringify({
      clip_gcs_paths: clipGcsPaths,
      ...(assignments !== undefined ? { assignments } : {}),
    }),
  });
}

export function generatePlanItem(itemId: string): Promise<PlanItem> {
  return request<PlanItem>(`/plan-items/${itemId}/generate`, { method: "POST" });
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
}

// NOTE: `PlanItemVariant` is kept structurally assignable to the shared
// `EditableVariant` (lib/variant-editor/types.ts) so the 0-latency instant
// editor (IntroTextPreview + EditToolbar + useVariantEditSession) drives plan-item
// variants exactly as it does generative ones. The fields the shared machinery
// reads (text_mode, style_set_id, intro_text_size_px, render_status,
// base_video_url, intro_layout, intro_mode) must mirror EditableVariant's types
// — keep them in lockstep.
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
  words?: { text: string; start_s: number; end_s: number }[] | null;
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
  role: "generative_intro" | "generative_sequence";
  position?: "top" | "middle" | "bottom" | "custom";
  x_frac?: number | null;
  y_frac?: number | null;
  font_family?: string | null;
  size_px?: number | null;
  size_class?: "small" | "medium" | "large" | "xlarge" | "xxlarge" | "jumbo" | null;
  color?: string | null;
  highlight_color?: string | null;
  stroke_width?: number | null;
  alignment?: "left" | "center" | "right" | null;
  effect?: "static" | "fade-in" | "slide-up" | "karaoke-line" | null;
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
}

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

/**
 * Per-variant editor capability map — mirrors `_editor_capabilities` in
 * app/routes/generative_jobs.py. All-false ⇒ the editor shell is read-only;
 * per-section false gates that tool with its honest `*_reason`.
 */
export interface EditorCapabilities {
  text_elements?: boolean;
  timeline?: boolean;
  split_clips?: boolean;
  mix?: boolean;
  sfx?: boolean;
  overlays?: boolean;
  /** AI overlay suggestions inside the editor's Overlays drawer (plans/005-010).
   *  Deliberately does NOT check pool assets — the drawer owns the empty-pool state. */
  suggestions?: boolean;
  reason?: string;
  sfx_reason?: string | null;
  overlays_reason?: string | null;
  /** "autoplace_disabled" | "song_or_lyric_variant" | inherited overlay reasons. */
  suggestions_reason?: string | null;
}

export interface PlanItemVariant {
  variant_id: string;
  output_url: string | null;
  // Literal union (not bare string) to match EditableVariant — every plan
  // consumer compares against these literals, so this is non-breaking.
  render_status: "ready" | "rendering" | "failed" | null;
  // Edit controls: swap-song is hidden when music_track_id is null (the
  // original-audio variant has no song), and the style picker reflects style_set_id.
  text_mode: "lyrics" | "agent_text" | "none";
  music_track_id?: string | null;
  track_title?: string | null;
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
  // Instant editor: fresh-signed playback URL + GCS key of the text-free
  // fast-reburn base. The API's `_variants_for_response` already signs these for
  // plan-item renders (the plan flow just discarded them before); their presence
  // is what makes a variant instant-edit-eligible. Absent on lyrics/legacy.
  base_video_url?: string | null;
  base_video_path?: string | null;
  // Narrated on-video caption editor: editable cues over the caption-free base.
  // Present only on narrated variants; null otherwise.
  caption_cues?: CaptionCue[] | null;
  // Subtitles on/off, independent of caption_cues length — off always yields the
  // caption-free burn on Apply. Null/absent on legacy variants ⇒ treat as enabled
  // (matches the render-time default). See setPlanItemCaptionsEnabled.
  captions_enabled?: boolean | null;
  // "sentence" (full lines) or "word" (one word at a time). Present on narrated
  // + talking-to-camera variants. See setPlanItemVariantCaptionStyle.
  voiceover_caption_style?: VoiceoverCaptionStyle | null;
  // Background-sound (voice/bed) level — narrated only. Null = Nova's render-time
  // default. See setPlanItemNarratedBedLevel / BackgroundSoundControl.
  voiceover_bed_level?: number | null;
  // Generic rendered bed level returned by editor-capable variants (voiceover +
  // montage). Older narrated rows may only carry voiceover_bed_level.
  mix?: number | null;
  // Caption font (font-registry key) for narrated captions. Null = default (TikTok
  // Sans). Editable in the on-video caption editor; the reburn honors it.
  voiceover_caption_font?: string | null;
  // Language the subtitled captions were transcribed in ("en" | "tr"). Shown as the
  // editor chip; changing it re-transcribes (setPlanItemCaptionLanguage). Absent for
  // narrated/montage variants.
  caption_language?: string | null;
  // What actually rendered. "narrated" → captions are edited via CaptionEditor and
  // the hero shows the burned output, so it is NOT instant-edit-eligible. Absent
  // on legacy/montage variants. See isInstantEditEligible (variant-editor/eligibility).
  resolved_archetype?: string | null;
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
  /**
   * T6: True once the user has applied a PUT text-elements edit. The flag
   * prevents the read adapter from overwriting user edits on re-render.
   */
  text_elements_user_edited?: boolean;
  /** Media-overlay cards applied on top of this variant (slice 1). */
  media_overlays?: MediaOverlay[] | null;
  /** GCS key of the clean (un-carded) variant before the first overlay apply-pass. */
  pre_media_overlay_video_path?: string | null;
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
// Ask Nova advisor, footage pool. Append-only — do not edit code above.

// Clip notes + provisional machine matches (interface merging, append-only rule).
export interface ClipAssignment {
  /** Creator context about the clip ("famous vegan restaurant"). "" = none. */
  user_note?: string;
  /** True = the footage-pool matcher placed this clip (provisional chip). */
  machine_matched?: boolean;
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

/** "Looks wrong? Tell Nova" — mark the verdict contested (suppresses low-confidence re-reads). */
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
 * POST /plan-items/{id}/agent/turn — one "Ask Nova" advisor turn for this item.
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
  status: string; // "uploaded" | "analyzing" | "ready" | "failed"
  source_filename: string | null;
  duration_s: number | null;
  aspect: number | null;
  /** Pixel dims (plan 009 E1) — null on legacy assets until the backfill
   *  re-analyzes them. Feed the fullscreen low-res warning; never faked. */
  width?: number | null;
  height?: number | null;
  subject: string | null;
  display_url: string | null;
  deduped: boolean;
  /** Object key under users/{uid}/plan/{itemId}/pool/ — already inside
   *  attach_clips' allowed prefix, so "Use in edit" can promote the asset to a
   *  clip via the existing attach flow (no copy, no new endpoint). */
  gcs_path: string;
}

/** Signed PUT URLs for pool assets (users/{uid}/plan/{itemId}/pool/, persistent). */
export async function requestPoolAssetUploadUrls(
  itemId: string,
  files: { filename: string; content_type: string; file_size_bytes: number }[],
): Promise<UploadUrl[]> {
  const res = await request<{ urls: UploadUrl[] }>(`/plan-items/${itemId}/assets/upload-urls`, {
    method: "POST",
    body: JSON.stringify({ files }),
  });
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
    content_type: string;
    content_hash: string | null;
    source_filename: string | null;
  },
): Promise<PoolAsset> {
  return request<PoolAsset>(`/plan-items/${itemId}/assets`, {
    method: "POST",
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
export function listPoolAssets(
  itemId: string,
): Promise<{ assets: PoolAsset[]; max_assets: number }> {
  return request<{ assets: PoolAsset[]; max_assets: number }>(`/plan-items/${itemId}/assets`);
}

/** Remove an asset from the pool. */
export function deletePoolAsset(itemId: string, assetId: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/plan-items/${itemId}/assets/${assetId}`, {
    method: "DELETE",
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
