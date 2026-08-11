"use client";

import type { NovaStep } from "@/lib/job-phases";

interface NovaStepRowProps {
  step: NovaStep;
  tone: "dark" | "light";
  size: "full" | "compact";
  expanded: boolean;
  onToggle: () => void;
}

/**
 * One row in NovaActivityFeed.
 *
 * - done: zinc check + ink label.
 * - active: lime dot + motion-safe ping halo + pale lime row bg (light tone);
 *   amber equivalents on dark tone (D20 — amber stays a dark-render-system
 *   accent, never lime on dark).
 * - failed: zinc treatment, no red (D10) — a plain dash, not a check.
 * - Detail lines (when `step.detail` is non-empty) sit behind a chevron
 *   `<button aria-expanded>`; the reveal uses the `.t-accordion` grid-rows
 *   token (DESIGN.md §6) so the row's own height animates, not just opacity.
 */
export function NovaStepRow({ step, tone, size, expanded, onToggle }: NovaStepRowProps) {
  const hasDetail = !!step.detail && step.detail.length > 0;
  const compact = size === "compact";

  const rowPad = compact ? "px-2.5 py-1.5" : "px-3 py-2";
  const textSize = compact ? "text-xs" : "text-sm";
  const rowBg =
    step.status === "active"
      ? tone === "light"
        ? "bg-lime-50"
        : "bg-amber-400/10"
      : "";

  const labelColor =
    step.status === "active"
      ? tone === "light"
        ? "text-[#0c0c0e] font-medium"
        : "text-amber-200 font-medium"
      : tone === "light"
        ? "text-[#0c0c0e]"
        : "text-zinc-200";

  return (
    <li
      role="listitem"
      className={`rounded-lg ${rowBg} transition-colors`}
    >
      <div className={`flex items-start gap-2.5 ${rowPad}`}>
        <StepIcon step={step} tone={tone} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className={`${textSize} ${labelColor} leading-snug`}>{step.label}</span>
            {hasDetail && (
              <button
                type="button"
                aria-expanded={expanded}
                aria-label={expanded ? `Hide details for ${step.label}` : `Show details for ${step.label}`}
                onClick={onToggle}
                className={`inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full transition-transform ${
                  tone === "light" ? "text-[#71717a] hover:text-[#0c0c0e]" : "text-zinc-500 hover:text-zinc-200"
                } ${expanded ? "rotate-180" : ""}`}
              >
                <ChevronIcon />
              </button>
            )}
          </div>
          {hasDetail && (
            <div className={`t-accordion ${expanded ? "is-open" : ""}`}>
              <div>
                {/* Detail lines are prose, not sub-steps — a plain div stack
                    keeps them out of the feed's role="listitem" count. */}
                <div
                  className={`mt-1 space-y-0.5 border-l-2 pl-2.5 ${
                    tone === "light" ? "border-lime-300" : "border-amber-400/40"
                  }`}
                >
                  {step.detail!.map((line, idx) => (
                    <p
                      key={idx}
                      className={`${compact ? "text-[11px]" : "text-xs"} ${
                        tone === "light" ? "text-[#71717a]" : "text-zinc-400"
                      }`}
                    >
                      {line}
                    </p>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </li>
  );
}

function StepIcon({ step, tone }: { step: NovaStep; tone: "dark" | "light" }) {
  if (step.status === "done") {
    const color = tone === "light" ? "text-[#71717a]" : "text-zinc-500";
    return (
      <svg
        className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${color}`}
        viewBox="0 0 12 12"
        fill="none"
        aria-hidden="true"
      >
        <path
          d="M2 6l3 3 5-5"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  }

  if (step.status === "active") {
    const pingClass = tone === "light" ? "bg-lime-600/60" : "bg-amber-400/60";
    const dotClass = tone === "light" ? "bg-lime-600" : "bg-amber-400";
    return (
      <span className="relative mt-1 flex h-2.5 w-2.5 shrink-0" aria-hidden="true">
        <span className={`motion-safe:animate-ping absolute inline-flex h-full w-full rounded-full ${pingClass}`} />
        <span className={`relative inline-flex h-2.5 w-2.5 rounded-full ${dotClass}`} />
      </span>
    );
  }

  // failed — D10 zinc treatment, never red.
  const color = tone === "light" ? "text-[#a1a1aa]" : "text-zinc-600";
  return (
    <svg
      className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${color}`}
      viewBox="0 0 12 12"
      fill="none"
      aria-hidden="true"
    >
      <circle cx="6" cy="6" r="5" stroke="currentColor" strokeWidth="1.2" />
      <path d="M4 6h4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}

function ChevronIcon() {
  return (
    <svg className="h-3 w-3" viewBox="0 0 12 12" fill="none" aria-hidden="true">
      <path
        d="M3 4.5l3 3 3-3"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** A future phase not yet reached — no NovaStep exists for it yet. Derived
 *  from the phase order (GENERATIVE_PHASE_ORDER), dimmed, non-interactive. */
export function NovaPendingRow({ label, tone, size }: { label: string; tone: "dark" | "light"; size: "full" | "compact" }) {
  const compact = size === "compact";
  const rowPad = compact ? "px-2.5 py-1.5" : "px-3 py-2";
  const textSize = compact ? "text-xs" : "text-sm";
  const dimColor = tone === "light" ? "text-[#a1a1aa]" : "text-zinc-600";
  const ringColor = tone === "light" ? "border-zinc-300" : "border-zinc-700";

  return (
    <li role="listitem" className={`flex items-start gap-2.5 ${rowPad}`}>
      <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full border ${ringColor}`} aria-hidden="true" />
      <span className={`${textSize} ${dimColor} leading-snug`}>{label}</span>
    </li>
  );
}
