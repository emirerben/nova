import type { CarouselMoment } from "@/lib/generative-api";

export const CAROUSEL_DURATION_MIN_S = 2;
export const CAROUSEL_DURATION_MAX_S = 15;
export const CAROUSEL_HOLD_MIN_S = 0.5;
export const CAROUSEL_HOLD_MAX_S = 5;
export const CAROUSEL_MOVE_MIN_S = 0.2;
export const CAROUSEL_MOVE_MAX_S = 4;
export const CAROUSEL_ZOOM_MIN_S = 0.2;
export const CAROUSEL_ZOOM_MAX_S = 2;
export const CAROUSEL_BOUNDARY_MIN_S = 0.1;
export const CAROUSEL_BOUNDARY_MAX_S = 1;

export function roundCarouselSeconds(value: number): number {
  return Math.round(value * 10) / 10;
}

/** Static legacy moments have no manual choreography to upgrade. They remain
 * byte-identical until the user explicitly chooses Focus or Rolling. */
export function shouldAutoUpgradeCarouselTiming(moment: CarouselMoment): boolean {
  return (
    moment.timing_model !== "ripple_v1" &&
    (moment.mode as string | undefined) !== "stills"
  );
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, roundCarouselSeconds(value)));
}

function carouselMoveCount(
  sequence: NonNullable<CarouselMoment["sequence"]>,
  initialClipIndex: number,
): number {
  let previousClipIndex = initialClipIndex;
  let moves = 0;
  for (const item of sequence) {
    if (item.clip_index !== previousClipIndex) moves += 1;
    previousClipIndex = item.clip_index;
  }
  return moves;
}

function rawCarouselChoreographyDuration(
  moment: CarouselMoment,
  activeClipIndices?: readonly number[],
): number {
  const sequence = moment.sequence ?? [];
  const moveS = moment.move_duration_s ?? 0.6;
  const zoomS = moment.mode === "rolling" ? 0 : (moment.zoom_duration_s ?? 0.6) * 2;
  return roundCarouselSeconds(
    sequence.reduce((total, item) => total + item.hold_s + zoomS, 0) +
      carouselMoveCount(sequence, activeClipIndices?.[0] ?? sequence[0]?.clip_index ?? 0) * moveS,
  );
}

function scaleCarouselPhases(moment: CarouselMoment, ratio: number): CarouselMoment {
  return {
    ...moment,
    sequence:
      moment.sequence?.map((item) => ({
        ...item,
        hold_s: clamp(item.hold_s * ratio, CAROUSEL_HOLD_MIN_S, CAROUSEL_HOLD_MAX_S),
      })) ?? moment.sequence,
    move_duration_s:
      moment.move_duration_s == null
        ? undefined
        : clamp(moment.move_duration_s * ratio, CAROUSEL_MOVE_MIN_S, CAROUSEL_MOVE_MAX_S),
    zoom_duration_s:
      moment.zoom_duration_s == null
        ? undefined
        : clamp(moment.zoom_duration_s * ratio, CAROUSEL_ZOOM_MIN_S, CAROUSEL_ZOOM_MAX_S),
  };
}

function absorbRoundingInHolds(
  moment: CarouselMoment,
  targetS: number,
  activeClipIndices?: readonly number[],
): CarouselMoment {
  const sequence = moment.sequence?.map((item) => ({ ...item })) ?? [];
  if (sequence.length === 0) return moment;
  let remainingSteps = Math.round(
    (targetS - rawCarouselChoreographyDuration(moment, activeClipIndices)) * 10,
  );
  let cursor = 0;
  while (remainingSteps !== 0 && cursor < sequence.length * 50) {
    const item = sequence[cursor % sequence.length];
    const direction = Math.sign(remainingSteps) * 0.1;
    const nextHold = roundCarouselSeconds(item.hold_s + direction);
    if (nextHold >= CAROUSEL_HOLD_MIN_S && nextHold <= CAROUSEL_HOLD_MAX_S) {
      item.hold_s = nextHold;
      remainingSteps -= Math.sign(remainingSteps);
    }
    cursor += 1;
  }
  return { ...moment, sequence };
}

/** Upgrade an existing sparse/legacy moment when it is opened in the editor.
 * Legacy render behavior is preserved until this function is explicitly used. */
export function upgradeCarouselTiming(
  moment: CarouselMoment,
  clipIndices: readonly number[],
): CarouselMoment {
  if (moment.timing_model === "ripple_v1") return moment;
  // The upgraded sequence represents every participating video in timeline
  // order. `focus_clip_index` remains available as a legacy read/write input,
  // but the explicit sequence is authoritative for ripple_v1 in both Focus
  // and Rolling modes.
  const sequenceIndices = clipIndices.length > 0 ? clipIndices : [0];
  const moveDurationS = 0.6;
  const zoomDurationS = 0.6;
  const moveCount = carouselMoveCount(
    sequenceIndices.map((clip_index) => ({ clip_index, hold_s: 0 })),
    sequenceIndices[0],
  );
  const fixedPhaseS =
    moveCount * moveDurationS +
    (moment.mode === "rolling" ? 0 : sequenceIndices.length * zoomDurationS * 2);
  const perClipHold = clamp(
    ((moment.duration_s ?? 6) - fixedPhaseS) / Math.max(1, sequenceIndices.length),
    CAROUSEL_HOLD_MIN_S,
    CAROUSEL_HOLD_MAX_S,
  );
  const upgraded: CarouselMoment = {
    ...moment,
    position: moment.position ?? "middle",
    timing_model: "ripple_v1",
    sequence: sequenceIndices.map((clip_index) => ({ clip_index, hold_s: perClipHold })),
    move_duration_s: moveDurationS,
    zoom_duration_s: zoomDurationS,
    transition_in: moment.transition ?? "crossfade",
    transition_in_duration_s: 0.4,
    transition_out: moment.transition ?? "crossfade",
    transition_out_duration_s: 0.4,
  };
  return {
    ...upgraded,
    // A manual outer duration must contain every authored phase. Small
    // rounding gaps are eliminated here so opening a legacy moment cannot
    // immediately truncate its final video.
    duration_s: carouselChoreographyDuration(upgraded, sequenceIndices),
  };
}

export function carouselChoreographyDuration(
  moment: CarouselMoment,
  activeClipIndices?: readonly number[],
): number {
  return clamp(
    rawCarouselChoreographyDuration(moment, activeClipIndices),
    CAROUSEL_DURATION_MIN_S,
    CAROUSEL_DURATION_MAX_S,
  );
}

/** Proportionally stretch the authored phases. The block duration remains the
 * authoritative outer frame count; individual values use 0.1s editor steps. */
export function resizeCarouselTiming(
  moment: CarouselMoment,
  targetDurationS: number,
  activeClipIndices?: readonly number[],
): CarouselMoment {
  const target = clamp(targetDurationS, CAROUSEL_DURATION_MIN_S, CAROUSEL_DURATION_MAX_S);
  const currentNatural = Math.max(
    CAROUSEL_DURATION_MIN_S,
    rawCarouselChoreographyDuration(moment, activeClipIndices),
  );
  let scaled = scaleCarouselPhases(moment, target / currentNatural);
  // Safe phase floors can make a requested contraction physically
  // impossible. Never truncate the authored ending: stop the handle at the
  // shortest complete choreography instead. Re-applying the ratio also
  // absorbs 0.1s per-phase rounding without privileging one card's hold.
  for (let pass = 0; pass < 4; pass += 1) {
    const natural = rawCarouselChoreographyDuration(scaled, activeClipIndices);
    if (natural === target || natural <= 0) break;
    scaled = scaleCarouselPhases(scaled, target / natural);
  }
  scaled = absorbRoundingInHolds(scaled, target, activeClipIndices);
  // Phase ceilings can make an expansion impossible (for example, one
  // Rolling card tops out at its 5s hold). The block must report the complete
  // choreography it can actually render instead of padding to the requested
  // handle position and snapping back on Save.
  const completeDuration = rawCarouselChoreographyDuration(scaled, activeClipIndices);
  return {
    ...scaled,
    duration_s: clamp(
      completeDuration,
      CAROUSEL_DURATION_MIN_S,
      CAROUSEL_DURATION_MAX_S,
    ),
  };
}

export function effectiveBoundaryDuration(
  requestedS: number | undefined,
  beforeDurationS: number,
  afterDurationS: number,
): number {
  return Math.min(
    clamp(requestedS ?? 0.4, CAROUSEL_BOUNDARY_MIN_S, CAROUSEL_BOUNDARY_MAX_S),
    Math.max(0, beforeDurationS) * 0.3,
    Math.max(0, afterDurationS) * 0.3,
  );
}
