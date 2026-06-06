/**
 * Unified phase vocabulary for all Nova job types.
 *
 * - Generative phases defined here (GENERATIVE_PHASE_ORDER, GENERATIVE_PHASE_LABEL).
 * - Template phases re-exported from template-job-phases.ts (that module remains
 *   the source of truth — REGRESSION invariant: template screen must compile untouched).
 * - Shared math helpers: computeAnchors, dampedPos.
 */

// ===== GENERATIVE PHASES =====
export const GENERATIVE_PHASE_ORDER = [
  "queued",
  "analyze_clips",
  "match_song",
  "render_variants",
  "finalize",
] as const;
export type GenerativePhase = (typeof GENERATIVE_PHASE_ORDER)[number];

export const GENERATIVE_PHASE_LABEL: Record<GenerativePhase, string> = {
  queued: "Waiting for a render slot",
  analyze_clips: "Analyzing your clips",
  match_song: "Selecting the perfect song",
  render_variants: "Rendering your edits",
  finalize: "Wrapping up",
};

// ===== TEMPLATE PHASES (re-export from legacy location, exact names preserved) =====
// NOTE: template-job-phases.ts is the source of truth — DO NOT duplicate the logic here.
// The template page imports from that module; this file re-exports it for
// the new unified system. (REGRESSION invariant: template screen must compile untouched)
export type { JobPhase as TemplatePhase } from "./template-job-phases";
export {
  PHASE_ORDER as TEMPLATE_PHASE_ORDER,
  PHASE_LABEL as TEMPLATE_PHASE_LABEL,
  phaseProgress as templatePhaseProgress, // DEPRECATED: index-derived, use duration-weighted anchors
  humanisePhase,
  formatElapsedMs,
  splitPhaseLog,
} from "./template-job-phases";

// ===== DURATION-WEIGHTED ANCHOR HELPERS =====

/**
 * Compute bar anchor positions from expected_phase_durations.
 * Returns a map from phase name → [fromFraction, toFraction].
 * Falls back to equal-width slices when durations are absent.
 */
export function computeAnchors(
  phaseOrder: readonly string[],
  expectedMs: Record<string, number> | null | undefined,
): Record<string, [number, number]> {
  const n = phaseOrder.length;
  if (n === 0) return {};

  // Determine weights: use expectedMs if present and all entries are positive,
  // otherwise fall back to equal-width slices.
  const weights: number[] = phaseOrder.map((phase) => {
    const ms = expectedMs?.[phase];
    return typeof ms === "number" && ms > 0 ? ms : 0;
  });

  const totalWeight = weights.reduce((a, b) => a + b, 0);
  const useEqual = totalWeight <= 0;

  const result: Record<string, [number, number]> = {};
  let cursor = 0;
  for (let i = 0; i < n; i++) {
    const phase = phaseOrder[i];
    const slice = useEqual ? 1 / n : weights[i] / totalWeight;
    result[phase] = [cursor, cursor + slice];
    cursor += slice;
  }
  // Clamp last phase end to exactly 1.0 to absorb floating-point drift.
  const last = phaseOrder[n - 1];
  if (result[last]) {
    result[last] = [result[last][0], 1.0];
  }
  return result;
}

/**
 * Damped bar position between two anchor points.
 * Pure function of timestamps — NEVER accumulated animation state.
 * D11: pos = from + (to - from) * (1 - e^(-1.6 * elapsed / baseline))
 * Monotone: never below fromFraction.
 *
 * @param fromFraction  Start of the anchor window (0..1)
 * @param toFraction    End of the anchor window (0..1)
 * @param elapsedInPhaseMs  Milliseconds elapsed since the phase event landed
 * @param baselineMs    Expected duration of this phase in ms (must be > 0)
 */
export function dampedPos(
  fromFraction: number,
  toFraction: number,
  elapsedInPhaseMs: number,
  baselineMs: number,
): number {
  if (baselineMs <= 0) return fromFraction;
  if (elapsedInPhaseMs <= 0) return fromFraction;
  const DAMPING_K = 1.6;
  const t = elapsedInPhaseMs / baselineMs;
  const progress = 1 - Math.exp(-DAMPING_K * t);
  const pos = fromFraction + (toFraction - fromFraction) * progress;
  // Monotone: never exceed toFraction (asymptotic) and never go below fromFraction.
  return Math.max(fromFraction, Math.min(toFraction - Number.EPSILON, pos));
}
