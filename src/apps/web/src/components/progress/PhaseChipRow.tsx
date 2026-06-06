"use client";

import { useEffect, useRef } from "react";

interface PhaseChipRowProps {
  /** Ordered list of all phase names. */
  phases: readonly string[];
  /** Human-readable label for each phase. */
  phaseLabels: Record<string, string>;
  /** Currently active phase name, or null if none. */
  currentPhase: string | null;
}

type ChipState = "done" | "active" | "pending";

function chipState(
  phase: string,
  phases: readonly string[],
  currentPhase: string | null,
): ChipState {
  if (!currentPhase) return "pending";
  const currentIdx = phases.indexOf(currentPhase);
  const phaseIdx = phases.indexOf(phase);
  if (phaseIdx < currentIdx) return "done";
  if (phaseIdx === currentIdx) return "active";
  return "pending";
}

/**
 * Horizontal scrollable row of phase chips.
 *
 * - done: zinc text + checkmark
 * - active: amber bg + animate-ping halo (reduced motion: bg only, no ping)
 * - pending: dim zinc
 *
 * D16: overflow-x-auto with hidden scrollbar, 24px fade masks on both edges.
 * On active change: scrollIntoView({ inline: 'center' }), respecting prefers-reduced-motion.
 */
export function PhaseChipRow({ phases, phaseLabels, currentPhase }: PhaseChipRowProps) {
  const activeRef = useRef<HTMLDivElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!activeRef.current) return;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    activeRef.current.scrollIntoView({
      inline: "center",
      block: "nearest",
      behavior: reducedMotion ? "instant" : "smooth",
    });
  }, [currentPhase]);

  return (
    <div className="relative">
      {/* Left fade mask */}
      <div
        className="pointer-events-none absolute left-0 top-0 bottom-0 z-10 w-6 bg-gradient-to-r from-black to-transparent"
        aria-hidden="true"
      />
      {/* Right fade mask */}
      <div
        className="pointer-events-none absolute right-0 top-0 bottom-0 z-10 w-6 bg-gradient-to-l from-black to-transparent"
        aria-hidden="true"
      />

      {/* Scrollable chips */}
      <div
        ref={containerRef}
        className="flex gap-2 overflow-x-auto px-6 py-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        role="list"
        aria-label="Processing phases"
      >
        {phases.map((phase) => {
          const state = chipState(phase, phases, currentPhase);
          return (
            <Chip
              key={phase}
              phase={phase}
              label={phaseLabels[phase] ?? phase}
              state={state}
              ref={state === "active" ? activeRef : null}
            />
          );
        })}
      </div>
    </div>
  );
}

interface ChipProps {
  phase: string;
  label: string;
  state: ChipState;
  ref?: React.Ref<HTMLDivElement>;
}

function Chip({ phase: _phase, label, state, ref }: ChipProps) {
  if (state === "done") {
    return (
      <div
        ref={ref}
        role="listitem"
        className="flex shrink-0 items-center gap-1.5 rounded-full border border-zinc-700 px-3 py-1 text-xs text-zinc-400"
      >
        <svg
          className="h-3 w-3 text-zinc-500"
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
        {label}
      </div>
    );
  }

  if (state === "active") {
    return (
      <div
        ref={ref}
        role="listitem"
        aria-current="step"
        className="relative flex shrink-0 items-center gap-1.5 rounded-full border border-amber-400/60 bg-amber-400/10 px-3 py-1 text-xs font-medium text-amber-300"
      >
        {/* Ping halo — hidden when reduced motion is preferred */}
        <span className="relative flex h-2 w-2 shrink-0" aria-hidden="true">
          <span className="motion-safe:animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400/60" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-amber-400" />
        </span>
        {label}
      </div>
    );
  }

  // pending
  return (
    <div
      ref={ref}
      role="listitem"
      className="flex shrink-0 items-center gap-1.5 rounded-full border border-zinc-800 px-3 py-1 text-xs text-zinc-600"
    >
      {label}
    </div>
  );
}
