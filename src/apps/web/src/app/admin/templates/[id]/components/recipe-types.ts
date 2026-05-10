// ── Recipe domain types ──────────────────────────────────────────────────────

export const TRANSITION_IN_OPTIONS = [
  "hard-cut", "whip-pan", "zoom-in", "dissolve", "curtain-close", "none",
] as const;
export type TransitionIn = (typeof TRANSITION_IN_OPTIONS)[number];

export const COLOR_HINT_OPTIONS = [
  "warm", "cool", "high-contrast", "desaturated", "vintage", "none",
] as const;
export type ColorHint = (typeof COLOR_HINT_OPTIONS)[number];

export const SLOT_TYPE_OPTIONS = ["hook", "broll", "outro"] as const;
export type SlotType = (typeof SLOT_TYPE_OPTIONS)[number];

export const MEDIA_TYPE_OPTIONS = ["video", "photo"] as const;
export type MediaType = (typeof MEDIA_TYPE_OPTIONS)[number];

export const OVERLAY_EFFECT_OPTIONS = [
  "pop-in", "fade-in", "scale-up", "font-cycle", "typewriter",
  "glitch", "bounce", "slide-in", "slide-up", "static", "none",
] as const;
export type OverlayEffect = (typeof OVERLAY_EFFECT_OPTIONS)[number];

export const OVERLAY_POSITION_OPTIONS = ["top", "center", "center-above", "center-label", "center-below", "bottom"] as const;
export type OverlayPosition = (typeof OVERLAY_POSITION_OPTIONS)[number];

export const FONT_STYLE_OPTIONS = [
  "display", "sans", "serif", "serif_italic", "script",
] as const;
export type FontStyle = (typeof FONT_STYLE_OPTIONS)[number];

export const TEXT_SIZE_OPTIONS = ["small", "medium", "large", "xlarge", "xxlarge", "jumbo"] as const;
export type TextSize = (typeof TEXT_SIZE_OPTIONS)[number];

export const OVERLAY_ROLE_OPTIONS = ["hook", "reaction", "cta", "label"] as const;
export type OverlayRole = (typeof OVERLAY_ROLE_OPTIONS)[number];

export const SYNC_STYLE_OPTIONS = [
  "cut-on-beat", "transition-on-beat", "energy-match", "freeform",
] as const;
export type SyncStyle = (typeof SYNC_STYLE_OPTIONS)[number];

export const INTERSTITIAL_TYPE_OPTIONS = [
  "curtain-close", "fade-black-hold", "flash-white",
] as const;
export type InterstitialType = (typeof INTERSTITIAL_TYPE_OPTIONS)[number];

// ── Data structures ─────────────────────────────────────────────────────────

export interface TextSpan {
  text: string;
  font_family?: string;   // Override overlay-level font
  text_color?: string;     // Override overlay-level color (#RRGGBB)
  text_size?: TextSize;    // Override overlay-level size
}

export interface RecipeTextOverlay {
  role: OverlayRole;
  text: string;
  position: OverlayPosition;
  effect: OverlayEffect;
  font_style: FontStyle;
  font_family?: string;  // Overrides font_style when set (real font name from registry)
  text_size: TextSize;
  text_color: string;
  start_s: number;
  end_s: number;
  start_s_override: number | null;
  end_s_override: number | null;
  has_darkening: boolean;
  has_narrowing: boolean;
  sample_text: string;
  font_cycle_accel_at_s: number | null;
  spans?: TextSpan[];     // When set, overrides flat text for rendering
  outline_px?: number | null;  // When set, draws black outline of N pixels around text for legibility
  // Subject substitution opt-in. The renderer slices the user's `inputs.location`
  // value into this overlay's text: "first_half"/"second_half" split at midpoint (ceil),
  // "full" replaces entirely. Casing matches sample_text. Currently invisible in the
  // editor UI — set via backfill scripts; see backend _resolve_overlay_text for semantics.
  subject_part?: "first_half" | "second_half" | "full" | null;
}

export interface RecipeInterstitial {
  type: InterstitialType;
  after_slot: number;
  hold_s: number;
  hold_color: string;
  animate_s: number;
}

export interface RecipeSlot {
  position: number;
  target_duration_s: number;
  priority: number;
  slot_type: SlotType;
  transition_in: TransitionIn;
  color_hint: ColorHint;
  speed_factor: number;
  energy: number;
  media_type: MediaType;
  text_overlays: RecipeTextOverlay[];
}

export interface Recipe {
  shot_count: number;
  total_duration_s: number;
  hook_duration_s: number;
  slots: RecipeSlot[];
  copy_tone: string;
  caption_style: string;
  beat_timestamps_s: number[];
  creative_direction: string;
  transition_style: string;
  color_grade: ColorHint;
  pacing_style: string;
  sync_style: SyncStyle;
  interstitials: RecipeInterstitial[];
}

// ── Editor state ────────────────────────────────────────────────────────────

export interface EditorSelection {
  type: "slot" | "overlay" | "interstitial" | "global";
  slotIndex: number;
  overlayIndex?: number;
  interstitialIndex?: number;
}

export type EditorAction =
  | { type: "LOAD_RECIPE"; recipe: Recipe }
  | { type: "UPDATE_SLOT_FIELD"; slotIndex: number; field: keyof RecipeSlot; value: unknown }
  | {
      type: "UPDATE_OVERLAY_FIELD";
      slotIndex: number;
      overlayIndex: number;
      field: keyof RecipeTextOverlay;
      value: unknown;
    }
  | {
      type: "UPDATE_INTERSTITIAL_FIELD";
      interstitialIndex: number;
      field: keyof RecipeInterstitial;
      value: unknown;
    }
  | { type: "UPDATE_GLOBAL_FIELD"; field: keyof Recipe; value: unknown }
  | { type: "ADD_OVERLAY"; slotIndex: number }
  | { type: "REMOVE_OVERLAY"; slotIndex: number; overlayIndex: number }
  | { type: "ADD_INTERSTITIAL" }
  | { type: "REMOVE_INTERSTITIAL"; interstitialIndex: number }
  | { type: "SET_SELECTED"; selection: EditorSelection | null }
  | { type: "RESET_TO_SAVED"; recipe: Recipe }
  | { type: "SET_VERSION"; versionId: string; versionNumber: number };

export interface EditorState {
  recipe: Recipe | null;
  savedRecipe: Recipe | null;
  selection: EditorSelection | null;
  versionId: string;
  versionNumber: number;
  loading: boolean;
  saving: boolean;
  error: string | null;
}

export const EMPTY_OVERLAY: RecipeTextOverlay = {
  role: "label",
  text: "",
  position: "center",
  effect: "pop-in",
  font_style: "sans",
  text_size: "medium",
  text_color: "#FFFFFF",
  start_s: 0,
  end_s: 2,
  start_s_override: null,
  end_s_override: null,
  has_darkening: false,
  has_narrowing: false,
  sample_text: "",
  font_cycle_accel_at_s: null,
};

export const EMPTY_INTERSTITIAL: RecipeInterstitial = {
  type: "fade-black-hold",
  after_slot: 1,
  hold_s: 0.5,
  hold_color: "#000000",
  animate_s: 0.3,
};
