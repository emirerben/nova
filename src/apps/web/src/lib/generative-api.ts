/**
 * API client for generative-edit endpoints.
 * Mirrors src/lib/music-api.ts. Clip upload reuses the music slot-upload endpoint
 * (lands under the `music-uploads/` prefix the backend allowlists).
 */

import type { NovaStep } from "./job-phases";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// Intro font-size envelope + nudge step (mirrors overlay_sizing.MIN/MAX_INTRO_PX
// on the backend, which clamps server-side regardless of what the UI sends).
export const INTRO_SIZE_MIN = 40;
export const INTRO_SIZE_MAX = 80;
export const INTRO_SIZE_STEP = 6;

// Tooltip for text controls locked by a synced sequence intro
// (intro_mode === "sequence") — shared by VariantCard and PlanVariantEditor.
// Mode-neutral: a sequence variant is either transcript-synced (voiceover) or
// rhythm-mode (an authored quote over music), so the copy must not claim a voiceover.
export const SEQUENCE_TEXT_LOCKED_HINT =
  "Text is synced for this Editorial variant — switch to Classic to edit text";

export type GenerativeTextMode = "lyrics" | "agent_text" | "none";
export type RenderedMontagePreset = "masonry" | "polaroid_wall";

/**
 * Carousel-as-a-moment: a full-screen multi-clip carousel burned in at a
 * position in the edit. Mirrors `carousel_moment` on the variant-edit dispatch
 * endpoint (backend contract, "carousel as an editable visual template").
 * Every field is optional at the wire level — a PATCH-style partial merges
 * over the current moment server-side. `null` (not this type) on the wire
 * means "remove the moment"; the client sends that as a literal `null`, never
 * as this interface.
 */
export interface CarouselMoment {
  position?: "intro" | "middle" | "outro";
  mode?: "focus" | "rolling";
  effect?: "scale_sweep" | "cover_flow" | "cards_stack" | "flipbook";
  /** null = "let Nova pick" (server auto-selects the focus tile). */
  focus_clip_index?: number | null;
  /** 2..15 seconds. */
  duration_s?: number;
  transition?: "crossfade" | "none";
  /** Ordered manual choreography. `null` keeps Nova's legacy auto timing. */
  sequence?: Array<{ clip_index: number; hold_s: number }> | null;
  /** Seconds spent moving between cards (manual/ripple timing only). */
  move_duration_s?: number;
  /** Seconds for each focus zoom direction (ignored by Rolling mode). */
  zoom_duration_s?: number;
  transition_in?: "crossfade" | "none";
  transition_in_duration_s?: number;
  transition_out?: "crossfade" | "none";
  transition_out_duration_s?: number;
  /** Marks the deterministic, ripple-inserted timing contract. */
  timing_model?: "ripple_v1";
}

/**
 * A `CarouselMoment` as it may come back off the wire (`variant.carousel_moment`),
 * widened with the internal render-time focus shape. The pipeline reads ONLY
 * `focus` (`_apply_moment_overrides`/`_parse_focus_override`), never
 * `focus_clip_index` — the backend now persists both fields side by side
 * (`_merge_carousel_moment_override`), but a moment persisted before that fix
 * landed may carry ONLY this one. NOT part of the `CarouselMoment` contract
 * type itself (that type also shapes the POST payload / copilot snapshot,
 * where `keyof CarouselMoment` must stay exactly the contract fields) — kept
 * as a separate read-side type so widening it here can't leak `focus` into
 * those other surfaces.
 */
export type CarouselMomentPersisted = CarouselMoment & {
  focus?: Array<{ card_index: number }>;
};

/**
 * `focus_clip_index` is the contract field every reader should use — but a
 * moment persisted before the backend started writing it alongside `focus`
 * (see `CarouselMomentPersisted`) may have only the internal render-time
 * shape. Falls back to that shape's `focus[0].card_index` so already-
 * persisted moments still prefill correctly.
 */
export function resolveCarouselFocusClipIndex(
  moment: CarouselMomentPersisted | null | undefined,
): number | null {
  if (!moment) return null;
  if (moment.focus_clip_index !== undefined && moment.focus_clip_index !== null) {
    return moment.focus_clip_index;
  }
  return moment.focus?.[0]?.card_index ?? null;
}

export interface GenerativeVariant {
  variant_id: string;
  rank: number;
  text_mode: GenerativeTextMode;
  music_track_id: string | null;
  track_title: string | null;
  /**
   * Fresh-signed preview URL (+ best-section offset) for the matched track,
   * minted on every status read. Present even for unpublished tracks (which
   * the public /music-tracks gallery filters out) — the editor's virtual
   * preview falls back to this when the gallery has no entry for the track.
   */
  music_preview_url?: string | null;
  music_preview_start_s?: number | null;
  style_set_id: string | null;
  output_url: string | null;
  /** Attachment-signed URL for native, streaming download (never used by video playback). */
  download_url?: string | null;
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
  // Intro rendering mode (D6/D19). "sequence" = transcript-synced typographic
  // sequence (Editorial auto-upgrade on audible+coherent voiceovers): the text
  // is derived from the voiceover, so intro-text / highlight-word edits are
  // server-rejected with 422 — the size nudge and a layout opt-out (Classic or
  // static-cluster Editorial) remain allowed. Absent on legacy variants.
  intro_mode?: "sequence" | "cluster" | "linear" | null;
  // Convenience flag from the backend: true iff intro_mode === "sequence".
  sequence_synced?: boolean | null;
  // Whether the AI-intro overlay is occluded behind the moving subject
  // (text-behind-subject feature). Absent/false on legacy variants and when
  // the backend flag is off. See `text_behind_subject` on EditVariantPayload.
  intro_behind_subject?: boolean | null;
  // Voice/bed mix for voiceover variants (0..1; 1.0 = voice only / bed ducked,
  // 0.0 = bed full). null on non-voiceover variants.
  mix?: number | null;
  // The archetype that actually rendered this variant (Lane D). null on montage
  // variants. Carried for verification + Lane E UI; current UI ignores it.
  resolved_archetype?: string | null;
  montage_preset?: RenderedMontagePreset | null;
  montage_preset_rendered?: RenderedMontagePreset | null;
  montage_preset_fallback?: string | null;
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
  /** Approved guided-story timeline and strict publication evidence. */
  story_timeline?: Array<Record<string, unknown>> | null;
  proposal_version?: number | null;
  media_digest?: string | null;
  render_receipt?: Record<string, unknown> | null;
  duration_s?: number | null;
  // User-pinned independent overrides (decoupled from style_set_id).
  // Null when the user hasn't pinned them; the renderer uses the style-set value.
  intro_font_family?: string | null;
  intro_effect?: string | null;
  intro_text_color?: string | null;
  intro_cluster_hero_font?: string | null;
  intro_cluster_body_font?: string | null;
  intro_cluster_accent_font?: string | null;
  intro_cluster_hero_size_px?: number | null;
  intro_cluster_body_size_px?: number | null;
  intro_cluster_accent_size_px?: number | null;
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
  /** Stable server failure taxonomy. Strict guided-story verification errors
   * remain distinguishable from generic rendering failures. */
  failure_reason?: string | null;
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
  /** True while the render attempt died silently and is awaiting automatic
   *  retry (stale worker heartbeat). Optional — older API builds omit it. */
  retrying?: boolean;
  /** Nova AI steps activity feed (PR1 `nova_steps` projection). Optional —
   *  gated server-side by NOVA_STEPS_FEED_ENABLED; older API builds and the
   *  flag-off case both omit it. NovaActivityFeed falls back to
   *  PhaseChipRow whenever this is absent/empty. */
  steps?: NovaStep[] | null;
}

/** The SUCCESS half of the terminal set. */
export const GENERATIVE_SUCCESS_STATUSES = [
  "variants_ready",
  "variants_ready_partial",
  "clips_ready",
  "done",
];

/** The FAILURE half of the terminal set. */
export const GENERATIVE_FAILED_STATUSES = ["variants_failed", "processing_failed"];

/** Terminal statuses the poller should stop on. Composed so the two halves
 *  partition it — a new failure status can never be missing from FAILED. */
export const GENERATIVE_TERMINAL_STATUSES = [
  ...GENERATIVE_SUCCESS_STATUSES,
  ...GENERATIVE_FAILED_STATUSES,
];

/**
 * How long a variant may sit in "rendering" before we stop believing it.
 *
 * Bounds the success-terminal escape below. `reconcile_stuck_variants` only heals
 * a stranded variant after ~60 min, so without an upper bound the UI would poll a
 * dead render forever with a live-ticking timer.
 */
const STUCK_RENDER_CEILING_MS = 30 * 60 * 1000;

/**
 * Has this job settled for polling purposes?
 *
 * The subtlety: a re-render dispatched after a successful first render does NOT
 * move `job.status` — it stays `variants_ready`. So a naive "terminal status wins"
 * check stops the poller the instant the user presses Save, and they never see the
 * restarted clock or the new video.
 *
 * Three rules, in order:
 *  1. Not a terminal status  → not settled (the first render is still running).
 *  2. A FAILED terminal      → settled, whatever the variants say. A variant frozen
 *     in "rendering" after a failed job is a backend data-integrity gap and must
 *     never block the UI; it renders through the existing "failed" branch.
 *  3. A SUCCESS terminal     → not settled while a variant is genuinely rendering,
 *     where "genuinely" means its `render_started_at` is inside
 *     STUCK_RENDER_CEILING_MS. Past that the render is presumed dead and we settle
 *     rather than spin. A variant with no timestamp at all is treated as live (it
 *     was just dispatched and the stamp has not been read back yet).
 *
 * Single source of truth for all three ProgressTheater pollers — the item page,
 * the public generative page, and the onboarding EditPayoff panel. They each used
 * to hand-roll this and drifted.
 */
export function isGenerativeJobSettled(
  status: string | null | undefined,
  variants: ReadonlyArray<{ render_status?: string | null; render_started_at?: string | null }>
    | null
    | undefined,
  nowMs: number = Date.now(),
): boolean {
  if (status == null || !GENERATIVE_TERMINAL_STATUSES.includes(status)) return false;
  if (GENERATIVE_FAILED_STATUSES.includes(status)) return true;
  const liveRender = (variants ?? []).some((v) => {
    if (v.render_status !== "rendering") return false;
    if (!v.render_started_at) return true;
    const startedMs = new Date(v.render_started_at).getTime();
    if (!Number.isFinite(startedMs)) return true;
    return nowMs - startedMs < STUCK_RENDER_CEILING_MS;
  });
  return !liveRender;
}

type GenerativeUploadResult = { gcs_path: string; kind: "video" | "image" | "audio" };
type GenerativeUploadInit = GenerativeUploadResult & {
  upload_url: string;
  content_type: string;
  upload_headers: Record<string, string>;
};

function uploadError(detail: unknown, fallback: string): Error {
  if (typeof detail === "string" && detail) return new Error(detail);
  if (Array.isArray(detail)) {
    const message = detail
      .map((entry) => (typeof entry?.msg === "string" ? entry.msg : null))
      .filter(Boolean)
      .join("; ");
    if (message) return new Error(message);
  }
  return new Error(fallback);
}

const MAX_DIRECT_UPLOADS = 2;
let activeDirectUploads = 0;
const directUploadWaiters: Array<() => void> = [];

async function acquireDirectUploadSlot(): Promise<void> {
  if (activeDirectUploads >= MAX_DIRECT_UPLOADS) {
    await new Promise<void>((resolve) => directUploadWaiters.push(resolve));
  }
  activeDirectUploads += 1;
}

function releaseDirectUploadSlot(): void {
  activeDirectUploads -= 1;
  directUploadWaiters.shift()?.();
}

async function legacyUpload(file: File, errorLabel: string): Promise<GenerativeUploadResult> {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch(`${API_BASE}/music-jobs/upload-slot`, { method: "POST", body: fd });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? errorLabel);
  }
  return res.json();
}

async function relaySignedUpload(
  uploadUrl: string,
  file: File,
  contentType: string,
  uploadHeaders: Record<string, string>,
): Promise<void> {
  const form = new FormData();
  form.append("file", file, file.name);
  form.append("signed_url", uploadUrl);
  form.append("content_type", contentType);
  form.append("file_size_bytes", String(file.size));
  const ifGenerationMatch = uploadHeaders["x-goog-if-generation-match"];
  if (ifGenerationMatch) form.append("if_generation_match", ifGenerationMatch);
  const res = await fetch(`${API_BASE}/uploads/relay`, { method: "POST", body: form });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Upload failed");
  }
}

async function uploadGenerativeFile(file: File): Promise<GenerativeUploadResult> {
  await acquireDirectUploadSlot();
  try {
    const initRes = await fetch(`${API_BASE}/generative-jobs/upload-url`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: file.name,
        content_type: file.type || "application/octet-stream",
        file_size_bytes: file.size,
      }),
    });
    // Vercel may update before Fly during a split deploy. Only an absent route
    // uses the legacy byte proxy; validation/server failures stay visible.
    if (initRes.status === 404 || initRes.status === 405) {
      return legacyUpload(file, "Upload failed");
    }
    if (!initRes.ok) {
      const detail = await initRes.json().catch(() => ({ detail: initRes.statusText }));
      throw uploadError(detail.detail, "Upload failed");
    }
    const init = (await initRes.json()) as GenerativeUploadInit;
    const uploadHeaders = init.upload_headers ?? {};
    try {
      const putRes = await fetch(init.upload_url, {
        method: "PUT",
        headers: { "Content-Type": init.content_type, ...uploadHeaders },
        body: file,
      });
      if (!putRes.ok) throw new Error(`Upload failed (${putRes.status})`);
    } catch (error) {
      // Production CORS permits the canonical site. Preview and unusual local
      // origins can be blocked before a response exists; relay the SAME signed
      // request through Fly only for that network-level browser failure.
      if (error instanceof TypeError) {
        await relaySignedUpload(init.upload_url, file, init.content_type, uploadHeaders);
      } else {
        throw error;
      }
    }
    return { gcs_path: init.gcs_path, kind: init.kind };
  } finally {
    releaseDirectUploadSlot();
  }
}

export async function uploadGenerativeClip(
  file: File,
): Promise<{ gcs_path: string; kind: "video" | "image" }> {
  const result = await uploadGenerativeFile(file);
  if (result.kind === "audio") throw new Error("Clip upload must be a video or image");
  return { gcs_path: result.gcs_path, kind: result.kind };
}

/** Upload a voiceover (a recorded Blob or a chosen audio File) directly to GCS. */
export async function uploadVoiceover(
  file: File | Blob,
  filename = "voiceover.webm",
): Promise<{ gcs_path: string; kind: string }> {
  // A MediaRecorder Blob has no filename; give it one so the backend can sniff
  // the extension. A real File already carries its name, so prefer that.
  let uploadFile: File;
  if (file instanceof File) {
    const name = file.name.toLowerCase();
    if (name.endsWith(".mp4") && !(file.type || "").toLowerCase().startsWith("audio/")) {
      uploadFile = new File([file], file.name.replace(/\.mp4$/i, ".m4a"), {
        type: "audio/mp4",
        lastModified: file.lastModified,
      });
    } else {
      uploadFile = file;
    }
  } else {
    uploadFile = new File([file], filename, { type: file.type || "audio/webm" });
  }
  const body = await uploadGenerativeFile(uploadFile);
  if (body.kind !== "audio") {
    throw new Error("Voiceover upload must be an audio file");
  }
  return body;
}

export async function createGenerativeJob(
  clip_gcs_paths: string[],
  voiceover_gcs_path: string | null = null,
  opts: { topic?: string; intent?: string } = {},
): Promise<GenerativeJobResponse> {
  // No target length: the backend derives output length from the uploaded
  // footage (and the matched song's beat structure), so the edit can never run
  // longer than the clips the user provided. When a voiceover is provided the
  // backend renders voiceover variants instead.
  const res = await fetch(`${API_BASE}/generative-jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      clip_gcs_paths,
      voiceover_gcs_path,
      topic: opts.topic ?? undefined,
      intent: opts.intent ?? undefined,
    }),
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
  /** Independent font override — registry font name. */
  font_family?: string;
  /** Independent animation/effect override. */
  effect?: string;
  /** Independent text color override — hex string (#RRGGBB). */
  text_color?: string;
  /** Editorial cluster: hero-word font override. */
  cluster_hero_font?: string;
  /** Editorial cluster: body/connector font override. */
  cluster_body_font?: string;
  /** Editorial cluster: accent/closer font override. */
  cluster_accent_font?: string;
  /** Editorial cluster: per-role size overrides (absolute px). */
  cluster_hero_size_px?: number;
  cluster_body_size_px?: number;
  cluster_accent_size_px?: number;
  /** Occlude the AI-intro overlay behind the moving subject. Tri-state at the
   * wire level (undefined = keep current); gated server-side by
   * settings.text_behind_subject_enabled. */
  text_behind_subject?: boolean;
  /**
   * Carousel-as-a-moment (partial merges over the current moment server-side).
   * `undefined` = unchanged (field omitted from the request body); explicit
   * `null` = remove the moment entirely. See CarouselMoment.
   */
  carousel_moment?: CarouselMoment | null;
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
  | "masonry_preset"
  | "sources_expired";

/** Machine codes the timeline POST can reject with (409/422). */
export type TimelineErrorCode =
  | "disabled"
  | "TIMELINE_STALE"
  | "JOB_BUSY"
  | "TIMELINE_OUT_OF_BOUNDS"
  | "TIMELINE_INVALID_DURATION"
  | "TIMELINE_EMPTY"
  | "TIMELINE_UNKNOWN_CLIP"
  | "TIMELINE_BEATS_EXHAUSTED"
  | "TIMELINE_TOO_LONG"
  | "masonry_preset"
  | "sources_expired";

export type EditorTransition = "cut" | "crossfade" | "dip_to_black" | "flash";
export type LookPreset =
  | "none"
  | "stadium_diffusion"
  | "olive_film"
  | "smoky_split_tone";

export interface LookAdjustments {
  intensity: number;
  warmth: number;
  contrast: number;
  grain: number;
  vignette: number;
}

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
  transition_after?: EditorTransition;
  transition_duration_s?: number | null;
  look_preset?: LookPreset;
  look_adjustments?: LookAdjustments | null;
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
  transition_after?: EditorTransition;
  transition_duration_s?: number | null;
  look_preset?: LookPreset;
  look_adjustments?: LookAdjustments | null;
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
