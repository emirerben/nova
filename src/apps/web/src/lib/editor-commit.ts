/**
 * Editor-commit client (plan E2) — the SINGLE transactional persist for the
 * TikTok-parity editor shell's Save button.
 *
 * POST /plan-items/{itemId}/variants/{variantId}/editor-commit
 *
 * The server validates ALL sections, compares `base_generation` (stale
 * baseline → 409, surfaced as the §9 conflict tile), persists atomically in
 * one job-JSON update, bumps `render_gen_id`, and kicks ONE render. This
 * replaces per-field endpoints (putTextElements / timeline / mix) for shell
 * saves — no partial states across separate writes.
 *
 * The backend route lands with the API-side task; until then this call 404s
 * locally — callers keep working state and show the quiet retry tile.
 *
 * Calls go through the same-origin /api/plan proxy (session cookie +
 * INTERNAL_API_KEY injection), exactly like lib/plan-api.ts.
 */

import {
  NotAuthenticatedError,
  type MediaOverlay,
  type CaptionCue,
  type SoundEffectPlacement,
  type TextElement,
  type VisualBlock,
} from "@/lib/plan-api";

const PLAN_BASE = "/api/plan";

/** One clip-slot override, mirroring the timeline-override contract
 * (user_timeline slots; `removed: true` = slot deleted, ≥1 slot floor
 * enforced server-side). Optional section — omitted until the timeline
 * task wires clip edits into the shell. */
export interface EditorTimelineSlot {
  slot_id: string | null;
  clip_index: number;
  in_s: number;
  duration_s: number | null;
  duration_beats: number | null;
  removed: boolean;
}

export interface EditorCommitMix {
  music_level?: number | null;
  original_level?: number | null;
}

export interface EditorCommitCaptionMetaDraft {
  enabled?: boolean;
  style?: "sentence" | "word";
  font?: string | null;
  y_frac?: number | null;
}

export interface EditorCommitCaptionMetaRequest {
  enabled?: boolean;
  style?: "sentence" | "word";
  font?: string | null;
  font_set: boolean;
  y_frac?: number;
}

export interface LyricLineOverride {
  text?: string;
  style?: {
    color?: string;
    highlight_color?: string;
    font_family?: string;
    size_px?: number;
  };
  orig_text: string;
  orig_start_s: number;
}

export interface EditorCommitLyricsRequest {
  enabled?: boolean;
  line_overrides?: Record<string, LyricLineOverride> | null;
}

export interface EditorCommitRequest {
  /** Full replacement text-element list (same shape putTextElements sends). */
  text_elements?: TextElement[];
  /** Full replacement narrated caption cue list. */
  caption_cues?: CaptionCue[];
  /** Narrated-caption display settings. Omit when untouched. */
  caption_meta?: EditorCommitCaptionMetaRequest;
  /** Clip-slot overrides (timeline task). Omit when untouched. */
  timeline_slots?: EditorTimelineSlot[];
  /** Voice/bed mix 0..1 (gutter mutes map onto this). Omit when untouched. */
  mix?: EditorCommitMix;
  /** New music track id. Omit when untouched. */
  music_track_id?: string | null;
  music_window?: {
    start_s: number;
    alignment: "preserve_cuts" | "resync_beats";
  };
  /** Full replacement sound-effect placement list. Omit when untouched. */
  sound_effects?: SoundEffectPlacement[];
  /** Full replacement media-overlay card list. Omit when untouched. */
  media_overlays?: MediaOverlay[];
  /** Full replacement visual-block list. Omit when untouched. */
  visual_blocks?: VisualBlock[];
  /** Working-state title. Omit when untouched; null clears. */
  title?: string | null;
  /** Lyrics editor state. Omit when untouched; any presence triggers full render. */
  lyrics?: EditorCommitLyricsRequest;
  /** Output canvas orientation. Omit when untouched; any presence triggers full render. */
  orientation?: "portrait" | "landscape";
  /**
   * AI-suggestion resolution metadata, NOT a section: envelope ids from
   * `variants[i].overlay_suggestions` the user ✓-accepted in the editor. Their
   * cards ride inside `media_overlays`; the commit drops the envelopes
   * atomically with that write. The server 422s when these arrive WITHOUT the
   * media_overlays section — the builder only emits them alongside it.
   */
  accepted_suggestion_ids?: string[];
  /**
   * Compare-and-fail baseline (stale-baseline 409, plan §9). The variant's
   * `render_gen_id` once the GET payload exposes it; until then callers seed
   * it from `render_finished_at` (string) — the server accepts either.
   */
  base_generation: string;
}

export interface EditorCommitResponse {
  ok: boolean;
  /** The new monotonic render generation stamped by this commit. */
  generation: string;
  /** Per-section persist echo — which sections this commit actually wrote. */
  sections: {
    text_elements?: boolean;
    caption_cues?: boolean;
    caption_meta?: boolean;
    timeline?: boolean;
    mix?: boolean;
    music?: boolean;
    sound_effects?: boolean;
    media_overlays?: boolean;
    visual_blocks?: boolean;
    title?: boolean;
    lyrics?: boolean;
    orientation?: boolean;
  };
}

export interface EditorCommitDraftSlot {
  slotId: string | null;
  clipIndex: number;
  inS: number;
  durationS: number | null;
  durationBeats: number | null;
  removed: boolean;
}

export interface EditorCommitVariantBaseline {
  render_generation_id?: string | null;
  render_finished_at?: string | null;
  music_track_id?: string | null;
  editor_capabilities?: {
    mix?: boolean;
  } | null;
  mix?: number | null;
  voiceover_bed_level?: number | null;
}

export function editorCommitBaseGeneration(
  variant: EditorCommitVariantBaseline,
): string {
  return variant.render_generation_id ?? variant.render_finished_at ?? "";
}

/** One editor-accepted AI suggestion: envelope id + the overlay card id it
 * staged into the working overlay list (the undo filter key). */
export interface AcceptedSuggestionRef {
  id: string;
  overlayId: string;
}

export function buildEditorCommitRequest({
  elements,
  captionCues,
  captionMeta,
  textDirty = true,
  captionDirty = false,
  captionMetaDirty = false,
  timelineDirty,
  slots,
  mixDirty = false,
  mixLevel,
  musicDirty = false,
  musicTrackId,
  musicWindow,
  sfxDirty = false,
  soundEffects = [],
  overlaysDirty = false,
  mediaOverlays = [],
  visualBlocksDirty = false,
  visualBlocks = [],
  acceptedSuggestions = [],
  titleDirty = true,
  title,
  lyricsDirty = false,
  lyrics,
  orientationDirty = false,
  orientation,
  variant,
}: {
  elements: TextElement[];
  captionCues?: CaptionCue[];
  captionMeta?: EditorCommitCaptionMetaDraft;
  textDirty?: boolean;
  captionDirty?: boolean;
  captionMetaDirty?: boolean;
  timelineDirty: boolean;
  slots: EditorCommitDraftSlot[];
  mixDirty?: boolean;
  mixLevel?: number | null;
  musicDirty?: boolean;
  musicTrackId?: string | null;
  musicWindow?: {
    startS: number;
    alignment: "preserve_cuts" | "resync_beats";
  };
  sfxDirty?: boolean;
  soundEffects?: SoundEffectPlacement[];
  overlaysDirty?: boolean;
  mediaOverlays?: MediaOverlay[];
  visualBlocksDirty?: boolean;
  visualBlocks?: VisualBlock[];
  acceptedSuggestions?: AcceptedSuggestionRef[];
  titleDirty?: boolean;
  title: string;
  lyricsDirty?: boolean;
  lyrics?: EditorCommitLyricsRequest;
  orientationDirty?: boolean;
  orientation?: "portrait" | "landscape";
  variant: EditorCommitVariantBaseline;
}): EditorCommitRequest {
  const mixEditable = variant.editor_capabilities?.mix !== false;
  const normalizedMix =
    mixLevel == null ? null : Math.max(0, Math.min(1, Number(mixLevel)));
  // An accepted suggestion the user later undid (its card is no longer in the
  // staged overlay list) must NOT be resolved server-side — filter against the
  // overlays actually being sent. Ids ride ONLY with the media_overlays
  // section (the server 422s otherwise).
  const stagedOverlayIds = new Set(mediaOverlays.map((o) => o.id));
  const acceptedIds = overlaysDirty
    ? acceptedSuggestions
        .filter((a) => stagedOverlayIds.has(a.overlayId))
        .map((a) => a.id)
    : [];
  const hasCaptionFont =
    !!captionMeta && Object.prototype.hasOwnProperty.call(captionMeta, "font");
  const captionMetaRequest: EditorCommitCaptionMetaRequest | undefined =
    captionMetaDirty && captionMeta
      ? {
          ...(captionMeta.enabled !== undefined ? { enabled: captionMeta.enabled } : {}),
          ...(captionMeta.style !== undefined ? { style: captionMeta.style } : {}),
          ...(hasCaptionFont ? { font: captionMeta.font ?? null } : {}),
          font_set: hasCaptionFont,
          ...(typeof captionMeta.y_frac === "number" ? { y_frac: captionMeta.y_frac } : {}),
        }
      : undefined;
  return {
    text_elements: textDirty ? elements : undefined,
    caption_cues: captionDirty ? (captionCues ?? []) : undefined,
    caption_meta: captionMetaRequest,
    timeline_slots:
      timelineDirty &&
      ((!musicDirty && !musicWindow) || musicWindow?.alignment === "preserve_cuts")
      ? slots.map((s) => ({
          slot_id: s.slotId,
          clip_index: s.clipIndex,
          in_s: s.inS,
          duration_s: s.durationS,
          duration_beats: s.durationBeats,
          removed: s.removed,
        }))
      : undefined,
    mix: mixDirty && mixEditable && normalizedMix != null
      ? { music_level: normalizedMix }
      : undefined,
    music_track_id:
      musicDirty && musicTrackId !== variant.music_track_id
        ? musicTrackId ?? null
        : undefined,
    music_window: musicWindow
      ? { start_s: musicWindow.startS, alignment: musicWindow.alignment }
      : undefined,
    sound_effects: sfxDirty ? soundEffects : undefined,
    media_overlays: overlaysDirty ? mediaOverlays : undefined,
    visual_blocks: visualBlocksDirty ? visualBlocks : undefined,
    accepted_suggestion_ids: acceptedIds.length > 0 ? acceptedIds : undefined,
    title: titleDirty ? (title.trim() !== "" ? title.trim() : null) : undefined,
    lyrics: lyricsDirty ? lyrics : undefined,
    orientation: orientationDirty ? orientation : undefined,
    base_generation: editorCommitBaseGeneration(variant),
  };
}

function formatLoc(loc: unknown): string {
  if (Array.isArray(loc)) {
    return loc
      .filter((part) => part !== "body")
      .map(String)
      .join(".");
  }
  return typeof loc === "string" ? loc : "detail";
}

/** Friendly copy for machine-readable editor validation codes the save endpoint
 * can return. Codes not
 * listed here fall through to the raw string — better than nothing, but add
 * new codes here as they're discovered surfacing verbatim to users. */
const TIMELINE_ERROR_MESSAGES: Record<string, string> = {
  TIMELINE_TOO_LONG: "That timeline is longer than the maximum allowed length.",
  TIMELINE_OUT_OF_BOUNDS:
    "One of the clips ran out of footage for this edit. Try trimming it or picking a different clip.",
  TIMELINE_BEATS_EXHAUSTED: "Ran out of song to sync clips to — try removing a clip.",
  TIMELINE_INVALID_DURATION: "One of the clips has an invalid length.",
  TIMELINE_EMPTY: "The timeline needs at least one clip.",
  TIMELINE_UNKNOWN_CLIP: "One of the clips isn't part of this edit anymore.",
  TIMELINE_STALE: "This video changed in another tab — reload to continue.",
  masonry_preset: "Collage presets do not use a clip timeline.",
  sources_expired: "One of the clips has expired and needs to be re-uploaded.",
  music_window_out_of_range:
    "That song section is no longer available. Choose another point in the song.",
  music_window_unsupported_variant:
    "Song section editing is not available for this version.",
  music_track_unavailable:
    "That song is no longer available. Choose another song and try again.",
  video_duration_unknown:
    "The video duration is unavailable, so its song section cannot be changed.",
  track_duration_unknown:
    "The song duration is unavailable, so its section cannot be changed.",
  song_shorter_than_video: "This song is shorter than the video.",
  timing_metadata_unavailable:
    "Beat timing is unavailable for this song, so its section cannot be changed.",
  linear_timeline_unavailable:
    "This older edit cannot preserve its cuts. Choose Re-sync to beats instead.",
};

function formatDetailValue(detail: unknown, fallback: string): string {
  if (typeof detail === "string") {
    if (detail in TIMELINE_ERROR_MESSAGES) {
      return TIMELINE_ERROR_MESSAGES[detail];
    }
    const match = detail.match(
      /^Text element ([^:]+): field ([^ ]+) has invalid value [\s\S]*: (.+)$/,
    );
    if (match) return `Text ${match[1]}: field ${match[2]} — ${match[3]}`;
    return detail;
  }
  if (Array.isArray(detail)) {
    const lines = detail.map((entry) => {
      if (typeof entry === "string") return entry;
      if (entry && typeof entry === "object") {
        const record = entry as { loc?: unknown; msg?: unknown };
        const loc = formatLoc(record.loc);
        const msg =
          typeof record.msg === "string" ? record.msg : JSON.stringify(entry);
        return `${loc}: ${msg}`;
      }
      return String(entry);
    });
    return lines.join("\n");
  }
  if (detail && typeof detail === "object") {
    const record = detail as { detail?: unknown; code?: unknown; msg?: unknown };
    if (record.detail !== undefined) {
      return formatDetailValue(record.detail, fallback);
    }
    if (typeof record.code === "string") return formatDetailValue(record.code, fallback);
    if (typeof record.msg === "string") return record.msg;
    try {
      return JSON.stringify(detail);
    } catch {
      return fallback;
    }
  }
  return fallback;
}

export function formatEditorCommitError(
  payload: unknown,
  status: number,
): string {
  return formatDetailValue(payload, `Save failed (${status})`);
}

/** Thrown on a 409: the variant changed under this session (another tab
 * saved, or an older bake landed). Callers show the reload-to-continue tile. */
export class EditorCommitConflictError extends Error {
  constructor(detail?: string) {
    super(detail ?? "This video changed in another tab — reload to continue");
    this.name = "EditorCommitConflictError";
  }
}

export async function commitEditorSession(
  planItemId: string,
  variantId: string,
  body: EditorCommitRequest,
): Promise<EditorCommitResponse> {
  const res = await fetch(
    `${PLAN_BASE}/plan-items/${planItemId}/variants/${variantId}/editor-commit`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  if (res.status === 401) throw new NotAuthenticatedError();
  if (res.status === 409) {
    let detail: string | undefined;
    try {
      detail = ((await res.json()) as { detail?: string })?.detail;
    } catch {
      /* non-JSON body */
    }
    throw new EditorCommitConflictError(detail);
  }
  if (!res.ok) {
    let detail = `Save failed (${res.status})`;
    try {
      detail = formatEditorCommitError(await res.json(), res.status);
    } catch {
      /* non-JSON body — keep the generic message */
    }
    throw new Error(detail);
  }
  return (await res.json()) as EditorCommitResponse;
}
