"use client";

/**
 * StepRail — the shared step-progress rail for the split-rail setup shells
 * (onboarding + the transcript/record takeover).
 *
 * Two presentations, one source of truth (DESIGN.md §8 responsive baseline):
 *
 *  - `md` and up: the docked vertical rail — brand eyebrow + dot/label list,
 *    `w-56` with a right border. Unchanged from the pre-extraction design.
 *  - below `md`: a horizontal strip beneath the header — dots + the active
 *    step's label + an "n of N" counter. A 224px rail on a 390px viewport left
 *    70px for content, and because flex items default to `min-width: auto` the
 *    pane could not shrink into it — the row overflowed the viewport instead.
 *
 * Callers render ONE <StepRail>; it emits both presentations and gates them by
 * breakpoint. The parent must be `flex-col md:flex-row` so the strip lands as a
 * full-width row on phones and the aside becomes the left column on desktop.
 *
 * Step state is computed by the caller (each flow has its own rules about what
 * is revisitable) and passed in; the rail only paints it.
 */

import { BRAND_NAME } from "@/lib/brand";
import { cn } from "@/lib/cn";

export type StepRailState = "done" | "active" | "upcoming" | "skipped";

export interface StepRailStep {
  /** Stable identity, also passed back to onGoBack. */
  key: string | number;
  label: string;
  state: StepRailState;
  /** Whether this step can be navigated back to. */
  clickable?: boolean;
  /** Desktop-only suffix, e.g. a "✓" or "(skipped)" marker. */
  note?: { text: string; tone: "lime" | "zinc" };
}

function dotClass(state: StepRailState): string {
  if (state === "done") return "bg-lime-600";
  if (state === "active") return "bg-[#0c0c0e]";
  // upcoming + skipped both read as inert.
  return "bg-zinc-300";
}

function labelClass(state: StepRailState): string {
  if (state === "active") return "text-[#0c0c0e] font-semibold";
  if (state === "done") return "text-[#3f3f46]";
  return "text-[#a1a1aa]";
}

function noteClass(tone: "lime" | "zinc"): string {
  return tone === "lime" ? "text-lime-600" : "text-[#a1a1aa]";
}

export interface StepRailProps {
  steps: StepRailStep[];
  onGoBack: (key: string | number) => void;
}

export function StepRail({ steps, onGoBack }: StepRailProps) {
  const activeIndex = steps.findIndex((s) => s.state === "active");
  const active = activeIndex >= 0 ? steps[activeIndex] : null;

  return (
    <>
      {/* ── Phone: horizontal strip ─────────────────────────────────────────
          Height comes from the 44px dot buttons (DESIGN.md §8 touch targets),
          so the strip costs one compact row and zero horizontal width. */}
      <nav
        aria-label="Progress"
        className="flex items-center gap-3 border-b border-zinc-200 bg-white px-5 md:hidden"
      >
        <ol className="flex items-center">
          {steps.map((step) => {
            const isClickable = Boolean(step.clickable);
            const dot = (
              <span
                aria-hidden
                className={cn("h-[7px] w-[7px] rounded-full", dotClass(step.state))}
              />
            );
            // Only revisitable dots are interactive, so only they need the full
            // 44x44 target; inert dots stay narrow to keep the track compact.
            const box = "flex h-11 items-center justify-center";
            return (
              <li key={step.key}>
                {isClickable ? (
                  <button
                    type="button"
                    onClick={() => onGoBack(step.key)}
                    className={cn(box, "w-11 transition-opacity hover:opacity-70")}
                  >
                    {dot}
                    <span className="sr-only">{`Back to ${step.label}`}</span>
                  </button>
                ) : (
                  <span
                    className={cn(box, "w-8")}
                    aria-current={step.state === "active" ? "step" : undefined}
                  >
                    {dot}
                    {/* The active step is already named by the visible label to
                        the right; labelling its dot too would announce twice. */}
                    {step.state !== "active" && (
                      <span className="sr-only">{step.label}</span>
                    )}
                  </span>
                )}
              </li>
            );
          })}
        </ol>

        {active && (
          <p className="min-w-0 flex-1 truncate text-[13px] font-semibold text-[#0c0c0e]">
            {active.label}
          </p>
        )}
        {active && (
          <p className="shrink-0 text-[11px] uppercase tracking-wide text-[#a1a1aa]">
            {activeIndex + 1} of {steps.length}
          </p>
        )}
      </nav>

      {/* ── Desktop: docked vertical rail ──────────────────────────────────── */}
      <aside className="hidden w-56 shrink-0 flex-col border-r border-zinc-200 bg-white px-8 py-10 md:flex">
        <p className="text-xs font-semibold uppercase tracking-widest text-[#3f3f46]">
          {BRAND_NAME}
        </p>

        <ol aria-label="Progress" className="mt-10 flex flex-col gap-6">
          {steps.map((step) => {
            const isClickable = Boolean(step.clickable);
            return (
              <li key={step.key}>
                <button
                  type="button"
                  disabled={!isClickable}
                  onClick={() => isClickable && onGoBack(step.key)}
                  aria-current={step.state === "active" ? "step" : undefined}
                  className={cn(
                    "flex items-center gap-3 text-left text-sm",
                    isClickable
                      ? "cursor-pointer transition-opacity hover:opacity-70"
                      : "cursor-default",
                    labelClass(step.state),
                  )}
                >
                  <span
                    className={cn(
                      "h-[7px] w-[7px] shrink-0 rounded-full",
                      dotClass(step.state),
                    )}
                  />
                  <span>
                    {step.label}
                    {step.note && (
                      <span className={cn("ml-1 text-xs", noteClass(step.note.tone))}>
                        {step.note.text}
                      </span>
                    )}
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      </aside>
    </>
  );
}
