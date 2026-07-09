"use client";

/**
 * SilenceCutStrip — per-variant visualization of the silence / filler /
 * retake cut plan (plans/010) persisted by the render task at
 * Job.assembly_plan.variants[i].silence_cut:
 *
 *   { removed: [{ start_s, end_s, reason }], time_saved_s, version }
 *
 * The strip spans the ORIGINAL (pre-cut) duration; each removed range is a
 * band colored by reason, positioned proportionally. Hover a band for the
 * reason + exact seconds.
 *
 * Version-skew safe by construction: every field is optional in the TS
 * mirror, and the component renders null when silence_cut is absent or has
 * no removed ranges — old jobs (no blob) and old APIs (key stripped) simply
 * show nothing. The debug payload carries assembly_plan as raw JSONB
 * (admin_jobs.py JobPayload.assembly_plan: Any), so no backend change is
 * needed for the blob to reach this component.
 */

// ── TS mirror of the persisted blob (everything optional — version skew) ─────

export interface SilenceCutRemovedRange {
  start_s?: number;
  end_s?: number;
  /** 'silence' | 'filler_lexical' | 'filler_acoustic' | 'retake' | future values */
  reason?: string;
}

export interface SilenceCut {
  removed?: SilenceCutRemovedRange[];
  time_saved_s?: number;
  version?: number;
}

// ── Band layout (pure, exported for tests) ───────────────────────────────────

export interface SilenceCutBand {
  /** Left edge as a percentage of the original (pre-cut) duration, 0–100. */
  leftPct: number;
  /** Width as a percentage of the original (pre-cut) duration, 0–100. */
  widthPct: number;
  startS: number;
  endS: number;
  durationS: number;
  reason: string;
}

export interface SilenceCutBandLayout {
  bands: SilenceCutBand[];
  /** Inferred original (pre-cut) duration the strip spans. */
  originalDurationS: number;
  /** time_saved_s from the blob, else the summed removed durations. */
  timeSavedS: number;
}

/**
 * Compute proportional band positions for a silence_cut blob.
 *
 * Original (pre-cut) duration is inferred as `variantDurationS` (the CUT
 * output duration, when known) + the summed removed durations; without a
 * variant duration it falls back to the last removed end. The max of both
 * is used so inconsistent inputs can never push a band past 100%.
 *
 * Returns null when the blob is absent or carries no usable removed range —
 * callers render nothing (old jobs / old APIs / no-op plans).
 */
export function layoutSilenceCutBands(
  cut: SilenceCut | null | undefined,
  variantDurationS?: number | null,
): SilenceCutBandLayout | null {
  if (!cut || typeof cut !== "object") return null;
  const ranges = (Array.isArray(cut.removed) ? cut.removed : [])
    .map((r) => ({
      startS: Number(r?.start_s),
      endS: Number(r?.end_s),
      reason:
        typeof r?.reason === "string" && r.reason.length > 0
          ? r.reason
          : "unknown",
    }))
    .filter(
      (r) =>
        Number.isFinite(r.startS) &&
        Number.isFinite(r.endS) &&
        r.endS > r.startS &&
        r.endS > 0,
    )
    .sort((a, b) => a.startS - b.startS);
  if (ranges.length === 0) return null;

  const removedTotalS = ranges.reduce((acc, r) => acc + (r.endS - r.startS), 0);
  const maxEndS = ranges.reduce((acc, r) => Math.max(acc, r.endS), 0);
  const originalDurationS =
    variantDurationS != null &&
    Number.isFinite(variantDurationS) &&
    variantDurationS > 0
      ? Math.max(variantDurationS + removedTotalS, maxEndS)
      : maxEndS;
  if (originalDurationS <= 0) return null;

  const bands: SilenceCutBand[] = [];
  for (const r of ranges) {
    // Clamp into [0, originalDurationS] — overlapping or slightly-out-of-range
    // input renders clipped instead of breaking the strip.
    const start = Math.min(Math.max(r.startS, 0), originalDurationS);
    const end = Math.min(Math.max(r.endS, 0), originalDurationS);
    if (end <= start) continue;
    bands.push({
      leftPct: (start / originalDurationS) * 100,
      widthPct: ((end - start) / originalDurationS) * 100,
      startS: r.startS,
      endS: r.endS,
      durationS: r.endS - r.startS,
      reason: r.reason,
    });
  }
  if (bands.length === 0) return null;

  const timeSavedS =
    typeof cut.time_saved_s === "number" && Number.isFinite(cut.time_saved_s)
      ? cut.time_saved_s
      : removedTotalS;
  return { bands, originalDurationS, timeSavedS };
}

/** Defensive read of `silence_cut` off an untyped variant entry. */
export function readSilenceCut(variant: unknown): SilenceCut | null {
  if (!variant || typeof variant !== "object") return null;
  const sc = (variant as { silence_cut?: unknown }).silence_cut;
  if (!sc || typeof sc !== "object") return null;
  return sc as SilenceCut;
}

// ── Renderer ──────────────────────────────────────────────────────────────────

// Same family as STAGE_COLOR on the job-debug page (bg-*-600/70).
const REASON_COLOR: Record<string, string> = {
  silence: "bg-blue-600/70",
  filler_lexical: "bg-yellow-600/70",
  filler_acoustic: "bg-orange-600/70",
  retake: "bg-red-600/70",
};
const REASON_FALLBACK_COLOR = "bg-zinc-600/70";

export function SilenceCutStrip({
  variant,
  variantDurationS,
}: {
  /** One assembly_plan.variants[] entry (untyped — read defensively). */
  variant: unknown;
  /** CUT output duration in seconds, when the caller knows it. */
  variantDurationS?: number | null;
}): JSX.Element | null {
  const layout = layoutSilenceCutBands(readSilenceCut(variant), variantDurationS);
  if (!layout) return null;

  return (
    <div className="mt-2" data-testid="silence-cut-strip">
      <div className="mb-1 flex items-baseline justify-between gap-2 text-[10px] uppercase tracking-wider text-zinc-500">
        <span>Silence cut</span>
        <span className="normal-case text-zinc-400">
          saved {layout.timeSavedS.toFixed(1)}s · {layout.bands.length} cut
          {layout.bands.length === 1 ? "" : "s"}
        </span>
      </div>
      <div
        className="relative h-3 overflow-hidden rounded border border-zinc-800 bg-zinc-900/60"
        title={`Original ~${layout.originalDurationS.toFixed(1)}s — colored bands were removed`}
      >
        {layout.bands.map((b, i) => (
          <div
            key={`cut-${i}`}
            data-testid="silence-cut-band"
            className={`absolute top-0 bottom-0 ${REASON_COLOR[b.reason] ?? REASON_FALLBACK_COLOR}`}
            style={{
              left: `${b.leftPct}%`,
              width: `${b.widthPct}%`,
              minWidth: "2px",
            }}
            title={`${b.reason} · ${b.startS.toFixed(2)}–${b.endS.toFixed(2)}s · ${b.durationS.toFixed(2)}s removed`}
          />
        ))}
      </div>
    </div>
  );
}
