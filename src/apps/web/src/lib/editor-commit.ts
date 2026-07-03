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

import { NotAuthenticatedError, type TextElement } from "@/lib/plan-api";

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
  text_elements: TextElement[];
  /** Clip-slot overrides (timeline task). Omit when untouched. */
  timeline_slots?: EditorTimelineSlot[];
  /** Voice/bed mix 0..1 (gutter mutes map onto this). Omit when untouched. */
  mix?: EditorCommitMix;
  /** Working-state title. Omit when untouched; null clears. */
  title?: string | null;
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
    timeline?: boolean;
    mix?: boolean;
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
}

export function editorCommitBaseGeneration(
  variant: EditorCommitVariantBaseline,
): string {
  return variant.render_generation_id ?? variant.render_finished_at ?? "";
}

export function buildEditorCommitRequest({
  elements,
  timelineDirty,
  slots,
  soundMuted,
  title,
  variant,
}: {
  elements: TextElement[];
  timelineDirty: boolean;
  slots: EditorCommitDraftSlot[];
  soundMuted: boolean;
  title: string;
  variant: EditorCommitVariantBaseline;
}): EditorCommitRequest {
  return {
    text_elements: elements,
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
    mix: soundMuted ? { music_level: 0.0 } : undefined,
    title: title.trim() !== "" ? title.trim() : null,
    base_generation: editorCommitBaseGeneration(variant),
  };
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
      const parsed = (await res.json()) as { detail?: string };
      if (parsed?.detail) detail = parsed.detail;
    } catch {
      /* non-JSON body — keep the generic message */
    }
    throw new Error(detail);
  }
  return (await res.json()) as EditorCommitResponse;
}
