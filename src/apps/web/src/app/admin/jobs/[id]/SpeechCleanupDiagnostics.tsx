"use client";

import { useMemo, useState } from "react";

import type { PipelineTraceEvent } from "@/lib/admin-jobs-api";

const ANALYSIS_EVENT = "silence_cut_mixed_gap_analysis";
const OUTCOME_EVENT = "speech_cleanup_render_outcome";
const SUPPORTED_SCHEMA_VERSION = 1;
const ANALYSIS_VIEWS = new Set(["full_clip", "talking_head_spine_capped"]);
const ASSIGNMENT_STATUSES = new Set([
  "assigned",
  "missing_source_instance",
  "cardinality_mismatch",
  "invalid_source_instance",
  "duplicate_source_instance",
  "unmapped_clip_id",
  "ambiguous_clip_id",
  "identity_cache_unavailable",
]);
const MODES = new Set(["off", "shadow", "apply"]);
export const SPEECH_CLEANUP_CANDIDATE_STATUSES = [
  "analysis_failed",
  "analysis_not_started",
  "build_failed",
  "outer_media_probe_failed",
  "precheck_clip_too_short",
  "precheck_no_audio",
  "ready",
  "receipt_build_failed",
  "tool_unavailable",
  "validation_failed",
] as const;
const CANDIDATE_STATUSES = new Set<string>(SPEECH_CLEANUP_CANDIDATE_STATUSES);
const SELECTED_PLANS = new Set(["baseline", "candidate"]);
const TERMINAL_OUTCOMES = new Set([
  "published_applied",
  "published_no_change",
  "published_baseline_fallback",
  "discarded_superseded",
  "discarded_finalization_rejected",
  "failed_owned",
  "cancelled_owned",
]);
const SOURCE_TAG_PATTERN = /^[0-9a-f]{16}$/;

type Span = { startMs: number; endMs: number };

type Island = Span & {
  detection: string;
  reason: string;
  disposition: string | null;
};

type PlanBand = {
  spans: Span[];
  removedCount: number | null;
  removedMs: number | null;
  omitted: number;
  clamped: boolean | null;
  bailoutReason: string | null;
};

type Thresholds = {
  silenceMin: number | null;
  islandMin: number | null;
  islandMax: number | null;
  flankMin: number | null;
  minCut: number | null;
};

type TerminalOutcome = {
  outcome: string;
  variantId: string | null;
  generationId: string | null;
  ts: string;
};

export type SpeechCleanupAttempt = {
  key: string;
  eventOrdinal: number;
  ts: string;
  detectorVersion: string;
  attemptId: string;
  analysisView: string;
  sourceSlot: number | null;
  sourceTag: string | null;
  assignmentStatus: string;
  analysisPolicy: string;
  configuredMode: string;
  effectiveMode: string;
  candidateStatus: string;
  rolloutPercent: number | null;
  rolloutBucket: number | null;
  durationMs: number;
  asr: Span[];
  asrOmitted: number;
  silence: Span[];
  silenceOmitted: number;
  islands: Island[];
  islandsOmitted: number;
  thresholds: Thresholds;
  baseline: PlanBand;
  candidate: PlanBand;
  selectedPlan: string;
  truncated: boolean;
  outcome: TerminalOutcome | null;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function boundedString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 && value.length <= 160
    ? value
    : null;
}

function optionalString(value: unknown): string | null {
  return value == null ? null : boundedString(value);
}

function enumString(value: unknown, allowed: ReadonlySet<string>): string | null {
  return typeof value === "string" && allowed.has(value) ? value : null;
}

function boundedInteger(value: unknown, min: number, max: number): number | null {
  const parsed = finiteNumber(value);
  return parsed !== null && Number.isInteger(parsed) && parsed >= min && parsed <= max
    ? parsed
    : null;
}

function nonNegativeInteger(value: unknown): number {
  const parsed = finiteNumber(value);
  return parsed !== null && Number.isInteger(parsed) && parsed >= 0 ? parsed : 0;
}

function parseSpan(value: unknown, durationMs: number): Span | null {
  if (!Array.isArray(value) || value.length !== 2) return null;
  const startMs = finiteNumber(value[0]);
  const endMs = finiteNumber(value[1]);
  if (
    startMs === null ||
    endMs === null ||
    !Number.isInteger(startMs) ||
    !Number.isInteger(endMs) ||
    startMs < 0 ||
    endMs <= startMs ||
    endMs > durationMs
  ) {
    return null;
  }
  return { startMs, endMs };
}

function parseSpans(value: unknown, durationMs: number): Span[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => parseSpan(item, durationMs))
    .filter((span): span is Span => span !== null)
    .sort((a, b) => a.startMs - b.startMs || a.endMs - b.endMs);
}

function parsePlan(value: unknown, durationMs: number): PlanBand {
  const raw = isRecord(value) ? value : {};
  return {
    spans: parseSpans(raw.removed_spans_ms, durationMs),
    removedCount: finiteNumber(raw.removed_count),
    removedMs: finiteNumber(raw.removed_ms),
    omitted: nonNegativeInteger(raw.removed_spans_omitted),
    clamped: typeof raw.clamped === "boolean" ? raw.clamped : null,
    bailoutReason: optionalString(raw.bailout_reason),
  };
}

function parseThresholds(value: unknown): Thresholds {
  const raw = isRecord(value) ? value : {};
  return {
    silenceMin: finiteNumber(raw.silence_min),
    islandMin: finiteNumber(raw.island_min),
    islandMax: finiteNumber(raw.island_max),
    flankMin: finiteNumber(raw.flank_silence_min),
    minCut: finiteNumber(raw.min_cut),
  };
}

function parseIslands(value: unknown, durationMs: number): Island[] {
  if (!isRecord(value) || !Array.isArray(value.records)) return [];
  const islands: Island[] = [];
  for (const item of value.records) {
    if (!isRecord(item)) continue;
    const span = parseSpan(
      [item.island_start_ms, item.island_end_ms],
      durationMs,
    );
    const detection = boundedString(item.detection);
    const reason = boundedString(item.reason);
    if (!span || !detection || !reason) continue;
    islands.push({
      ...span,
      detection,
      reason,
      disposition: optionalString(item.plan_disposition),
    });
  }
  return islands.sort((a, b) => a.startMs - b.startMs || a.endMs - b.endMs);
}

function parseOutcome(event: PipelineTraceEvent): TerminalOutcome | null {
  if (event.event !== OUTCOME_EVENT || !isRecord(event.data)) return null;
  const outcome = enumString(event.data.outcome, TERMINAL_OUTCOMES);
  if (!outcome) return null;
  return {
    outcome,
    variantId: optionalString(event.data.variant_id),
    generationId: optionalString(event.data.render_generation_id),
    ts: event.ts,
  };
}

function sameCorrelation(
  analysis: Record<string, unknown>,
  outcome: Record<string, unknown>,
): boolean {
  if (
    analysis.analysis_attempt_id !== outcome.analysis_attempt_id ||
    analysis.analysis_view !== outcome.analysis_view ||
    analysis.detector_version !== outcome.detector_version
  ) {
    return false;
  }
  const analysisTag = analysis.source_tag;
  const outcomeTag = outcome.source_tag;
  return analysisTag == null || outcomeTag == null
    ? analysisTag == null && outcomeTag == null
    : analysisTag === outcomeTag;
}

/** Parse only the bounded v1 timing receipt. Unknown versions are deliberately ignored. */
export function parseSpeechCleanupAttempts(
  events: PipelineTraceEvent[],
): SpeechCleanupAttempt[] {
  const outcomes = events
    .map((event, ordinal) => ({ event, ordinal, parsed: parseOutcome(event) }))
    .filter(
      (entry): entry is {
        event: PipelineTraceEvent;
        ordinal: number;
        parsed: TerminalOutcome;
      } => entry.parsed !== null,
    );
  const attempts: SpeechCleanupAttempt[] = [];

  events.forEach((event, eventOrdinal) => {
    if (event.event !== ANALYSIS_EVENT || !isRecord(event.data)) return;
    const raw = event.data;
    if (raw.schema_version !== SUPPORTED_SCHEMA_VERSION) return;
    const durationMs = finiteNumber(raw.duration_ms);
    const detectorVersion = boundedString(raw.detector_version);
    const attemptId = boundedString(raw.analysis_attempt_id);
    const analysisView = enumString(raw.analysis_view, ANALYSIS_VIEWS);
    const assignmentStatus = enumString(raw.assignment_status, ASSIGNMENT_STATUSES);
    const analysisPolicy =
      raw.analysis_policy === "required_v1" ? "required_v1" : null;
    const configuredMode = enumString(raw.configured_mode, MODES);
    const effectiveMode = enumString(raw.effective_mode, MODES);
    const candidateStatus = enumString(raw.candidate_status, CANDIDATE_STATUSES);
    const selectedPlan = enumString(raw.selected_plan, SELECTED_PLANS);
    const rolloutPercent = boundedInteger(raw.rollout_percent, 0, 100);
    const rolloutBucket =
      raw.rollout_bucket == null ? null : boundedInteger(raw.rollout_bucket, 0, 99);
    if (
      durationMs === null ||
      !Number.isInteger(durationMs) ||
      durationMs <= 0 ||
      !detectorVersion ||
      !attemptId ||
      !analysisView ||
      !assignmentStatus ||
      !analysisPolicy ||
      !configuredMode ||
      !effectiveMode ||
      !candidateStatus ||
      !selectedPlan ||
      rolloutPercent === null ||
      (raw.rollout_bucket != null && rolloutBucket === null)
    ) {
      return;
    }
    const sourceSlotValue = finiteNumber(raw.source_slot);
    const sourceSlot =
      sourceSlotValue !== null && Number.isInteger(sourceSlotValue) && sourceSlotValue >= 0
        ? sourceSlotValue
        : null;
    const sourceTag =
      raw.source_tag == null
        ? null
        : typeof raw.source_tag === "string" && SOURCE_TAG_PATTERN.test(raw.source_tag)
          ? raw.source_tag
          : undefined;
    if (sourceTag === undefined) return;
    if (
      (assignmentStatus === "assigned" &&
        (sourceSlot === null || sourceTag === null || rolloutBucket === null)) ||
      (assignmentStatus !== "assigned" &&
        (sourceTag !== null || rolloutBucket !== null || effectiveMode === "apply"))
    ) {
      return;
    }
    const inputs = isRecord(raw.inputs) ? raw.inputs : {};
    const mixedGap = isRecord(raw.mixed_gap_scan) ? raw.mixed_gap_scan : {};
    const baseline = parsePlan(raw.baseline_plan, durationMs);
    const candidate = parsePlan(raw.candidate_plan, durationMs);
    const matchingOutcomes = outcomes
      .filter(({ event: outcomeEvent }) => sameCorrelation(raw, outcomeEvent.data))
      .sort(
        (a, b) =>
          a.parsed.ts.localeCompare(b.parsed.ts) || a.ordinal - b.ordinal,
      );
    const latestOutcome = matchingOutcomes.at(-1)?.parsed ?? null;
    const key = sourceTag
      ? `${sourceTag}:${analysisView}:${attemptId}:${eventOrdinal}`
      : `${attemptId}:${analysisView}:${sourceSlot ?? `event-${eventOrdinal}`}`;
    attempts.push({
      key,
      eventOrdinal,
      ts: event.ts,
      detectorVersion,
      attemptId,
      analysisView,
      sourceSlot,
      sourceTag,
      assignmentStatus,
      analysisPolicy,
      configuredMode,
      effectiveMode,
      candidateStatus,
      rolloutPercent,
      rolloutBucket,
      durationMs,
      asr: parseSpans(inputs.asr_word_spans_ms, durationMs),
      asrOmitted: nonNegativeInteger(inputs.asr_word_spans_omitted),
      silence: parseSpans(inputs.silence_spans_ms, durationMs),
      silenceOmitted: nonNegativeInteger(inputs.silence_spans_omitted),
      islands: parseIslands(mixedGap, durationMs),
      islandsOmitted: nonNegativeInteger(mixedGap.records_omitted),
      thresholds: parseThresholds(raw.thresholds_ms),
      baseline,
      candidate,
      selectedPlan,
      truncated:
        nonNegativeInteger(inputs.asr_word_spans_omitted) > 0 ||
        nonNegativeInteger(inputs.silence_spans_omitted) > 0 ||
        nonNegativeInteger(inputs.lexical_candidates_omitted) > 0 ||
        nonNegativeInteger(mixedGap.records_omitted) > 0 ||
        baseline.omitted > 0 ||
        candidate.omitted > 0,
      outcome: latestOutcome,
    });
  });

  return attempts.sort(
    (a, b) => a.ts.localeCompare(b.ts) || a.eventOrdinal - b.eventOrdinal,
  );
}

function currentVariantGenerations(assemblyPlan: unknown): Map<string, string> {
  const generations = new Map<string, string>();
  if (!isRecord(assemblyPlan) || !Array.isArray(assemblyPlan.variants)) return generations;
  for (const raw of assemblyPlan.variants) {
    if (!isRecord(raw)) continue;
    const variantId = boundedString(raw.variant_id);
    const generationId = boundedString(raw.render_generation_id);
    if (variantId && generationId) generations.set(variantId, generationId);
  }
  return generations;
}

function formatMs(value: number): string {
  return `${Math.round(value).toLocaleString("en-US")} ms`;
}

function shortAttemptId(value: string): string {
  return value.length <= 28 ? value : `${value.slice(0, 16)}…${value.slice(-8)}`;
}

function trackAriaLabel(label: string, spans: Span[], partial = false): string {
  const detail = spans.length
    ? spans.map((span) => `${formatMs(span.startMs)} to ${formatMs(span.endMs)}`).join(", ")
    : "none recorded";
  return `${label}: ${detail}${partial ? "; partial receipt" : ""}`;
}

function TimelineTrack({
  label,
  spans,
  durationMs,
  className,
  partial = false,
}: {
  label: string;
  spans: Span[];
  durationMs: number;
  className: string;
  partial?: boolean;
}): JSX.Element {
  return (
    <div className="grid grid-cols-[9rem_minmax(0,1fr)] items-center gap-3">
      <div className="text-right text-[11px] text-zinc-400">
        {label}
        {partial ? <span className="ml-1 text-zinc-300">(partial)</span> : null}
      </div>
      <div
        role="img"
        aria-label={trackAriaLabel(label, spans, partial)}
        className="relative h-5 overflow-hidden rounded border border-zinc-800 bg-zinc-900/70"
      >
        {spans.map((span, index) => (
          <span
            key={`${span.startMs}-${span.endMs}-${index}`}
            data-testid={`speech-cleanup-${label.toLowerCase().replaceAll(" ", "-")}-band`}
            className={`absolute inset-y-0 border border-black/40 ${className}`}
            style={{
              left: `${(span.startMs / durationMs) * 100}%`,
              width: `${((span.endMs - span.startMs) / durationMs) * 100}%`,
              minWidth: "2px",
            }}
            title={`${label}: ${formatMs(span.startMs)}–${formatMs(span.endMs)}`}
          />
        ))}
      </div>
    </div>
  );
}

function IslandTrack({
  islands,
  durationMs,
  partial = false,
}: {
  islands: Island[];
  durationMs: number;
  partial?: boolean;
}): JSX.Element {
  return (
    <div className="grid grid-cols-[9rem_minmax(0,1fr)] items-center gap-3">
      <div className="text-right text-[11px] text-zinc-400">
        V2 islands
        {partial ? <span className="ml-1 text-zinc-300">(partial)</span> : null}
      </div>
      <div
        role="img"
        aria-label={`V2 islands: ${
          islands.length
            ? islands
                .map(
                  (island) =>
                    `${island.detection} ${formatMs(island.startMs)} to ${formatMs(island.endMs)}, ${island.reason}${island.disposition ? `, ${island.disposition}` : ""}`,
                )
                .join("; ")
            : "none recorded"
        }${partial ? "; partial receipt" : ""}`}
        className="relative h-5 overflow-hidden rounded border border-zinc-800 bg-zinc-900/70"
      >
        {islands.map((island, index) => {
          const eligible = island.detection === "eligible";
          return (
            <span
              key={`${island.startMs}-${island.endMs}-${index}`}
              data-testid={`speech-cleanup-island-${eligible ? "eligible" : "rejected"}`}
              className={`absolute inset-y-0 border-2 ${
                eligible
                  ? "border-zinc-100 bg-zinc-300/70"
                  : "border-dashed border-zinc-500 bg-zinc-700/30"
              }`}
              style={{
                left: `${(island.startMs / durationMs) * 100}%`,
                width: `${((island.endMs - island.startMs) / durationMs) * 100}%`,
                minWidth: "4px",
              }}
              title={`${eligible ? "Eligible" : "Rejected"} island: ${formatMs(island.startMs)}–${formatMs(island.endMs)} · ${island.reason}${island.disposition ? ` · ${island.disposition}` : ""}`}
            />
          );
        })}
      </div>
    </div>
  );
}

function Detail({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wider text-zinc-400">{label}</dt>
      <dd className="mt-0.5 break-words text-xs text-zinc-200">{value}</dd>
    </div>
  );
}

function describeOutcome(
  attempt: SpeechCleanupAttempt,
  generations: Map<string, string>,
): string {
  const terminal = attempt.outcome;
  if (!terminal) return "No terminal render outcome recorded";
  if (!terminal.outcome.startsWith("published_")) return terminal.outcome;
  if (!terminal.variantId || !terminal.generationId) {
    return `${terminal.outcome} — generation unavailable`;
  }
  const current = generations.get(terminal.variantId);
  if (current === undefined) {
    return `${terminal.outcome} — current generation unavailable`;
  }
  return current === terminal.generationId
    ? `${terminal.outcome} — currently live`
    : `${terminal.outcome} — historical generation`;
}

function describePlanTotal(plan: PlanBand): string {
  const count = plan.removedCount === null ? "unknown cuts" : `${plan.removedCount} cuts`;
  const duration = plan.removedMs === null ? "unknown duration" : formatMs(plan.removedMs);
  return `${count} · ${duration}`;
}

function describeThresholds(thresholds: Thresholds): string {
  const values: Array<[string, number | null]> = [
    ["silence", thresholds.silenceMin],
    ["island", thresholds.islandMin],
    ["island max", thresholds.islandMax],
    ["flank", thresholds.flankMin],
    ["min cut", thresholds.minCut],
  ];
  const available = values.filter((entry): entry is [string, number] => entry[1] !== null);
  return available.length
    ? available.map(([label, value]) => `${label} ${formatMs(value)}`).join("; ")
    : "not recorded";
}

export function SpeechCleanupDiagnostics({
  events,
  assemblyPlan,
}: {
  events: PipelineTraceEvent[];
  assemblyPlan?: unknown;
}): JSX.Element | null {
  const attempts = useMemo(() => parseSpeechCleanupAttempts(events), [events]);
  const rawAnalysisCount = useMemo(
    () => events.filter((event) => event.event === ANALYSIS_EVENT).length,
    [events],
  );
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const generations = useMemo(
    () => currentVariantGenerations(assemblyPlan),
    [assemblyPlan],
  );
  if (rawAnalysisCount === 0) return null;
  const discardedReceiptCount = rawAnalysisCount - attempts.length;
  if (attempts.length === 0) {
    return (
      <section
        aria-labelledby="speech-cleanup-diagnostics-title"
        className="rounded border border-zinc-800 bg-zinc-950 px-4 py-4"
        data-testid="speech-cleanup-diagnostics"
      >
        <h2 id="speech-cleanup-diagnostics-title" className="text-sm font-semibold">
          Speech cleanup diagnostics
        </h2>
        <div
          role="status"
          className="mt-3 rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-300"
        >
          {rawAnalysisCount} analysis {rawAnalysisCount === 1 ? "receipt is" : "receipts are"}{" "}
          unavailable because the recorded schema is unsupported or malformed.
        </div>
      </section>
    );
  }
  const selected =
    attempts.find((attempt) => attempt.key === selectedKey) ?? attempts.at(-1)!;
  const baselinePartial = selected.baseline.omitted > 0;
  const candidatePartial = selected.candidate.omitted > 0;

  return (
    <section
      aria-labelledby="speech-cleanup-diagnostics-title"
      className="rounded border border-zinc-800 bg-zinc-950 px-4 py-4"
      data-testid="speech-cleanup-diagnostics"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <h2 id="speech-cleanup-diagnostics-title" className="text-sm font-semibold">
          Speech cleanup diagnostics
        </h2>
        {attempts.length > 1 ? (
          <label className="w-full text-sm text-zinc-300 sm:w-auto">
            <span>Attempt</span>
            <select
              aria-label="Speech cleanup attempt"
              className="mt-1 min-h-11 w-full max-w-full rounded border border-zinc-700 bg-zinc-900 px-3 text-base text-zinc-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-400 sm:ml-2 sm:mt-0 sm:w-auto sm:max-w-md sm:text-sm"
              value={selected.key}
              onChange={(event) => setSelectedKey(event.target.value)}
            >
              {attempts.map((attempt, index) => (
                <option key={attempt.key} value={attempt.key}>
                  {index + 1}. {attempt.sourceTag ?? `unassigned/${attempt.sourceSlot ?? attempt.eventOrdinal}`} · {attempt.analysisView} · {shortAttemptId(attempt.attemptId)}
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </div>

      {discardedReceiptCount > 0 ? (
        <div
          role="status"
          className="mt-3 rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-300"
        >
          {discardedReceiptCount} additional analysis {discardedReceiptCount === 1 ? "receipt" : "receipts"}{" "}
          could not be displayed because the recorded schema is unsupported or malformed.
        </div>
      ) : null}

      {selected.truncated ? (
        <div
          role="status"
          className="mt-3 rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-300"
        >
          Partial receipt: one or more timing arrays were truncated. Summary totals remain
          authoritative; visible bands are not the complete set.
        </div>
      ) : null}

      <dl className="mt-4 grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Detail label="Detector" value={selected.detectorVersion} />
        <Detail label="View" value={selected.analysisView} />
        <Detail label="Policy" value={selected.analysisPolicy} />
        <Detail
          label="Mode"
          value={`${selected.configuredMode} → ${selected.effectiveMode}`}
        />
        <Detail label="Assignment" value={selected.assignmentStatus} />
        <Detail
          label="Bucket"
          value={
            selected.rolloutBucket === null
              ? "unassigned"
              : `${selected.rolloutBucket} / ${selected.rolloutPercent ?? "?"}%`
          }
        />
        <Detail label="Candidate" value={selected.candidateStatus} />
        <Detail label="Thresholds" value={describeThresholds(selected.thresholds)} />
        <Detail label="Selection intent" value={selected.selectedPlan} />
        <Detail
          label="Terminal render"
          value={describeOutcome(selected, generations)}
        />
        <Detail
          label="Baseline total"
          value={describePlanTotal(selected.baseline)}
        />
        <Detail
          label="Candidate total"
          value={describePlanTotal(selected.candidate)}
        />
        <Detail
          label="Clamp"
          value={`baseline ${selected.baseline.clamped === null ? "unknown" : selected.baseline.clamped ? "clamped" : "not clamped"}; candidate ${selected.candidate.clamped === null ? "unknown" : selected.candidate.clamped ? "clamped" : "not clamped"}`}
        />
      </dl>

      <div className="mt-5 space-y-2">
        <TimelineTrack
          label="ASR words"
          spans={selected.asr}
          durationMs={selected.durationMs}
          className="bg-zinc-200/75"
          partial={selected.asrOmitted > 0}
        />
        <TimelineTrack
          label="FFmpeg silence"
          spans={selected.silence}
          durationMs={selected.durationMs}
          className="bg-zinc-500/70"
          partial={selected.silenceOmitted > 0}
        />
        <IslandTrack
          islands={selected.islands}
          durationMs={selected.durationMs}
          partial={selected.islandsOmitted > 0}
        />
        <TimelineTrack
          label="Baseline cuts"
          spans={selected.baseline.spans}
          durationMs={selected.durationMs}
          className="bg-zinc-700/90"
          partial={baselinePartial}
        />
        <TimelineTrack
          label="Candidate cuts"
          spans={selected.candidate.spans}
          durationMs={selected.durationMs}
          className="bg-zinc-300/80"
          partial={candidatePartial}
        />
      </div>

      <div className="mt-4 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-zinc-400">
        <span>Duration {formatMs(selected.durationMs)}</span>
        <span>Attempt {selected.attemptId}</span>
        <span>Source {selected.sourceTag ?? "unassigned"}</span>
        {selected.baseline.bailoutReason ? (
          <span>Baseline bailout: {selected.baseline.bailoutReason}</span>
        ) : null}
        {selected.candidate.bailoutReason ? (
          <span>Candidate bailout: {selected.candidate.bailoutReason}</span>
        ) : null}
      </div>
    </section>
  );
}
