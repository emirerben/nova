/**
 * Motion constants (D14), ETA/stall thresholds (D18/D19), variant display
 * names (D20), and error-class copy (D10) for the progress theater system.
 *
 * All values are pure data — no React, no DOM.
 */

// ===== D14 Motion values =====
export const HEADLINE_CROSSFADE_MS = 450;
export const HEADLINE_MIN_DWELL_MS = 600;
export const CHIP_TRANSITION_MS = 350;
export const ARRIVE_ANIMATION_MS = 500;
export const BAR_TRANSITION_MS = 500;
export const DAMPING_K = 1.6;
export const SHIMMER_MS = 2200;
export const PING_MS = 1400;
export const CELEBRATION_HOLD_MS = 1200;
export const BAND_COLLAPSE_MS = 650;
export const FIELD_TILES_FADE_MS = 500;
export const AWAY_NOTE_IN_MS = 400;
export const AWAY_NOTE_HOLD_MS = 3500;
export const AWAY_NOTE_OUT_MS = 400;
/** D8: show away-note only if hidden longer than this threshold. */
export const AWAY_HIDDEN_THRESHOLD_MS = 5000;

// ===== D18 ETA ladder thresholds =====
/** >=90s → "~N min left" */
export const ETA_LONG_THRESHOLD_S = 90;
/** 25–90s → "about a minute left"; <25s → "less than a minute…" */
export const ETA_MID_THRESHOLD_S = 25;

// ===== D19 stall thresholds =====
export const STALL_TIER1_MULTIPLIER = 1.5;
export const STALL_TIER2_MULTIPLIER = 2.5;

// ===== Poll cadence =====
export const POLL_INTERVAL_MS = 2000;

// ===== D20 Variant display names =====
export const VARIANT_DISPLAY_NAME: Record<string, string> = {
  song_lyrics: "Song Lyrics",
  song_text: "Song Text",
  original_text: "Original",
  voiceover_only: "Voiceover",
  voiceover_music: "Voiceover + Music",
  talking_head: "Talking Head",
};

/** Human-readable variant name. Falls back to id with underscores replaced. */
export function variantDisplayName(id: string): string {
  return VARIANT_DISPLAY_NAME[id] ?? id.replace(/_/g, " ");
}

// ===== D10 Error class → human copy =====
export const ERROR_CLASS_COPY: Record<string, string> = {
  timeout: "This render took too long and was stopped",
  encoder_error: "Something went wrong while encoding this video",
  clip_read_error: "We couldn't read one of your clips",
  storage_error: "A storage error interrupted this render",
  match_failed: "We couldn't find a matching song for this edit",
};

export const ERROR_FALLBACK_COPY = "Something went wrong with this edit";

/** Human-readable error copy from an error_class string, with safe fallback. */
export function errorCopy(errorClass?: string | null): string {
  if (!errorClass) return ERROR_FALLBACK_COPY;
  return ERROR_CLASS_COPY[errorClass] ?? ERROR_FALLBACK_COPY;
}
