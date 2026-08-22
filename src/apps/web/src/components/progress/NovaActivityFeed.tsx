"use client";

import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import type { NovaStep } from "@/lib/job-phases";
import { NovaPendingRow, NovaStepRow } from "./NovaStepRow";

export type StepsPresentation = "full" | "disclosure";

interface NovaActivityFeedProps {
  /** Steps in arrival order. Empty/absent ⇒ renders nothing (state a — the
   *  existing pre-steps shimmer/PhaseChipRow fallback owns that window;
   *  see ProgressTheater). */
  steps: NovaStep[] | null | undefined;
  /** "light" (lime accent, editorial surfaces) or "dark" (amber accent, D20). */
  tone?: "dark" | "light";
  /** "compact" is for the future chat inline usage (PR4); "full" (default)
   *  is the render-progress band. */
  size?: "full" | "compact";
  isTerminal: boolean;
  /** Terminal + successful. Drives the collapsed receipt state (c). */
  isSuccess: boolean;
  /** "Ready in m:ss" style copy — same value ProgressTheater already derives
   *  via deriveReceiptText, reused verbatim as the receipt's lead text. */
  receiptText?: string;
  /** Future phase labels not yet reached, derived by the caller from the
   *  phase order (D6: never derived from an index in here). Dimmed,
   *  non-interactive placeholder rows appended after the real steps.
   *  Ignored once isTerminal. */
  pendingLabels?: string[];
  /** "disclosure" keeps only the latest step and completed count visible
   *  until the user asks for the full analysis feed. */
  stepsPresentation?: StepsPresentation;
}

/**
 * Render-progress "Nova AI steps" activity feed.
 *
 * D15: draws no border/background/outer padding — the host (ProgressTheater's
 * status band) owns the surface.
 * D17: a single `role="status" aria-live="polite"` region announces each
 * newly-arrived step label once — the visible row list itself is NOT a live
 * region (re-announcing on every render would spam screen readers). This is
 * a second, step-level voice alongside StatusHeadline's phase-level
 * announcements — the two never carry the same text, so they don't double up.
 */
export function NovaActivityFeed({
  steps,
  tone = "light",
  size = "full",
  isTerminal,
  isSuccess,
  receiptText = "Ready",
  pendingLabels = [],
  stepsPresentation = "full",
}: NovaActivityFeedProps) {
  const [manualOverrides, setManualOverrides] = useState<Record<string, boolean>>({});
  const [showFullList, setShowFullList] = useState(false);
  const listId = useId();

  // Announce each newly-arrived step's label exactly once.
  const announcedIdsRef = useRef<Set<string>>(new Set());
  const [announceText, setAnnounceText] = useState("");
  useEffect(() => {
    if (!steps || steps.length === 0) return;
    const unannounced = steps.filter((s) => !announcedIdsRef.current.has(s.id));
    if (unannounced.length === 0) return;
    unannounced.forEach((s) => announcedIdsRef.current.add(s.id));
    setAnnounceText(unannounced[unannounced.length - 1].label);
  }, [steps]);

  if (!steps || steps.length === 0) return null;

  const isFailed = isTerminal && !isSuccess;
  const doneCount = steps.filter((s) => s.status === "done").length;
  const totalCount = steps.length;
  const latestStep =
    (!isTerminal
      ? [...steps].reverse().find((step) => step.status === "active")
      : undefined) ?? steps[steps.length - 1];

  function isExpanded(step: NovaStep): boolean {
    if (step.id in manualOverrides) return manualOverrides[step.id];
    return step.status === "active";
  }

  function toggle(step: NovaStep) {
    setManualOverrides((prev) => ({ ...prev, [step.id]: !isExpanded(step) }));
  }

  const announceRegion = (
    <span className="sr-only" role="status" aria-live="polite">
      {announceText}
    </span>
  );

  const stepList = (
    <ul role="list" aria-label="Nova AI steps" className="space-y-0.5">
      {steps.map((step) => (
        <NovaStepRow
          key={step.id}
          step={step}
          tone={tone}
          size={size}
          expanded={isExpanded(step)}
          onToggle={() => toggle(step)}
        />
      ))}
      {!isTerminal &&
        pendingLabels.map((label, idx) => (
          <NovaPendingRow key={`pending-${idx}`} label={label} tone={tone} size={size} />
        ))}
    </ul>
  );

  if (stepsPresentation === "disclosure") {
    const successReceipt = isTerminal && isSuccess;
    const checkColor = tone === "light" ? "text-lime-700" : "text-amber-300";
    const mutedColor = tone === "light" ? "text-[#71717a]" : "text-zinc-500";
    const linkColor =
      tone === "light"
        ? "text-lime-700 hover:text-lime-800"
        : "text-amber-300 hover:text-amber-200";

    return (
      <div>
        {announceRegion}
        <p className={`flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-sm ${mutedColor}`}>
          <span
            className={`flex items-center gap-1.5 font-medium ${
              successReceipt
                ? checkColor
                : tone === "light"
                  ? "text-[#3f3f46]"
                  : "text-zinc-200"
            }`}
          >
            {successReceipt && <span aria-hidden="true">✓</span>}
            <span>{successReceipt ? receiptText : latestStep.label}</span>
          </span>
          <span aria-hidden="true">·</span>
          <span>
            {successReceipt
              ? `${totalCount} step${totalCount === 1 ? "" : "s"}`
              : `${doneCount} completed`}
          </span>
          <span aria-hidden="true">·</span>
          <Button
            type="button"
            variant="link"
            onClick={() => setShowFullList((visible) => !visible)}
            aria-expanded={showFullList}
            aria-controls={listId}
            className={`h-auto min-h-11 p-0 underline underline-offset-2 hover:no-underline ${linkColor}`}
          >
            {showFullList ? "Hide analysis steps" : "Show analysis steps"}
          </Button>
        </p>
        <div id={listId} hidden={!showFullList}>
          {showFullList && stepList}
          {showFullList && isFailed && (
            <p
              className={`mt-2 text-xs ${tone === "light" ? "text-[#71717a]" : "text-zinc-500"}`}
            >
              Completed {doneCount} of {totalCount} steps before stopping
            </p>
          )}
        </div>
      </div>
    );
  }

  // State (c): terminal + success collapses into a one-line receipt.
  if (!showFullList && isTerminal && isSuccess) {
    const checkColor = tone === "light" ? "text-lime-700" : "text-amber-300";
    const mutedColor = tone === "light" ? "text-[#71717a]" : "text-zinc-500";
    const linkColor =
      tone === "light" ? "text-lime-700 hover:text-lime-800" : "text-amber-300 hover:text-amber-200";
    return (
      <div>
        {announceRegion}
        <p className={`flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-sm ${mutedColor}`}>
          <span
            className={`flex items-center gap-1.5 font-medium ${
              checkColor
            }`}
          >
            <span aria-hidden="true">✓</span>
            <span>{receiptText}</span>
          </span>
          <span aria-hidden="true">·</span>
          <span>
            {totalCount} step{totalCount === 1 ? "" : "s"}
          </span>
          <span aria-hidden="true">·</span>
          <Button
            type="button"
            variant="link"
            onClick={() => setShowFullList(true)}
            aria-expanded="false"
            aria-controls={listId}
            className={`h-auto p-0 underline underline-offset-2 ${linkColor}`}
          >
            See what Nova did
          </Button>
        </p>
        <span id={listId} hidden />
      </div>
    );
  }

  return (
    <div>
      {announceRegion}
      <div id={listId}>{stepList}</div>
      {isFailed && (
        <p className={`mt-2 text-xs ${tone === "light" ? "text-[#71717a]" : "text-zinc-500"}`}>
          Completed {doneCount} of {totalCount} steps before stopping
        </p>
      )}
      {showFullList && isTerminal && isSuccess && (
        <Button
          type="button"
          variant="link"
          onClick={() => setShowFullList(false)}
          aria-expanded="true"
          aria-controls={listId}
          className={`h-auto p-0 mt-1 text-xs underline underline-offset-2 ${
            tone === "light" ? "text-[#71717a] hover:text-[#0c0c0e]" : "text-zinc-500 hover:text-zinc-200"
          }`}
        >
          Hide steps
        </Button>
      )}
    </div>
  );
}
