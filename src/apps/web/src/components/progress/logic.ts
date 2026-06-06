/**
 * Pure math functions for the progress theater.
 * NO DOM, NO React — fully Jest-testable without jsdom.
 */

import {
  ETA_LONG_THRESHOLD_S,
  ETA_MID_THRESHOLD_S,
  STALL_TIER1_MULTIPLIER,
  STALL_TIER2_MULTIPLIER,
  AWAY_HIDDEN_THRESHOLD_MS,
  variantDisplayName,
} from "./constants";
import { dampedPos } from "../../lib/job-phases";

// ===== Bar position =====

/**
 * D11: damped bar position.
 * Monotone, pure fn of timestamps — never accumulates animation state.
 *
 * @param fromFraction   Anchor start (0..1)
 * @param toFraction     Anchor end (0..1)
 * @param lastEventAt    Timestamp (Date.now() style) when the phase event arrived
 * @param now            Current timestamp
 * @param baselineMs     Expected phase duration in ms
 */
export function computeBarPosition(
  fromFraction: number,
  toFraction: number,
  lastEventAt: number,
  now: number,
  baselineMs: number,
): number {
  const elapsed = Math.max(0, now - lastEventAt);
  const pos = dampedPos(fromFraction, toFraction, elapsed, baselineMs);
  // Clamp strictly: never below from, never at-or-above to.
  return Math.max(fromFraction, Math.min(toFraction - Number.EPSILON, pos));
}

// ===== ETA ladder =====

/** D18 ETA overrun copy — shown when elapsed > total baseline. */
export const ETA_OVERRUN_COPY = "almost there — taking a bit longer than usual";

/**
 * D18: ETA text from remaining milliseconds.
 * Returns null when ETA is unavailable.
 */
export function etaLadder(remainingMs: number | null): string | null {
  if (remainingMs == null || !Number.isFinite(remainingMs)) return null;
  const remainingS = remainingMs / 1000;
  if (remainingS >= ETA_LONG_THRESHOLD_S) {
    const mins = Math.round(remainingS / 60);
    return `~${mins} min left`;
  }
  if (remainingS >= ETA_MID_THRESHOLD_S) {
    return "about a minute left";
  }
  return "less than a minute…";
}

// ===== Stall tier =====

/**
 * D19: Stall tier.
 * Returns 0=normal, 1=mild stall, 2=escalated stall.
 */
export function stallTier(
  elapsedInPhaseMs: number,
  baselineMs: number | null,
): 0 | 1 | 2 {
  if (!baselineMs || baselineMs <= 0) return 0;
  const ratio = elapsedInPhaseMs / baselineMs;
  if (ratio >= STALL_TIER2_MULTIPLIER) return 2;
  if (ratio >= STALL_TIER1_MULTIPLIER) return 1;
  return 0;
}

// ===== Variant detail line =====

interface VariantLike {
  variant_id: string;
  render_status: string | null;
}

/**
 * D20: Detail line copy derived purely from variants render_status.
 * No constants — numbers come from the variants array length.
 */
export function detailLine(
  variants: VariantLike[] | null | undefined,
): string {
  if (!variants || variants.length === 0) return "";

  const total = variants.length;
  const ready = variants.filter((v) => v.render_status === "ready").length;
  const rendering = variants.filter((v) => v.render_status === "rendering");
  const renderingCount = rendering.length;

  if (ready === total) return "";

  if (renderingCount === 1) {
    const name = variantDisplayName(rendering[0].variant_id);
    return `Rendering the ${name} edit…`;
  }
  if (renderingCount > 1) {
    return `Rendering ${renderingCount} edits…`;
  }

  // Nothing actively rendering (queued/pending state)
  if (ready > 0) {
    return `${ready} of ${total} ready`;
  }
  return "";
}

// ===== Seen-ready tracking =====

/**
 * Returns a new Set containing all previously seen ready ids plus any newly
 * ready variant ids. Grows monotonically — ids are never removed.
 */
export function updateSeenReady(
  prev: ReadonlySet<string>,
  variants: VariantLike[] | null | undefined,
): Set<string> {
  const next = new Set(prev);
  for (const v of variants ?? []) {
    if (v.render_status === "ready") {
      next.add(v.variant_id);
    }
  }
  return next;
}

// ===== Away-note visibility =====

/**
 * D8: Determine whether the away-note should be shown.
 * Condition: the page was hidden for longer than AWAY_HIDDEN_THRESHOLD_MS
 * AND at least one new variant became ready while hidden.
 */
export function shouldShowAwayNote(
  hiddenAtMs: number | null,
  seenReadyBeforeHide: ReadonlySet<string>,
  seenReadyNow: ReadonlySet<string>,
): boolean {
  if (hiddenAtMs == null) return false;
  const hiddenDuration = Date.now() - hiddenAtMs;
  if (hiddenDuration < AWAY_HIDDEN_THRESHOLD_MS) return false;
  // Check if any new ids appeared since hiding.
  // Use Array.from for ES2015 compat (avoid --downlevelIteration requirement).
  const nowArray = Array.from(seenReadyNow);
  for (let i = 0; i < nowArray.length; i++) {
    if (!seenReadyBeforeHide.has(nowArray[i])) return true;
  }
  return false;
}

// ===== Elapsed formatter =====

/** Format elapsed milliseconds as m:ss (e.g. 1:23, 0:07). */
export function formatElapsed(elapsedMs: number): string {
  const totalS = Math.floor(Math.max(0, elapsedMs) / 1000);
  const m = Math.floor(totalS / 60);
  const s = totalS % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}
