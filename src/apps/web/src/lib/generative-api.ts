/**
 * API client for generative-edit endpoints.
 * Mirrors src/lib/music-api.ts. Clip upload reuses the music slot-upload endpoint
 * (lands under the `music-uploads/` prefix the backend allowlists).
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// Intro font-size envelope + nudge step (mirrors overlay_sizing.MIN/MAX_INTRO_PX
// on the backend, which clamps server-side regardless of what the UI sends).
export const INTRO_SIZE_MIN = 40;
export const INTRO_SIZE_MAX = 80;
export const INTRO_SIZE_STEP = 6;

export type GenerativeTextMode = "lyrics" | "agent_text" | "none";

export interface GenerativeVariant {
  variant_id: string;
  rank: number;
  text_mode: GenerativeTextMode;
  music_track_id: string | null;
  track_title: string | null;
  style_set_id: string | null;
  output_url: string | null;
  video_path: string | null;
  render_status: "ready" | "rendering" | "failed" | null;
  ok: boolean;
  error: string | null;
  // Agent-decided (or user-pinned) intro size. null for non-text variants.
  intro_text_size_px: number | null;
  intro_size_source: "computed" | "user" | null;
  intro_text?: string | null;
  intro_highlight_word?: string | null;
  // Effective intro layout. "cluster" = editorial word-cluster (multi-block,
  // engine-positioned) — the instant editor must NOT local-preview it (the TS
  // mirror only models the linear layout); edits use the server-reburn controls.
  intro_layout?: "linear" | "cluster" | null;
  // Voice/bed mix for voiceover variants (0..1; 1.0 = voice only / bed ducked,
  // 0.0 = bed full). null on non-voiceover variants.
  mix?: number | null;
  // The archetype that actually rendered this variant (Lane D). null on montage
  // variants. Carried for verification + Lane E UI; current UI ignores it.
  resolved_archetype?: string | null;
  // PR2 instrumentation fields — optional so older API builds degrade gracefully.
  render_started_at?: string | null;
  render_finished_at?: string | null;
  error_class?: string | null;
  // Instant edit: fresh-signed playback URL of the text-free fast-reburn base
  // (agent_text/none variants only). Present even while render_status is
  // "rendering" so the editor keeps playing the base during a committed
  // re-render. Absent on lyrics/legacy variants → instant editor hidden.
  base_video_url?: string | null;
  base_video_path?: string | null;
}

/** Full intro-role look of a style set — drives the instant-edit client preview.
 * Display-only projection (never reaches the renderer burn dict). */
export interface StyleSetIntroPreview {
  font_family?: string | null;
  css_family?: string | null;
  font_file?: string | null;
  font_weight?: number | null;
  text_color?: string | null;
  highlight_color?: string | null;
  effect?: string | null;
  position?: string | null;
  position_x_frac?: number | null;
  position_y_frac?: number | null;
  text_anchor?: string | null;
  stroke_width?: number | null;
  text_size_px?: number | null;
}

export interface GenerativeStyleSet {
  id: string;
  label: string;
  tags: string[];
  // Display-only typography of the set's representative (hook) role, so the picker
  // can render a real-font preview chip BEFORE a re-render. All optional — older
  // API builds omit them and the chip falls back to the page font. `css_family`
  // matches a `@font-face` from the shared registry (see lib/font-faces.ts).
  font_family?: string | null;
  css_family?: string | null;
  font_file?: string | null;
  font_weight?: number | null;
  text_color?: string | null;
  highlight_color?: string | null;
  effect?: string | null;
  // Full intro-role look for the instant-edit preview. Optional — older API
  // builds omit it and the preview falls back to renderer defaults.
  intro?: StyleSetIntroPreview | null;
}

export interface GenerativeJobResponse {
  job_id: string;
  status: string;
}

export interface GenerativeJobStatus {
  job_id: string;
  status: string;
  variants: GenerativeVariant[];
  error_detail: string | null;
  created_at: string;
  updated_at: string;
  // Plan-declared edit format (montage default). Per-variant `resolved_archetype`
  // says what actually rendered. Optional — older API builds omit it.
  edit_format?: string | null;
  // PR2 instrumentation fields — optional so older API builds degrade gracefully.
  current_phase?: string | null;
  phase_log?: Array<{ name: string; ts: string; elapsed_ms?: number }> | null;
  started_at?: string | null;
  finished_at?: string | null;
  expected_phase_durations?: Record<string, number> | null;
}

/** Terminal statuses the poller should stop on. */
export const GENERATIVE_TERMINAL_STATUSES = [
  "variants_ready",
  "variants_ready_partial",
  "variants_failed",
  "processing_failed",
];

export async function uploadGenerativeClip(
  file: File,
): Promise<{ gcs_path: string; kind: "video" | "image" }> {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch(`${API_BASE}/music-jobs/upload-slot`, { method: "POST", body: fd });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Upload failed");
  }
  return res.json();
}

/** Upload a voiceover (a recorded Blob or a chosen audio File). Reuses the music
 * slot-upload endpoint; for an audio file the backend returns `kind: "audio"`. */
export async function uploadVoiceover(
  file: File | Blob,
  filename = "voiceover.webm",
): Promise<{ gcs_path: string; kind: string }> {
  const fd = new FormData();
  // A MediaRecorder Blob has no filename; give it one so the backend can sniff
  // the extension. A real File already carries its name, so prefer that.
  if (file instanceof File) {
    fd.append("file", file);
  } else {
    fd.append("file", file, filename);
  }
  const res = await fetch(`${API_BASE}/music-jobs/upload-slot`, { method: "POST", body: fd });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Voiceover upload failed");
  }
  return res.json();
}

export async function createGenerativeJob(
  clip_gcs_paths: string[],
  voiceover_gcs_path: string | null = null,
): Promise<GenerativeJobResponse> {
  // No target length: the backend derives output length from the uploaded
  // footage (and the matched song's beat structure), so the edit can never run
  // longer than the clips the user provided. When a voiceover is provided the
  // backend renders voiceover variants instead.
  const res = await fetch(`${API_BASE}/generative-jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ clip_gcs_paths, voiceover_gcs_path }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Failed to create generative job");
  }
  return res.json();
}

export async function getGenerativeJobStatus(jobId: string): Promise<GenerativeJobStatus> {
  const res = await fetch(`${API_BASE}/generative-jobs/${jobId}/status`);
  if (!res.ok) throw new Error(`Failed to get job status: ${res.status}`);
  return res.json();
}

export async function swapVariantSong(
  jobId: string,
  variantId: string,
  newTrackId: string,
): Promise<GenerativeJobResponse> {
  const res = await fetch(`${API_BASE}/generative-jobs/${jobId}/variants/${variantId}/swap-song`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ new_track_id: newTrackId }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Failed to swap song");
  }
  return res.json();
}

export async function retextVariant(
  jobId: string,
  variantId: string,
  opts: { text?: string; remove?: boolean },
): Promise<GenerativeJobResponse> {
  const res = await fetch(`${API_BASE}/generative-jobs/${jobId}/variants/${variantId}/retext`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: opts.text ?? null, remove: opts.remove ?? false }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Failed to update text");
  }
  return res.json();
}

/** The curated text style sets selectable for a generative edit (generative-eligible). */
export async function getGenerativeStyleSets(): Promise<GenerativeStyleSet[]> {
  const res = await fetch(`${API_BASE}/generative-jobs/style-sets`);
  if (!res.ok) throw new Error(`Failed to load style sets: ${res.status}`);
  const data = await res.json();
  return data.style_sets;
}

export async function changeVariantStyle(
  jobId: string,
  variantId: string,
  styleSetId: string,
): Promise<GenerativeJobResponse> {
  const res = await fetch(
    `${API_BASE}/generative-jobs/${jobId}/variants/${variantId}/change-style`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ style_set_id: styleSetId }),
    },
  );
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Failed to change style");
  }
  return res.json();
}

/** Set the voice/bed mix for a voiceover variant (0..1) — re-renders the variant.
 * Mirrors setVariantIntroSize; treats any non-ok response as an error. */
export async function setVariantMix(
  jobId: string,
  variantId: string,
  mix: number,
): Promise<void> {
  const res = await fetch(
    `${API_BASE}/generative-jobs/${jobId}/variants/${variantId}/mix`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mix }),
    },
  );
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Failed to set mix");
  }
}

/** One instant-edit session commit: text + style + size in a single request →
 * a single re-render. `text` and `remove_text` are mutually exclusive. */
export interface EditVariantPayload {
  text?: string;
  remove_text?: boolean;
  style_set_id?: string;
  text_size_px?: number;
  // Post-render layout pick: "cluster" = editorial word-cluster (3-6 word hooks
  // only — the server 422s otherwise), "linear" = classic centered block.
  intro_layout?: "linear" | "cluster";
}

export async function editVariant(
  jobId: string,
  variantId: string,
  payload: EditVariantPayload,
): Promise<GenerativeJobResponse> {
  const res = await fetch(`${API_BASE}/generative-jobs/${jobId}/variants/${variantId}/edit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...payload,
      text_size_px:
        payload.text_size_px !== undefined ? Math.round(payload.text_size_px) : undefined,
    }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Failed to save edits");
  }
  return res.json();
}

// ── Clip-timeline editor ──────────────────────────────────────────────────────
// Hand-mirrored from the backend timeline schema — keep literal unions in sync
// with the Pydantic schema (same precedent as SongSection in music-api.ts).

/** Why a variant's timeline is not editable (`editable: false`). */
export type TimelineUneditableReason =
  | "disabled"
  | "lyrics_sync"
  | "no_slot_timeline"
  | "voiceover_bed_fit"
  | "unsupported_variant"
  | "no_timeline"
  | "sources_expired";

/** Machine codes the timeline POST can reject with (409/422). */
export type TimelineErrorCode =
  | "disabled"
  | "TIMELINE_STALE"
  | "JOB_BUSY"
  | "TIMELINE_OUT_OF_BOUNDS"
  | "TIMELINE_TOO_SHORT"
  | "TIMELINE_INVALID_DURATION"
  | "TIMELINE_EMPTY"
  | "TIMELINE_UNKNOWN_CLIP"
  | "TIMELINE_BEATS_EXHAUSTED"
  | "TIMELINE_TOO_LONG"
  | "sources_expired";

export interface TimelineSlot {
  slot_id: string;
  clip_index: number;
  source_gcs_path: string;
  /** null for clips the worker never probed (e.g. user-added pool clips). */
  source_duration_s: number | null;
  in_s: number;
  duration_s: number;
  /** null on no-grid (original_text) timelines — duration_s is authoritative. */
  duration_beats: number | null;
  order: number;
  moment_energy: number | null;
  moment_description: string | null;
  removed?: boolean;
}

export interface TimelineClip {
  clip_index: number;
  /** null when signing failed server-side — the editor still opens. */
  signed_url: string | null;
  duration_s: number | null;
  used: boolean;
}

export interface TimelineResponse {
  editable: boolean;
  reason: TimelineUneditableReason | null;
  /** Non-uniform beat timestamps (seconds). Empty for original_text variants. */
  beat_grid: number[];
  total_duration_s: number;
  has_user_edits: boolean;
  slots: TimelineSlot[];
  clips: TimelineClip[];
}

/** One slot in the POST body. Exactly one of duration_beats / duration_s set. */
export interface TimelineEditSlotPayload {
  slot_id: string | null;
  clip_index: number;
  in_s: number;
  duration_beats: number | null;
  duration_s: number | null;
  removed: boolean;
}

/** Timeline error with the machine code preserved (404 → code null). */
export class TimelineApiError extends Error {
  status: number;
  code: string | null;
  constructor(status: number, code: string | null, message?: string) {
    super(message ?? `Timeline request failed (${status}${code ? ` ${code}` : ""})`);
    this.name = "TimelineApiError";
    this.status = status;
    this.code = code;
  }
}

/** The error payload may be wrapped in FastAPI `detail` — handle both
 * `{code}` and `{detail: {code}}` (plus a bare string detail). */
async function throwTimelineError(res: Response): Promise<never> {
  let code: string | null = null;
  try {
    const body = await res.json();
    if (typeof body?.code === "string") code = body.code;
    else if (typeof body?.detail?.code === "string") code = body.detail.code;
    else if (typeof body?.detail === "string") code = body.detail;
  } catch {
    // Non-JSON error body — keep code null.
  }
  throw new TimelineApiError(res.status, code);
}

/**
 * Which backend route family owns the timeline. The plan-item mirror endpoints
 * (`/plan-items/{item_id}/variants/{vid}/timeline`) reuse the generative dispatch
 * helpers server-side — identical request/response shapes — but are ownership-
 * checked, so they go through the authenticated same-origin /api/plan proxy
 * (relative URL, session cookie) exactly like the mutations in plan-api.ts.
 */
export type TimelineBase = "generative" | "plan-item";

/** `ownerId` is the generative job id, or the plan-item id for "plan-item". */
function timelineUrl(base: TimelineBase, ownerId: string, variantId: string): string {
  return base === "plan-item"
    ? `/api/plan/plan-items/${ownerId}/variants/${variantId}/timeline`
    : `${API_BASE}/generative-jobs/${ownerId}/variants/${variantId}/timeline`;
}

export async function getTimeline(
  ownerId: string,
  variantId: string,
  base: TimelineBase = "generative",
): Promise<TimelineResponse> {
  const res = await fetch(timelineUrl(base, ownerId, variantId));
  if (!res.ok) return throwTimelineError(res);
  return res.json();
}

/** Submit an edited cut — enqueues a re-render on success. */
export async function editTimeline(
  ownerId: string,
  variantId: string,
  slots: TimelineEditSlotPayload[],
  base: TimelineBase = "generative",
): Promise<void> {
  const res = await fetch(timelineUrl(base, ownerId, variantId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ slots }),
  });
  if (!res.ok) return throwTimelineError(res);
}

/** Reset to the AI cut — discards user edits and re-renders. */
export async function resetTimeline(
  ownerId: string,
  variantId: string,
  base: TimelineBase = "generative",
): Promise<void> {
  const res = await fetch(timelineUrl(base, ownerId, variantId), {
    method: "DELETE",
  });
  if (!res.ok) return throwTimelineError(res);
}

export async function setVariantIntroSize(
  jobId: string,
  variantId: string,
  textSizePx: number,
): Promise<GenerativeJobResponse> {
  const res = await fetch(
    `${API_BASE}/generative-jobs/${jobId}/variants/${variantId}/intro-size`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text_size_px: Math.round(textSizePx) }),
    },
  );
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Failed to resize intro text");
  }
  return res.json();
}
