"use client";

/**
 * StepRail — the shared step-progress rail for the split-rail setup shells
 * (onboarding + the transcript/record takeover).
 *
 * Two presentations, one source of truth (DESIGN.md §8 responsive baseline):
 *
 *  - `md` and up: the docked vertical rail — brand eyebrow + dot/label list,
 *    `w-56` with a right border. Unchanged from the pre-extraction design.
 *  - below `md`: a horizontal strip beneath the header — a dot per step plus the
 *    active step's label. A 224px rail on a 390px viewport left 70px for
 *    content, and because flex items default to `min-width: auto` the pane
 *    could not shrink into it — the row overflowed the viewport instead.
 *
 * Callers render ONE <StepRail>; it emits both presentations and gates them by
 * breakpoint. The parent must be `flex-col md:flex-row` so the strip lands as a
 * full-width row on phones and the aside becomes the left column on desktop.
 *
 * Step state is computed by the caller (each flow has its own rules about what
 * is revisitable) and passed in; the rail only paints it. Use `stepState` for
 * the common done/active/upcoming derivation.
 *
 * The strip deliberately carries NO "n of N" counter: the dots already convey
 * position, and each step's own eyebrow states it in words where it matters.
 */

import { BRAND_NAME } from "@/lib/brand";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";

export type StepRailState = "done" | "active" | "upcoming" | "skipped";

export interface StepRailStep<K extends string | number = string | number> {
  /** Stable identity, also passed back to onGoBack. */
  key: K;
  label: string;
  state: StepRailState;
  /** Whether this step can be navigated back to. */
  clickable?: boolean;
  /** Desktop-only suffix, e.g. a "✓" or "(skipped)" marker. */
  note?: { text: string; tone: "lime" | "zinc" };
}

/**
 * done / active / upcoming from a step's position relative to the current one.
 * Shared so each flow doesn't re-derive the same three-way branch; flows layer
 * their own extra states (e.g. onboarding's "skipped") on top of the result.
 */
export function stepState(n: number, current: number): StepRailState {
  if (n < current) return "done";
  if (n === current) return "active";
  return "upcoming";
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

/** DESIGN.md §8: visible focus on every interactive element. */
const FOCUS_RING =
  "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500";

export interface StepRailProps<K extends string | number> {
  steps: StepRailStep<K>[];
  onGoBack: (key: K) => void;
}

export function StepRail<K extends string | number>({
  steps,
  onGoBack,
}: StepRailProps<K>) {
  const active = steps.find((s) => s.state === "active") ?? null;

  return (
    <>
      {/* ── Phone: horizontal strip ─────────────────────────────────────────
          Height comes from the 44px dot boxes (DESIGN.md §8 touch targets), so
          the strip costs one compact row and zero horizontal width. Every dot
          gets the same 44px box whether or not it is interactive, so the track
          reads as an evenly spaced rhythm. */}
      <nav
        aria-label="Progress"
        className="flex items-center gap-2 border-b border-zinc-200 bg-white px-5 md:hidden"
      >
        <ol className="flex items-center">
          {steps.map((step) => {
            const dot = (
              <span
                aria-hidden
                className={cn("h-[7px] w-[7px] rounded-full", dotClass(step.state))}
              />
            );
            const box = "flex h-11 w-11 items-center justify-center";

            if (step.clickable) {
              return (
                <li key={step.key}>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    onClick={() => onGoBack(step.key)}
                    className={cn("h-11 w-11 rounded-full p-0 hover:bg-transparent hover:opacity-70", FOCUS_RING)}
                  >
                    {dot}
                    <span className="sr-only">{`Back to ${step.label}`}</span>
                  </Button>
                </li>
              );
            }

            return (
              <li key={step.key}>
                <span
                  className={box}
                  aria-current={step.state === "active" ? "step" : undefined}
                >
                  {dot}
                  {/* The active step is already named by the visible label to
                      the right; labelling its dot too would announce twice.
                      Skipped carries its suffix here because `note` is
                      desktop-only — without it a skipped step would be
                      indistinguishable from one not yet reached. */}
                  {step.state !== "active" && (
                    <span className="sr-only">
                      {step.state === "skipped"
                        ? `${step.label} (skipped)`
                        : step.label}
                    </span>
                  )}
                </span>
              </li>
            );
          })}
        </ol>

        {active && (
          <p className="min-w-0 flex-1 truncate text-[13px] font-semibold text-[#0c0c0e]">
            {active.label}
          </p>
        )}
      </nav>

      {/* ── Desktop: docked vertical rail ──────────────────────────────────── */}
      <aside className="hidden w-56 shrink-0 flex-col border-r border-zinc-200 bg-white px-8 py-10 md:flex">
        <p className="text-xs font-semibold uppercase tracking-widest text-[#3f3f46]">
          {BRAND_NAME}
        </p>

        <ol aria-label="Progress" className="mt-10 flex flex-col gap-6">
          {steps.map((step) => (
            <li key={step.key}>
              <Button
                type="button"
                variant="ghost"
                disabled={!step.clickable}
                onClick={() => step.clickable && onGoBack(step.key)}
                aria-current={step.state === "active" ? "step" : undefined}
                className={cn(
                  "h-auto w-full justify-start rounded p-0 text-left text-sm font-normal hover:bg-transparent disabled:opacity-100",
                  step.clickable
                    ? cn("cursor-pointer transition-opacity hover:opacity-70", FOCUS_RING)
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
              </Button>
            </li>
          ))}
        </ol>
      </aside>
    </>
  );
}
