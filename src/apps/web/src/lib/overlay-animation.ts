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
  /** Horizontal scale origin offset from text anchor in canvas px. */
  scaleOriginX: number;
  /** Vertical scale origin offset from text anchor in canvas px. */
  scaleOriginY: number;
  /** Visible text slice (typewriter / stream-in effects). */
  visibleText: string;
  /** Whether a zero-width streaming cursor should be drawn after visibleText. */
  showCursor: boolean;
}

export interface ThemeTransition {
  type?: string | null;
  target_glyph?: string | null;
}

export interface StaggeredSliceGlyphState {
  grapheme: string;
  opacity: number;
  translateYEm: number;
  rotateDeg: number;
}

export interface StaggeredSliceLineState {
  text: string;
  kind: "glyphs";
  glyphs: StaggeredSliceGlyphState[];
}

export interface StaggeredSliceState {
  lines: StaggeredSliceLineState[];
  settled: boolean;
  settleS: number;
}

const STAGGERED_SLICE_FIRST_WORD_END_S = 0.55;
const STAGGERED_SLICE_REMAINDER_START_S = 0.95;
const STAGGERED_SLICE_REMAINDER_END_S = 1.35;
const STAGGERED_SLICE_LINES_START_S = 1.5;
const STAGGERED_SLICE_LINE_STAGGER_S = 0.12;
const STAGGERED_SLICE_LINE_GLYPH_STAGE_S = 0.35;
const STAGGERED_SLICE_GLYPH_DURATION_S = 0.16;
const STAGGERED_SLICE_MAX_SETTLE_S = 2.4;
export const STAGGERED_SLICE_PREMOUNT_S = 0.35;
const GRAPHEME_SEGMENTER =
  typeof Intl !== "undefined" && "Segmenter" in Intl
    ? new Intl.Segmenter(undefined, { granularity: "grapheme" })
    : null;

/**
 * Keep the invisible preview node mounted across the effect's start boundary.
 * Native video `timeupdate` can be ~265ms apart, so mounting only after start
 * skips the earliest glyph frames even though the node animates smoothly once
 * present.
 */
export function staggeredSlicePreviewVisibleAt(
  tLocal: number,
  durationS: number,
  playing: boolean,
): boolean {
  return (
    tLocal < Math.max(0.01, durationS) &&
    (tLocal >= 0 || (playing && tLocal >= -STAGGERED_SLICE_PREMOUNT_S))
  );
}

/** User-visible character segmentation shared with Python's regex `\X` path. */
export function segmentGraphemes(text: string): string[] {
  if (GRAPHEME_SEGMENTER) {
    return Array.from(GRAPHEME_SEGMENTER.segment(text), ({ segment }) => segment);
  }
  return Array.from(text);
}

function staggeredSliceChoreographyS(text: string): number {
  const lineCount = Math.max(1, normalizeAnimatedRevealText(text).split("\n").length);
  if (lineCount === 1) return STAGGERED_SLICE_REMAINDER_END_S;
  const lastLineStart =
    STAGGERED_SLICE_LINES_START_S + Math.max(0, lineCount - 2) * STAGGERED_SLICE_LINE_STAGGER_S;
  return lastLineStart + STAGGERED_SLICE_LINE_GLYPH_STAGE_S;
}

export function staggeredSliceSettleS(text: string): number {
  return Math.min(STAGGERED_SLICE_MAX_SETTLE_S, staggeredSliceChoreographyS(text));
}

function staggeredSliceNominalTime(
  tLocal: number,
  durationS: number,
  settleS: number,
  choreographyS: number,
): number {
  const available = Math.max(0.01, Math.min(durationS, settleS));
  const scale = available / choreographyS;
  return Math.max(0, tLocal) / scale;
}

function timedProgress(t: number, start: number, duration: number): number {
  return easeOutCubic((t - start) / Math.max(0.01, duration));
}

/** Deterministic motion model mirrored by `_staggered_slice_state` in Skia. */
export function staggeredSliceStateAt(
  text: string,
  tLocal: number,
  durationS: number,
): StaggeredSliceState {
  const normalized = normalizeAnimatedRevealText(text);
  const logicalLines = normalized.split("\n");
  const choreographyS = staggeredSliceChoreographyS(normalized);
  const settleS = staggeredSliceSettleS(normalized);
  const t = staggeredSliceNominalTime(tLocal, durationS, settleS, choreographyS);

  const lines = logicalLines.map<StaggeredSliceLineState>((line, lineIndex) => {
    if (lineIndex === 0) {
      const glyphs = segmentGraphemes(line);
      const firstSpace = glyphs.findIndex((glyph) => /^\s+$/.test(glyph));
      const firstWordCount = firstSpace === -1 ? glyphs.length : firstSpace;
      const remainderCount = Math.max(0, glyphs.length - firstWordCount);
      const firstStagger =
        firstWordCount <= 1
          ? 0
          : Math.min(
              0.07,
              (STAGGERED_SLICE_FIRST_WORD_END_S - STAGGERED_SLICE_GLYPH_DURATION_S) /
                (firstWordCount - 1),
            );
      const remainderStagger =
        remainderCount <= 1
          ? 0
          : (STAGGERED_SLICE_REMAINDER_END_S -
              STAGGERED_SLICE_REMAINDER_START_S -
              STAGGERED_SLICE_GLYPH_DURATION_S) /
            (remainderCount - 1);

      return {
        text: line,
        kind: "glyphs",
        glyphs: glyphs.map((grapheme, glyphIndex) => {
          const inFirstWord = glyphIndex < firstWordCount;
          const start = inFirstWord
            ? glyphIndex * firstStagger
            : STAGGERED_SLICE_REMAINDER_START_S +
              (glyphIndex - firstWordCount) * remainderStagger;
          const progress = timedProgress(t, start, STAGGERED_SLICE_GLYPH_DURATION_S);
          return {
            grapheme,
            opacity: progress,
            translateYEm: 0.18 * (1 - progress),
            rotateDeg: (glyphIndex % 2 === 0 ? -4 : 4) * (1 - progress),
          };
        }),
      };
    }

    const lineStart =
      STAGGERED_SLICE_LINES_START_S + (lineIndex - 1) * STAGGERED_SLICE_LINE_STAGGER_S;
    const glyphs = segmentGraphemes(line);
    const glyphStagger =
      glyphs.length <= 1
        ? 0
        : (STAGGERED_SLICE_LINE_GLYPH_STAGE_S - STAGGERED_SLICE_GLYPH_DURATION_S) /
          (glyphs.length - 1);
    return {
      text: line,
      kind: "glyphs",
      glyphs: glyphs.map((grapheme, glyphIndex) => {
        const progress = timedProgress(
          t,
          lineStart + glyphIndex * glyphStagger,
          STAGGERED_SLICE_GLYPH_DURATION_S,
        );
        return {
          grapheme,
          opacity: progress,
          translateYEm: 0.18 * (1 - progress),
          rotateDeg: (glyphIndex % 2 === 0 ? -4 : 4) * (1 - progress),
        };
      }),
    };
  });

  return { lines, settled: t + 1e-9 >= choreographyS, settleS };
}

/** Mirror of _ease_out_cubic. t must be in [0,1]; clamped. */
export function easeOutCubic(t: number): number {
  const tc = Math.max(0, Math.min(1, t));
  return 1 - Math.pow(1 - tc, 3);
}

/** Mirror of _ease_in_out_cubic. t must be in [0,1]; clamped. */
export function easeInOutCubic(t: number): number {
  const tc = Math.max(0, Math.min(1, t));
  if (tc < 0.5) return 4 * Math.pow(tc, 3);
  return 1 - Math.pow(-2 * tc + 2, 3) / 2;
}

/** Mirror of _motion_cubic_bezier. */
export function motionCubicBezier(
  t: number,
  x1: number,
  y1: number,
  x2: number,
  y2: number,
): number {
  const targetX = Math.max(0, Math.min(1, t));
  if (targetX <= 0) return 0;
  if (targetX >= 1) return 1;

  const sample = (axis1: number, axis2: number, u: number): number => {
    const inv = 1 - u;
    return 3 * axis1 * inv * inv * u + 3 * axis2 * inv * u * u + Math.pow(u, 3);
  };
  const sampleX = (u: number): number => sample(x1, x2, u);
  const sampleY = (u: number): number => sample(y1, y2, u);
  const sampleXDerivative = (u: number): number => {
    const inv = 1 - u;
    return 3 * x1 * inv * inv + 6 * (x2 - x1) * inv * u + 3 * (1 - x2) * u * u;
  };

  let u = targetX;
  for (let i = 0; i < 8; i += 1) {
    const error = sampleX(u) - targetX;
    if (Math.abs(error) < 1e-6) return sampleY(u);
    const derivative = sampleXDerivative(u);
    if (Math.abs(derivative) < 1e-6) break;
    u = Math.max(0, Math.min(1, u - error / derivative));
  }

  let lower = 0;
  let upper = 1;
  u = targetX;
  for (let i = 0; i < 12; i += 1) {
    if (sampleX(u) < targetX) lower = u;
    else upper = u;
    u = (lower + upper) / 2;
  }
  return sampleY(u);
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

/** Mirror of _giant_title_wipe_scale_at. Holds, then scales into a title wipe. */
export function giantTitleWipeScaleAt(tLocal: number, durationS: number): number {
  const holdUntil = Math.max(0, durationS) * 0.68;
  const wipeFor = Math.max(0.01, Math.max(0, durationS) - holdUntil);
  const progress = motionCubicBezier((tLocal - holdUntil) / wipeFor, 0.76, 0.0, 0.24, 1.0);
  return 1.0 + (60.0 - 1.0) * progress;
}

/** Mirror of _giant_title_wipe_alpha_at. Removes the letter after passing through it. */
export function giantTitleWipeAlphaAt(tLocal: number, durationS: number): number {
  const holdUntil = Math.max(0, durationS) * 0.68;
  const wipeFor = Math.max(0.01, Math.max(0, durationS) - holdUntil);
  const wipeProgress = (tLocal - holdUntil) / wipeFor;
  if (wipeProgress <= 0.8) return 1.0;
  const fadeProgress = (wipeProgress - 0.8) / (1.0 - 0.8);
  return 1.0 - easeOutCubic(fadeProgress);
}

/** Mirror of _giant_title_wipe_scale_origin. Offsets target the selected O counter. */
export function giantTitleWipeScaleOrigin(): { scaleOriginX: number; scaleOriginY: number } {
  return {
    scaleOriginX: 13.0,
    scaleOriginY: -80.0,
  };
}

function identityAnimationState(text: string): AnimationState {
  return {
    scale: 1.0,
    alpha: 1.0,
    yTranslate: 0.0,
    scaleOriginX: 0.0,
    scaleOriginY: 0.0,
    visibleText: text,
    showCursor: false,
  };
}

/**
 * Theme-transition layer, independent from text entrance `effect`.
 * Mirror of text_overlay_skia._theme_transition_canvas.
 */
export function themeTransitionStateAt(
  themeTransition: ThemeTransition | string | null | undefined,
  tLocal: number,
  durationS: number,
): AnimationState {
  const transitionType =
    typeof themeTransition === "string" ? themeTransition : themeTransition?.type;
  const state = identityAnimationState("");
  if (transitionType !== "giant-title-wipe") return state;
  state.scale = giantTitleWipeScaleAt(tLocal, durationS);
  state.alpha = giantTitleWipeAlphaAt(tLocal, durationS);
  const origin = giantTitleWipeScaleOrigin();
  state.scaleOriginX = origin.scaleOriginX;
  state.scaleOriginY = origin.scaleOriginY;
  return state;
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
  let { scale, alpha, yTranslate, scaleOriginX, scaleOriginY, visibleText, showCursor } =
    identityAnimationState(text);

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

  return { scale, alpha, yTranslate, scaleOriginX, scaleOriginY, visibleText, showCursor };
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
