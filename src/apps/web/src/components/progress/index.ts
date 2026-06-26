/**
 * Barrel export for the progress theater component system.
 */

export { ProgressTheater } from "./ProgressTheater";
export { PhaseChipRow } from "./PhaseChipRow";
export { StatusHeadline } from "./StatusHeadline";
export { EtaBar } from "./EtaBar";
export { VariantRenderCard, ShimmerSweep } from "./VariantRenderCard";
export type { VariantRenderCardVariant } from "./VariantRenderCard";
export { PayoffField } from "./PayoffField";
export { UploadBar } from "./UploadBar";

// Constants
export {
  HEADLINE_CROSSFADE_MS,
  HEADLINE_MIN_DWELL_MS,
  CHIP_TRANSITION_MS,
  ARRIVE_ANIMATION_MS,
  BAR_TRANSITION_MS,
  DAMPING_K,
  SHIMMER_MS,
  PING_MS,
  CELEBRATION_HOLD_MS,
  BAND_COLLAPSE_MS,
  FIELD_TILES_FADE_MS,
  AWAY_NOTE_IN_MS,
  AWAY_NOTE_HOLD_MS,
  AWAY_NOTE_OUT_MS,
  AWAY_HIDDEN_THRESHOLD_MS,
  ETA_LONG_THRESHOLD_S,
  ETA_MID_THRESHOLD_S,
  STALL_TIER1_MULTIPLIER,
  STALL_TIER2_MULTIPLIER,
  POLL_INTERVAL_MS,
  VARIANT_DISPLAY_NAME,
  variantDisplayName,
  ERROR_CLASS_COPY,
  ERROR_FALLBACK_COPY,
  errorCopy,
} from "./constants";

// Logic
export {
  computeBarPosition,
  etaLadder,
  ETA_OVERRUN_COPY,
  stallTier,
  detailLine,
  updateSeenReady,
  shouldShowAwayNote,
  formatElapsed,
} from "./logic";
