"use client";

import { type ReactNode, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  BAND_COLLAPSE_MS,
  CELEBRATION_HOLD_MS,
  POLL_INTERVAL_MS,
} from "./constants";
import { computeBarPosition, detailLine, etaLadder, stallTier } from "./logic";
import { computeAnchors, type NovaStep } from "../../lib/job-phases";
import { EtaBar } from "./EtaBar";
import { NovaActivityFeed, type StepsPresentation } from "./NovaActivityFeed";
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
  /**
   * Nova AI steps activity feed (PR1 `steps` projection on the status
   * response). When `NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED` is "true" AND this
   * is non-empty, NovaActivityFeed renders in place of PhaseChipRow inside
   * the status band. Flag off, or steps absent/empty, ⇒ PhaseChipRow renders
   * exactly as before (byte-identical fallback — this prop is additive).
   */
  steps?: NovaStep[] | null;
  /** Controls whether the AI-steps feed is fully visible or summarized behind
   *  a disclosure. Defaults to full for backwards compatibility. */
  stepsPresentation?: StepsPresentation;
}

/**
 * D5 Progress Theater layout.
 *
 * Compact status band pinned at top:
 *   PhaseChipRow → StatusHeadline → detail line → EtaBar
 *
 * D12 receipt: when isTerminal && isSuccess, band collapses to "✓ {receiptText}"
 *              after CELEBRATION_HOLD_MS. EXCEPT in Nova-steps-feed mode
 *              (NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED + non-empty `steps`): the
 *              band settles into NovaActivityFeed's own persistent one-line
 *              receipt after the same hold and never collapses to height 0.
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
  receiptText = "Your video is ready",
  variants,
  onRetry,
  retrying = false,
  size = "full",
  children,
  tone = "dark",
  steps = null,
  stepsPresentation = "full",
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
  // While retrying, a confident "~N min left" directly contradicts the
  // recovery note below it — the ETA baseline knows nothing about the dead
  // attempt. Suppress the label; the bar itself stays.
  const etaText = isTerminal || retrying ? null : etaLadder(remainingMs);

  // Stall copy for "leave this page" note.
  const tier = stallTier(
    elapsedMs,
    totalBaseline,
  );
  const leaveNote = retrying
    ? "The render paused, so Kria is retrying automatically. This can add a few minutes."
    : tier >= 2
      ? "This is taking longer than expected. Kria is still working on it."
      : "You can leave this page. Kria will keep rendering.";

  // Nova AI steps feed — additive, flag-gated. Flag off or steps absent/empty
  // ⇒ useStepsFeed is false and PhaseChipRow + the legacy D12 receipt render
  // exactly as before.
  const stepsFeedEnabled = process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED === "true";
  const useStepsFeed = stepsFeedEnabled && !!steps && steps.length > 0;

  // D12 receipt band. When useStepsFeed is active, the band settles into
  // NovaActivityFeed's OWN persistent one-line receipt (with a working
  // "See what Nova did" toggle) instead of the plain "✓ {receiptText}" line —
  // and, critically, it never collapses to height 0: the bandCollapsed timer
  // is skipped entirely for this mode. The approved design treats the steps
  // receipt as a persistent artifact, not a transient celebration.
  const [showReceipt, setShowReceipt] = useState(false);
  const [bandCollapsed, setBandCollapsed] = useState(false);
  useEffect(() => {
    if (!isTerminal || !isSuccess) {
      // A re-render brings a collapsed band back to life on the SAME mount (an
      // in-place edit on the item page never remounts this component). Without
      // this reset the band stayed `opacity-0 h-0` and the restarted clock was
      // rendered but invisible.
      setShowReceipt(false);
      setBandCollapsed(false);
      return;
    }
    if (useStepsFeed) {
      // Defensive: guards the (unusual) case where `steps` arrives on a late
      // poll after this component already collapsed the band under the
      // legacy path — never leave the steps-feed mode stuck collapsed.
      setBandCollapsed(false);
    }
    const t1 = setTimeout(() => setShowReceipt(true), CELEBRATION_HOLD_MS);
    const t2 = useStepsFeed
      ? null
      : setTimeout(() => setBandCollapsed(true), CELEBRATION_HOLD_MS + BAND_COLLAPSE_MS);
    return () => {
      clearTimeout(t1);
      if (t2) clearTimeout(t2);
    };
  }, [isTerminal, isSuccess, useStepsFeed]);

  // Detail line from variants.
  const detail = detailLine(variants);

  // Terminal + not successful: every variant failed (or the job died before
  // any rendered). Distinct from isTerminal && isSuccess above — without this
  // branch the headline fell through to the last-seen phase label or
  // "Working on it…", claiming active progress on a render that's already dead.
  const isFailed = isTerminal && !isSuccess;

  // Headline text.
  const headlineText = (() => {
    if (isTerminal && isSuccess) return receiptText;
    if (isFailed) return "This video didn’t render";
    if (currentPhase && phaseLabels[currentPhase]) return phaseLabels[currentPhase];
    return "Working on it…";
  })();

  // Phase log — find the most recent phase event to derive phase-level stall.
  const _phaseLogEntries = phaseLog ?? [];

  // Phases beyond the current one, humanized — dimmed placeholder rows so the
  // feed still communicates "what's left" the way PhaseChipRow's pending
  // chips do today. D6: derived from the phase order, not an index/constant.
  const pendingPhaseLabels = (() => {
    if (!useStepsFeed || isTerminal) return [];
    const idx = currentPhase ? phases.indexOf(currentPhase) : -1;
    if (idx < 0) return [];
    return phases.slice(idx + 1).map((p) => phaseLabels[p] ?? p);
  })();

  // StatusHeadline + detail line + ETA + leave-note + retry button — the part
  // of the band that sits BELOW the chips/feed. Once the steps feed has
  // settled into its own persistent receipt (showReceipt && isTerminal &&
  // isSuccess, steps-feed mode only), this tail is suppressed: NovaActivityFeed
  // already owns "Ready in N:NN" at that point, and re-showing StatusHeadline
  // with the same text would be a duplicate voice.
  const bandTail = (
    <>
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
          // role="status": the retrying/stall copy swap is a stage-level
          // state change — announce it once to screen readers instead of
          // signaling recovery visually only.
          role="status"
          aria-live="polite"
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
      {isFailed && onRetry && (
        <Button
          type="button"
          onClick={onRetry}
          className={`h-auto min-h-11 rounded-full px-4 text-[13px] font-semibold transition-opacity hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
            tone === "light" ? "bg-[#0c0c0e] text-white hover:bg-[#0c0c0e]" : "bg-amber-300 text-zinc-900 hover:bg-amber-300"
          }`}
        >
          Retry render
        </Button>
      )}
    </>
  );

  const stepsReceiptSettled = useStepsFeed && showReceipt && isTerminal && isSuccess;

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
      {useStepsFeed ? (
        // NovaActivityFeed keeps ONE stable call site across the showReceipt
        // flip — its own isTerminal/isSuccess props drive the transition into
        // its persistent one-line receipt internally (no unmount, so its
        // expand/announce-once state carries through cleanly). D12's plain
        // "✓ {receiptText}" line and bandCollapsed height-0 path never apply
        // to this mode — see the effect above.
        <>
          <NovaActivityFeed
            steps={steps}
            tone={tone}
            size="full"
            isTerminal={isTerminal}
            isSuccess={isSuccess}
            receiptText={receiptText}
            pendingLabels={pendingPhaseLabels}
            stepsPresentation={stepsPresentation}
          />
          {!stepsReceiptSettled && bandTail}
        </>
      ) : showReceipt ? (
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
          {bandTail}
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
