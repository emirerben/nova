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

export interface EditorCommitRequest {
  /** Full replacement text-element list (same shape putTextElements sends). */
  text_elements?: TextElement[];
  /** Full replacement narrated caption cue list. */
  caption_cues?: CaptionCue[];
  /** Clip-slot overrides (timeline task). Omit when untouched. */
  timeline_slots?: EditorTimelineSlot[];
  /** Voice/bed mix 0..1 (gutter mutes map onto this). Omit when untouched. */
  mix?: EditorCommitMix;
  /** Full replacement sound-effect placement list. Omit when untouched. */
  sound_effects?: SoundEffectPlacement[];
  /** Full replacement media-overlay card list. Omit when untouched. */
  media_overlays?: MediaOverlay[];
  /** Working-state title. Omit when untouched; null clears. */
  title?: string | null;
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
    timeline?: boolean;
    mix?: boolean;
    sound_effects?: boolean;
    media_overlays?: boolean;
    title?: boolean;
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
  textDirty = true,
  captionDirty = false,
  timelineDirty,
  slots,
  mixDirty = false,
  mixLevel,
  sfxDirty = false,
  soundEffects = [],
  overlaysDirty = false,
  mediaOverlays = [],
  acceptedSuggestions = [],
  titleDirty = true,
  title,
  variant,
}: {
  elements: TextElement[];
  captionCues?: CaptionCue[];
  textDirty?: boolean;
  captionDirty?: boolean;
  timelineDirty: boolean;
  slots: EditorCommitDraftSlot[];
  mixDirty?: boolean;
  mixLevel?: number | null;
  sfxDirty?: boolean;
  soundEffects?: SoundEffectPlacement[];
  overlaysDirty?: boolean;
  mediaOverlays?: MediaOverlay[];
  acceptedSuggestions?: AcceptedSuggestionRef[];
  titleDirty?: boolean;
  title: string;
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
  return {
    text_elements: textDirty ? elements : undefined,
    caption_cues: captionDirty ? (captionCues ?? []) : undefined,
    timeline_slots: timelineDirty
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
    sound_effects: sfxDirty ? soundEffects : undefined,
    media_overlays: overlaysDirty ? mediaOverlays : undefined,
    accepted_suggestion_ids: acceptedIds.length > 0 ? acceptedIds : undefined,
    title: titleDirty ? (title.trim() !== "" ? title.trim() : null) : undefined,
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

/** Friendly copy for the timeline-validation machine codes the save endpoint
 * can 409/422 with (`_timeline_error` in generative_jobs.py). Codes not
 * listed here fall through to the raw string — better than nothing, but add
 * new codes here as they're discovered surfacing verbatim to users. */
const TIMELINE_ERROR_MESSAGES: Record<string, string> = {
  TIMELINE_TOO_SHORT: "That clip would be shorter than the minimum (0.6s).",
  TIMELINE_TOO_LONG: "That timeline is longer than the maximum allowed length.",
  TIMELINE_OUT_OF_BOUNDS:
    "One of the clips ran out of footage for this edit. Try trimming it or picking a different clip.",
  TIMELINE_BEATS_EXHAUSTED: "Ran out of song to sync clips to — try removing a clip.",
  TIMELINE_INVALID_DURATION: "One of the clips has an invalid length.",
  TIMELINE_EMPTY: "The timeline needs at least one clip.",
  TIMELINE_UNKNOWN_CLIP: "One of the clips isn't part of this edit anymore.",
  TIMELINE_STALE: "This video changed in another tab — reload to continue.",
  sources_expired: "One of the clips has expired and needs to be re-uploaded.",
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
