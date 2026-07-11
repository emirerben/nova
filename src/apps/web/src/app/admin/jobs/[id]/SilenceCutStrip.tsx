"use client";

/**
 * SilenceCutStrip — per-variant visualization of the silence / filler /
 * retake cut plan (plans/010) persisted by the render task at
 * Job.assembly_plan.variants[i].silence_cut:
 *
 *   { removed: [{ start_s, end_s, reason }], time_saved_s,
 *     original_duration_s, version }
 *
 * The strip spans the ORIGINAL (pre-cut) duration; each removed range is a
 * band colored by reason, positioned proportionally. A header legend names
 * every reason present in the data so color is not the only channel; hover
 * a band for the reason + exact seconds.
 *
 * Version-skew safe by construction: every field is optional in the TS
 * mirror, and the component renders null ONLY when silence_cut is absent —
 * old jobs (no blob) and old APIs (key stripped) simply show nothing. A
 * present blob with no renderable cuts (the pass ran but removed nothing)
 * renders the header with a quiet "no cuts made" note instead.
 * The debug payload carries assembly_plan as raw JSONB
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
  /**
   * True pre-cut source duration in seconds, persisted by newer backends.
   * OPTIONAL (version skew): old blobs lack it, and the backend may persist
   * null when probing failed.
   */
  original_duration_s?: number | null;
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
 * Overlapping ranges are merged before band generation so bands never
 * stack/blend: same-reason overlaps merge into a single range; a
 * different-reason overlap clips the later range to start where the earlier
 * one ends (a later range fully contained in an earlier one drops).
 *
 * The strip's total duration prefers the persisted `original_duration_s`
 * (newer backends) when it is finite and can contain every cut. Otherwise
 * it is inferred as `variantDurationS` (the CUT output duration, when
 * known) + the summed removed durations; without a variant duration it
 * falls back to the last removed end. The max of both is used so
 * inconsistent inputs can never push a band past 100%.
 *
 * Returns null when the blob is absent or carries no usable removed range —
 * the component renders the "no cuts made" header for a present-but-empty
 * blob, and nothing at all when the blob itself is absent.
 */
export function layoutSilenceCutBands(
  cut: SilenceCut | null | undefined,
  variantDurationS?: number | null,
): SilenceCutBandLayout | null {
  if (!cut || typeof cut !== "object") return null;
  const sorted = (Array.isArray(cut.removed) ? cut.removed : [])
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
  if (sorted.length === 0) return null;

  // Merge overlaps (see doc comment). The merged list is non-overlapping
  // with strictly increasing ends, so only the last entry needs checking.
  const ranges: typeof sorted = [];
  for (const r of sorted) {
    const last = ranges[ranges.length - 1];
    if (!last || r.startS >= last.endS) {
      ranges.push({ ...r });
    } else if (r.reason === last.reason) {
      last.endS = Math.max(last.endS, r.endS);
    } else if (r.endS > last.endS) {
      ranges.push({ ...r, startS: last.endS });
    }
  }

  const removedTotalS = ranges.reduce((acc, r) => acc + (r.endS - r.startS), 0);
  const maxEndS = ranges.reduce((acc, r) => Math.max(acc, r.endS), 0);
  const inferredDurationS =
    variantDurationS != null &&
    Number.isFinite(variantDurationS) &&
    variantDurationS > 0
      ? Math.max(variantDurationS + removedTotalS, maxEndS)
      : maxEndS;
  // Prefer the persisted true duration (newer backends); ignore it when it
  // is missing, non-finite, or too small to contain the last cut
  // (version skew / bad probe) — the old inference then applies unchanged.
  const originalDurationS =
    typeof cut.original_duration_s === "number" &&
    Number.isFinite(cut.original_duration_s) &&
    cut.original_duration_s >= maxEndS
      ? cut.original_duration_s
      : inferredDurationS;
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
  const cut = readSilenceCut(variant);
  // Null ONLY when the blob itself is absent (version skew / old jobs).
  if (!cut) return null;

  const layout = layoutSilenceCutBands(cut, variantDurationS);
  if (!layout) {
    // The cut pass ran (blob present) but removed nothing renderable.
    return (
      <div className="mt-2" data-testid="silence-cut-strip">
        <div className="mb-1 flex items-baseline justify-between gap-2 text-[10px] uppercase tracking-wider text-zinc-500">
          <span>Silence cut</span>
          <span className="normal-case text-zinc-500">no cuts made</span>
        </div>
      </div>
    );
  }

  // One legend entry per reason PRESENT in the data (band order) so color
  // is not the only channel distinguishing cut types.
  const reasons = Array.from(new Set(layout.bands.map((b) => b.reason)));
  const ariaLabel = `Silence cut: ${layout.bands
    .map((b) => `${b.reason} ${b.startS.toFixed(1)}–${b.endS.toFixed(1)}s`)
    .join(", ")}, saved ${layout.timeSavedS.toFixed(1)}s`;

  return (
    <div className="mt-2" data-testid="silence-cut-strip">
      <div className="mb-1 flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[10px] uppercase tracking-wider text-zinc-500">
        <span>Silence cut</span>
        {reasons.map((reason) => (
          <span
            key={reason}
            data-testid="silence-cut-legend"
            className="inline-flex items-center gap-1 normal-case text-zinc-500"
          >
            <span
              aria-hidden="true"
              className={`h-1.5 w-1.5 rounded-[2px] ${REASON_COLOR[reason] ?? REASON_FALLBACK_COLOR}`}
            />
            {reason}
          </span>
        ))}
        <span className="ml-auto normal-case text-zinc-400">
          saved {layout.timeSavedS.toFixed(1)}s · {layout.bands.length} cut
          {layout.bands.length === 1 ? "" : "s"}
        </span>
      </div>
      <div
        role="img"
        aria-label={ariaLabel}
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
