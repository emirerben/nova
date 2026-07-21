"use client";

import { type ReactNode, useEffect, useRef, useState } from "react";
import {
  BAND_COLLAPSE_MS,
  CELEBRATION_HOLD_MS,
  POLL_INTERVAL_MS,
} from "./constants";
import { computeBarPosition, detailLine, etaLadder, stallTier } from "./logic";
import { computeAnchors } from "../../lib/job-phases";
import { EtaBar } from "./EtaBar";
import { PhaseChipRow } from "./PhaseChipRow";
import { StatusHeadline } from "./StatusHeadline";

interface PhaseLogEntry {
  name: string;
  ts: string;
  elapsed_ms?: number;
}

interface VariantLike {
  variant_id: string;
  render_status: string | null;
}

interface ProgressTheaterProps {
  /** Ordered phase names (e.g. GENERATIVE_PHASE_ORDER). */
  phases: readonly string[];
  /** Human-readable label for each phase. */
  phaseLabels: Record<string, string>;
  /** Currently active phase name. */
  currentPhase: string | null;
  /** Backend-reported expected duration per phase in ms. */
  expectedPhaseMs: Record<string, number> | null | undefined;
  /** Phase log events from job status. */
  phaseLog: PhaseLogEntry[] | null | undefined;
  /** ISO timestamp when the job started processing (not just created). */
  startedAt: string | null | undefined;
  /** ISO timestamp when the job row was created — always available. */
  jobCreatedAt: string;
  /** True when job has reached a terminal state (success or failure). */
  isTerminal: boolean;
  /** True when terminal + successful — triggers celebration receipt. */
  isSuccess: boolean;
  /** Receipt text shown after band collapses on success (D12). */
  receiptText?: string;
  /** Variants for detail line and payoff zone. */
  variants?: VariantLike[] | null;
  /** Called when user requests retry. */
  onRetry?: () => void;
  /**
   * True when the backend reports the render attempt died and is being
   * automatically retried (stale worker heartbeat — see the generative
   * status route's `retrying` field). Replaces the leave-note with honest
   * recovery copy so a dead attempt doesn't masquerade as healthy progress.
   */
  retrying?: boolean;
  /**
   * D13 layout mode.
   * - 'full': dedicated page-level layout (default).
   * - 'inline': compact status band only, no page-level wrapper.
   */
  size?: "full" | "inline";
  /**
   * Payoff zone contents (variant cards etc).
   * Only rendered in 'full' mode.
   */
  children?: ReactNode;
  /**
   * D20 tone: "light" renders on cream canvas; "dark" (default) renders the dark
   * theatre palette. Forwarded to PhaseChipRow, StatusHeadline, EtaBar.
   */
  tone?: "dark" | "light";
}

/**
 * D5 Progress Theater layout.
 *
 * Compact status band pinned at top:
 *   PhaseChipRow → StatusHeadline → detail line → EtaBar
 *
 * D12 receipt: when isTerminal && isSuccess, band collapses to "✓ {receiptText}"
 *              after CELEBRATION_HOLD_MS.
 * D15: NO border/background/padding — host owns the surface.
 * D13: size='inline' renders the compact band only.
 */
export function ProgressTheater({
  phases,
  phaseLabels,
  currentPhase,
  expectedPhaseMs,
  phaseLog,
  startedAt,
  jobCreatedAt,
  isTerminal,
  isSuccess,
  receiptText = "Your edits are ready",
  variants,
  onRetry: _onRetry,
  retrying = false,
  size = "full",
  children,
  tone = "dark",
}: ProgressTheaterProps) {
  // Elapsed since job start.
  const [elapsedMs, setElapsedMs] = useState(0);
  useEffect(() => {
    const origin = startedAt ?? jobCreatedAt;
    const startTime = new Date(origin).getTime();
    const update = () => setElapsedMs(Date.now() - startTime);
    update();
    if (isTerminal) return;
    const id = setInterval(update, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [startedAt, jobCreatedAt, isTerminal]);

  // Duration-weighted anchors.
  const anchors = computeAnchors(phases, expectedPhaseMs);

  // Current phase anchor window.
  const phaseAnchor: [number, number] = currentPhase && anchors[currentPhase]
    ? anchors[currentPhase]
    : [0, 0.05];

  // Last phase-event timestamp — when did the current phase arrive?
  const lastEventTs = useRef<number>(Date.now());
  const prevPhase = useRef<string | null>(null);
  if (currentPhase !== prevPhase.current) {
    prevPhase.current = currentPhase;
    lastEventTs.current = Date.now();
  }

  // Bar position — pure fn of timestamps.
  const baseline = currentPhase ? (expectedPhaseMs?.[currentPhase] ?? 30_000) : 30_000;
  const barPosition = isTerminal && isSuccess
    ? 1.0
    : computeBarPosition(
        phaseAnchor[0],
        phaseAnchor[1],
        lastEventTs.current,
        Date.now(),
        baseline,
      );

  // ETA
  const totalBaseline = expectedPhaseMs
    ? Object.values(expectedPhaseMs).reduce((a, b) => a + b, 0)
    : null;
  const remainingMs = totalBaseline != null ? Math.max(0, totalBaseline - elapsedMs) : null;
  const etaText = isTerminal ? null : etaLadder(remainingMs);

  // Stall copy for "leave this page" note.
  const tier = stallTier(
    elapsedMs,
    totalBaseline,
  );
  const leaveNote = retrying
    ? "Hit a snag mid-render — retrying automatically. This can add a few minutes."
    : tier >= 2
      ? "Taking a bit longer than expected — still working on it."
      : "You can leave this page — we'll keep rendering.";

  // D12 receipt band.
  const [showReceipt, setShowReceipt] = useState(false);
  const [bandCollapsed, setBandCollapsed] = useState(false);
  useEffect(() => {
    if (!isTerminal || !isSuccess) return;
    const t1 = setTimeout(() => setShowReceipt(true), CELEBRATION_HOLD_MS);
    const t2 = setTimeout(
      () => setBandCollapsed(true),
      CELEBRATION_HOLD_MS + BAND_COLLAPSE_MS,
    );
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
    };
  }, [isTerminal, isSuccess]);

  // Detail line from variants.
  const detail = detailLine(variants);

  // Headline text.
  const headlineText = (() => {
    if (isTerminal && isSuccess) return receiptText;
    if (currentPhase && phaseLabels[currentPhase]) return phaseLabels[currentPhase];
    return "Working on it…";
  })();

  // Phase log — find the most recent phase event to derive phase-level stall.
  const _phaseLogEntries = phaseLog ?? [];

  const statusBand = (
    <div
      className={[
        "space-y-3",
        "transition-all",
        bandCollapsed ? "opacity-0 h-0 overflow-hidden pointer-events-none" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      style={{ transitionDuration: `${BAND_COLLAPSE_MS}ms` }}
    >
      {showReceipt ? (
        <p className={`flex items-center gap-2 text-sm font-medium ${tone === "light" ? "text-lime-700" : "text-amber-300"}`}>
          <span aria-hidden="true">✓</span>
          {receiptText}
        </p>
      ) : (
        <>
          <PhaseChipRow
            phases={phases}
            phaseLabels={phaseLabels}
            currentPhase={currentPhase}
            tone={tone}
          />
          <StatusHeadline text={headlineText} tone={tone} />
          {detail && (
            <p className={`text-xs ${tone === "light" ? "text-[#71717a]" : "text-zinc-500"}`}>{detail}</p>
          )}
          {!isTerminal && (
            <EtaBar
              barPosition={barPosition}
              elapsedMs={elapsedMs}
              etaText={etaText}
              tone={tone}
            />
          )}
          {!isTerminal && (
            <p
              className={[
                "text-xs",
                retrying || tier >= 2
                  ? (tone === "light" ? "text-lime-700" : "text-amber-400")
                  : (tone === "light" ? "text-[#a1a1aa]" : "text-zinc-600"),
              ].join(" ")}
            >
              {leaveNote}
            </p>
          )}
        </>
      )}
    </div>
  );

  if (size === "inline") {
    return statusBand;
  }

  return (
    <div className="w-full space-y-8">
      {statusBand}
      {children}
    </div>
  );
}
