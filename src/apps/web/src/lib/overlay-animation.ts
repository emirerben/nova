/**
 * Client-side animation math — TS mirror of `_draw_with_animation` in
 * text_overlay_skia.py. Computes per-frame animation state for the instant-
 * editor preview playback layer.
 *
 * Parity axis: TS-preview ↔ Skia (distinct from the Skia↔libass burn-field
 * invariant, CLAUDE.md #296). Constants are verbatim from the Python source.
 * Update both when the Python constants change.
 */

export interface AnimationState {
  /** Uniform scale applied at the text block's anchor point. 1.0 = no scale. */
  scale: number;
  /** Opacity 0-1. */
  alpha: number;
  /** Vertical offset in canvas px (1080-wide). Positive = down. */
  yTranslate: number;
  /** Visible text slice (typewriter / stream-in effects). */
  visibleText: string;
  /** Whether a zero-width streaming cursor should be drawn after visibleText. */
  showCursor: boolean;
}

/** Mirror of _ease_out_cubic. t must be in [0,1]; clamped. */
export function easeOutCubic(t: number): number {
  const tc = Math.max(0, Math.min(1, t));
  return 1 - Math.pow(1 - tc, 3);
}

/** Mirror of _clamped_keyframes_s + _pop_in_scale_at. */
export function popInScaleAt(tLocal: number, durationS: number): number {
  // Python constants (verbatim):
  const KF_S = [0.0, 0.150, 0.250]; // _POP_IN_KEYFRAMES_S
  const KF_SCALES = [0.30, 1.15, 1.00]; // _POP_IN_SCALES
  // _clamped_keyframes_s: if duration < last keyframe, scale them all proportionally
  const lastKf = KF_S[KF_S.length - 1];
  const scale_factor = durationS < lastKf ? durationS / lastKf : 1.0;
  const kfS = KF_S.map((k) => k * scale_factor);
  // piecewise linear between keyframes
  if (tLocal <= kfS[0]) return KF_SCALES[0];
  for (let i = 0; i < kfS.length - 1; i++) {
    if (tLocal <= kfS[i + 1]) {
      const p = (tLocal - kfS[i]) / (kfS[i + 1] - kfS[i]);
      return KF_SCALES[i] + p * (KF_SCALES[i + 1] - KF_SCALES[i]);
    }
  }
  return KF_SCALES[KF_SCALES.length - 1];
}

/** Mirror Skia's word-wrapper normalization for fixed-layout reveal effects. */
export function normalizeAnimatedRevealText(text: string): string {
  return text
    .split("\n")
    .map((line) => line.trim().split(/\s+/).filter(Boolean).join(" "))
    .join("\n");
}

/**
 * Mirror of _draw_with_animation — returns the four reduced values without
 * drawing. `durationS` should be MAX_INTRO_S (from overlay-constants.ts).
 * Effects NOT in INTRO_ANIMATIONS (karaoke-line, lyric-line, font-cycle, etc.)
 * return identity state.
 */
export function animationStateAt(
  effect: string,
  tLocal: number,
  durationS: number,
  text: string,
): AnimationState {
  let scale = 1.0;
  let alpha = 1.0;
  let yTranslate = 0.0;
  let visibleText = text;
  let showCursor = false;

  if (effect === "scale-up") {
    const window = durationS > 0.6 ? 0.6 : Math.max(durationS, 0.01);
    const progress = Math.min(1.0, tLocal / window);
    scale = 0.6 + 0.4 * easeOutCubic(progress);
  } else if (effect === "fade-in") {
    const window = durationS > 0.4 ? 0.4 : Math.max(durationS, 0.01);
    const progress = Math.min(1.0, tLocal / window);
    alpha = easeOutCubic(progress);
  } else if (effect === "typewriter") {
    const revealText = normalizeAnimatedRevealText(text);
    const CHARS_PER_S = 12.0;
    const visibleChars = Math.max(1, Math.floor(tLocal * CHARS_PER_S) + 1);
    visibleText = revealText.slice(0, visibleChars);
  } else if (effect === "stream-in") {
    const revealText = normalizeAnimatedRevealText(text);
    const WORDS_PER_S = 6.0;
    const words = Array.from(revealText.matchAll(/\S+/g));
    const n = Math.max(1, Math.floor(tLocal * WORDS_PER_S) + 1);
    const lastVisibleWord = words[Math.min(n, words.length) - 1];
    visibleText = lastVisibleWord
      ? revealText.slice(0, (lastVisibleWord.index ?? 0) + lastVisibleWord[0].length)
      : "";
    if (n < words.length && Math.floor(tLocal * 2) % 2 === 0) {
      showCursor = true;
    }
  } else if (effect === "slide-up" || effect === "slide-down") {
    const animateFor = Math.min(0.35, durationS * 0.5);
    const progress = animateFor > 0 ? Math.min(1.0, tLocal / animateFor) : 1.0;
    const eased = easeOutCubic(progress);
    const direction = effect === "slide-up" ? -1.0 : 1.0;
    yTranslate = direction * 220.0 * (1.0 - eased);
  } else if (effect === "pop-in") {
    scale = popInScaleAt(tLocal, durationS);
  } else if (effect === "bounce") {
    const animateFor = Math.min(0.5, durationS * 0.8);
    if (tLocal < animateFor) {
      const p = tLocal / animateFor;
      if (p < 0.36) {
        scale = 1.0 + 0.25 * (p / 0.36);
      } else if (p < 0.72) {
        scale = 1.25 - (1.25 - 0.90) * ((p - 0.36) / 0.36);
      } else {
        scale = 0.90 + 0.10 * ((p - 0.72) / 0.28);
      }
    }
    // else scale = 1.0 (identity)
  }
  // "none", "static", "karaoke-line", "lyric-line", "font-cycle", unknown → identity

  return { scale, alpha, yTranslate, visibleText, showCursor };
}

/** Mirror of `_sequence_fade_out_alpha` in text_overlay_skia.py.
 * Sequence blocks hold at full opacity until the final `fadeOutMs`, then use
 * libass/Skia's lingering accel=2 curve: `1 - progress²`. */
export function sequenceFadeOutAlphaAt(
  tLocal: number,
  durationS: number,
  fadeOutMs: number | null | undefined,
): number {
  if (durationS <= 0 || !fadeOutMs || fadeOutMs <= 0) return 1;
  const fadeOutS = Math.min(durationS, fadeOutMs / 1000);
  const fadeStartS = durationS - fadeOutS;
  const clampedT = Math.max(0, Math.min(tLocal, durationS));
  if (clampedT < fadeStartS) return 1;
  const progress = (clampedT - fadeStartS) / fadeOutS;
  return Math.max(0, 1 - progress * progress);
}

const SEQUENCE_FADE_EFFECTS = new Set(["fade-in", "static", "none"]);

/** Apply the sequence tail only to the same role/effect matrix as Skia's
 * `_is_sequence_overlay`. Lyric lines carry the same field name but use their
 * own fade-in/out curve and must never pass through this multiplier. */
export function sequenceOverlayFadeOutAlphaAt(
  role: string | null | undefined,
  effect: string | null | undefined,
  tLocal: number,
  durationS: number,
  fadeOutMs: number | null | undefined,
): number {
  if (role !== "generative_sequence" || !SEQUENCE_FADE_EFFECTS.has(effect ?? "none")) {
    return 1;
  }
  return sequenceFadeOutAlphaAt(tLocal, durationS, fadeOutMs);
}
