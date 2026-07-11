/**
 * plan-generate-gate — the ONE decision for the plan-item Generate button.
 *
 * The button's disabled state and the hint line under it must never disagree
 * (the "Record your voiceover first" bug lived in two divergent inline
 * expressions on page.tsx). Both are computed here, from the same inputs,
 * and unit-tested in __tests__/plan/plan-generate-gate.test.ts.
 *
 *          inputs                              outputs
 *   ┌──────────────────────┐        ┌──────────────────────────────┐
 *   │ busy flags            │        │ disabled: boolean            │
 *   │ clipCount             │──────▶│ hint: string | null          │
 *   │ isNarrated            │        │   (always explains WHY the   │
 *   │ hasVoiceover          │        │    button is off, or what     │
 *   │ selfNarrationEnabled  │        │    will drive the edit)       │
 *   │ isInstructed/shotsLeft│        │                               │
 *   └──────────────────────┘        └──────────────────────────────┘
 *
 * Self-narration (NEXT_PUBLIC_NARRATED_SELF_NARRATION_ENABLED): a narrated
 * item with no recorded voiceover may generate from the footage's own audio.
 * A recorded voiceover always wins server-side, so the hint keeps offering it.
 */

export interface GenerateGateInput {
  /** POST /generate is in flight (button just clicked). */
  generating: boolean;
  /** The item's derived status says a render is running. */
  isGenerating: boolean;
  /** A clip upload is still finishing. */
  uploaderBusy: boolean;
  clipCount: number;
  /** edit_format is one of the narrated family. */
  isNarrated: boolean;
  /** voiceover_gcs_path present (recorded or uploaded). */
  hasVoiceover: boolean;
  /** NEXT_PUBLIC_NARRATED_SELF_NARRATION_ENABLED === "true". */
  selfNarrationEnabled: boolean;
  /** Shot-slot flow: show the "N shots left" nudge. */
  isInstructed: boolean;
  shotsLeft: number;
}

export interface GenerateGateResult {
  disabled: boolean;
  hint: string | null;
}

export const VOICEOVER_REQUIRED_HINT =
  "Record your voiceover first — narration drives the edit";
export const SELF_NARRATION_HINT =
  "No voiceover? We'll use your video's own narration — or record one above to drive the edit.";
// Shared with the button label in page.tsx — one string, no silent divergence.
export const FINISHING_UPLOAD_HINT = "Finishing upload…";

export function generateGate(input: GenerateGateInput): GenerateGateResult {
  const {
    generating,
    isGenerating,
    uploaderBusy,
    clipCount,
    isNarrated,
    hasVoiceover,
    selfNarrationEnabled,
    isInstructed,
    shotsLeft,
  } = input;

  // Voiceover is only a hard requirement while self-narration is off.
  const voiceoverBlocked = isNarrated && !hasVoiceover && !selfNarrationEnabled;

  const disabled =
    generating || clipCount === 0 || isGenerating || uploaderBusy || voiceoverBlocked;

  let hint: string | null = null;
  if (uploaderBusy) {
    hint = FINISHING_UPLOAD_HINT;
  } else if (voiceoverBlocked) {
    hint = VOICEOVER_REQUIRED_HINT;
  } else if (clipCount === 0) {
    hint = "Add clips to generate";
  } else if (isInstructed && shotsLeft > 0) {
    // Shot-slot progress outranks the self-narration explainer: while slots are
    // unfilled, "N shots left" is the actionable next step; the self-narration
    // hint appears once the slots are full.
    hint = `${shotsLeft} shot${shotsLeft !== 1 ? "s" : ""} left`;
  } else if (isNarrated && !hasVoiceover) {
    // Self-narration path: proceeding is allowed; say what will drive the edit.
    hint = SELF_NARRATION_HINT;
  }

  return { disabled, hint };
}

/** Shape of GET /generative-jobs/{id}/status → archetype_fallback. */
export interface ArchetypeFallback {
  declared?: string | null;
  reason?: string | null;
}

export const NO_SPEECH_BANNER =
  "We couldn't hear narration in your clips, so we made a montage instead — record a voiceover, or upload a clip where you speak, to get the voiceover style.";
export const SPINE_FAILED_BANNER =
  "One of your clips couldn't be read, so we made a montage instead. Re-uploading that clip usually fixes it.";
export const GENERIC_FALLBACK_BANNER =
  "We made a montage instead of the voiceover style this time — record a voiceover to make sure narration drives the edit.";

/**
 * Banner copy for a narrated item whose render fell back to montage.
 * Null → no banner. Only narrated items surface this (other declared formats
 * keep the admin-only trace for now — deliberate v1 scope).
 *
 * ANY truthy reason on a narrated item shows a banner: known reasons get
 * specific copy, unknown ones the generic downgrade line. An unmapped reason
 * must never be silent — a quiet style swap is the original dogfood bug this
 * module exists to prevent (e.g. `archetype_not_implemented` during an
 * api-flipped/worker-stale flag window).
 */
export function narrationFallbackBanner(
  isNarrated: boolean,
  fallback: ArchetypeFallback | null | undefined,
): string | null {
  if (!isNarrated || !fallback?.reason) return null;
  if (fallback.reason === "no_speech") return NO_SPEECH_BANNER;
  if (fallback.reason === "spine_extraction_failed") return SPINE_FAILED_BANNER;
  return GENERIC_FALLBACK_BANNER;
}
