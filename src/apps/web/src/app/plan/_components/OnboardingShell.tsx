"use client";

/**
 * OnboardingShell — Split-rail onboarding wrapper (Build Goal #1).
 *
 * Layout:
 *   LEFT RAIL (w-56, fixed, full height):
 *     4 steps as a vertical progress rail (dot + label).
 *     Done → lime dot + checkmark; Active → ink dot; Upcoming → zinc dot.
 *     Clicking a DONE step goes back to it; upcoming steps are non-interactive.
 *
 *   RIGHT PANE (flex-1, max-w-lg centered):
 *     Step 1 — TikTok: <TikTokPreScreen>
 *     Step 2 — What you make: 4 multi-select toggle cards
 *     Step 3 — Style: <ChatInterview> while persona building, then <PersonaEditor>
 *     Step 4 — First plan: navigate to /plan workspace (handled by caller)
 *
 * Internal branch on TikTok signal:
 *   - Handle submitted with reach → Step 1 done, advance to Step 2.
 *   - Skipped / failed → Step 1 shown as "Skipped" (zinc dot), still advance.
 *
 * Step ordering is linear: TikTok → What you make → AI interview → Persona reveal → plan.
 * The edit/footage funnel is available from the workspace, not here.
 *
 * This component owns its own step-index state and drives the visual rail;
 * page.tsx remains the API orchestrator (it still owns persona/plan state and
 * the mode-resolution logic for workspace vs setup).
 */

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import type { PersonaContent, PersonaResponse } from "@/lib/plan-api";
import { patchPersonaFootageType } from "@/lib/plan-api";
import TikTokPreScreen from "./TikTokPreScreen";
import ChatInterview from "./ChatInterview";
import PersonaEditor from "./PersonaEditor";
import { GeneratingStateLight } from "./GeneratingStateLight";

// ── Types ─────────────────────────────────────────────────────────────────────

type OnboardingStep = 1 | 2 | 3 | 4;

type TikTokStatus = "pending" | "done" | "skipped";

export interface OnboardingShellProps {
  /** Called when TikTok handle submitted (may be empty string = skip). */
  onTikTokContinue: (handle: string) => Promise<void>;
  /** Whether the TikTok scrape is in progress. */
  tiktokBusy?: boolean;
  /** Current persona response from the server. */
  persona: PersonaResponse | null;
  /** Whether we're busy creating the plan (Step 4). */
  planBusy?: boolean;
  /** Persist persona field edits (Step 3, PersonaEditor). */
  onSavePersona: (draft: PersonaContent) => Promise<void>;
  /** Called when persona interview completes (fires persona generation). */
  onChatComplete: () => void;
  /** Called when PersonaEditor "Continue" is clicked — triggers plan creation. */
  onContinueToPlan: () => void;
  /** Re-tune persona from feedback. */
  onRetune?: () => Promise<void>;
  /** Error string to show at the top of the right pane. */
  error?: string | null;
}

// ── Footage type options ───────────────────────────────────────────────────────

interface FootageOption {
  /** Value stored in persona.footage_type_bias */
  value: string;
  label: string;
  description: string;
}

const FOOTAGE_OPTIONS: FootageOption[] = [
  {
    value: "talking_head",
    label: "Talking to camera",
    description: "You're the subject, speaking directly",
  },
  {
    value: "montage",
    label: "B-roll & nature",
    description: "Cinematic clips, environments, beauty shots",
  },
  {
    value: "day_vlog",
    label: "Vlogs & daily life",
    description: "Day-in-the-life, candid moments, routines",
  },
  {
    value: "mixed",
    label: "Mixed",
    description: "A bit of everything — Nova will adapt",
  },
];

// ── Step slide transition ─────────────────────────────────────────────────────
// Uses t-page (#8) motion values from globals.css. Mount with step-slide,
// add is-entered on the next rAF so the CSS transition fires (opacity/translateX/blur).
// Parent passes `key={step}` so React remounts this on every step change —
// each new step plays the entrance fresh. Reduced-motion guard in globals.css.

function StepSlide({ children }: { children: React.ReactNode }) {
  const [entered, setEntered] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(raf);
  }, []);
  return (
    <div className={`step-slide${entered ? " is-entered" : ""}`}>
      {children}
    </div>
  );
}

// ── Step rail ────────────────────────────────────────────────────────────────

const STEP_LABELS: Record<OnboardingStep, string> = {
  1: "TikTok",
  2: "What you make",
  3: "Style",
  4: "First plan",
};

function StepRail({
  current,
  tiktokStatus,
  onGoBack,
}: {
  current: OnboardingStep;
  tiktokStatus: TikTokStatus;
  onGoBack: (step: OnboardingStep) => void;
}) {
  return (
    <aside className="flex w-56 shrink-0 flex-col border-r border-zinc-200 bg-white px-8 py-10">
      <p className="text-xs font-semibold uppercase tracking-widest text-[#3f3f46]">Nova</p>

      <ol className="mt-10 flex flex-col gap-6">
        {([1, 2, 3, 4] as OnboardingStep[]).map((n) => {
          const isDone = n < current;
          const isActive = n === current;
          const isSkipped = n === 1 && tiktokStatus === "skipped";
          // Only done (non-skipped) steps before current are clickable.
          const isClickable = isDone && !isSkipped && n < current;

          let dotColor: string;
          if (isSkipped) {
            dotColor = "bg-zinc-300";
          } else if (isDone) {
            dotColor = "bg-lime-600";
          } else if (isActive) {
            dotColor = "bg-[#0c0c0e]";
          } else {
            dotColor = "bg-zinc-300";
          }

          let textColor: string;
          if (isActive) {
            textColor = "text-[#0c0c0e] font-semibold";
          } else if (isDone && !isSkipped) {
            textColor = "text-[#3f3f46]";
          } else {
            textColor = "text-[#a1a1aa]";
          }

          return (
            <li key={n}>
              <button
                type="button"
                disabled={!isClickable}
                onClick={() => isClickable && onGoBack(n)}
                className={cn(
                  "flex items-center gap-3 text-left text-sm",
                  isClickable && "cursor-pointer transition-opacity hover:opacity-70",
                  !isClickable && "cursor-default",
                  textColor,
                )}
              >
                <span
                  className={cn("h-[7px] w-[7px] shrink-0 rounded-full", dotColor)}
                />
                <span>
                  {STEP_LABELS[n]}
                  {isSkipped && (
                    <span className="ml-1 text-xs text-[#a1a1aa]">(skipped)</span>
                  )}
                  {isDone && !isSkipped && (
                    <span className="ml-1 text-xs text-lime-600">✓</span>
                  )}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </aside>
  );
}

// ── Step 2: What you make (multi-select) ─────────────────────────────────────

function WhatYouMakeStep({
  preselected,
  onContinue,
}: {
  preselected?: string[];
  onContinue: (values: string[]) => Promise<void>;
}) {
  const [selected, setSelected] = useState<string[]>(preselected ?? []);
  const [saving, setSaving] = useState(false);

  function toggle(value: string) {
    setSelected((prev) =>
      prev.includes(value) ? prev.filter((v) => v !== value) : [...prev, value],
    );
  }

  async function handleContinue() {
    if (selected.length === 0 || saving) return;
    setSaving(true);
    try {
      await onContinue(selected);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div>
      <p className="text-xs font-semibold uppercase tracking-widest text-lime-700">
        Step 2 of 4
      </p>
      <h1 className="mt-4 font-display text-4xl font-medium leading-tight tracking-tight text-[#0c0c0e]">
        What do you make?
      </h1>
      <p className="mt-3 text-[#71717a]">
        Pick all that apply — Nova adapts edits to your style.
      </p>

      <div className="mt-10 grid grid-cols-2 gap-4">
        {FOOTAGE_OPTIONS.map((opt) => {
          const isSelected = selected.includes(opt.value);
          return (
            <button
              key={opt.value}
              type="button"
              onClick={() => toggle(opt.value)}
              className={cn(
                "flex flex-col gap-1 rounded-2xl border p-6 text-left transition-all",
                isSelected
                  ? "border-transparent bg-lime-50 outline outline-2 outline-lime-500"
                  : "border-zinc-200 bg-white hover:border-zinc-300",
              )}
            >
              <span className="font-display text-lg font-medium text-[#0c0c0e]">
                {opt.label}
              </span>
              <span className="text-xs text-[#71717a]">{opt.description}</span>
            </button>
          );
        })}
      </div>

      <button
        type="button"
        onClick={handleContinue}
        disabled={selected.length === 0 || saving}
        className="mt-10 inline-flex min-h-[48px] items-center rounded-full bg-[#0c0c0e] px-9 py-[15px] text-[15px] font-semibold text-white transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {saving ? "Saving…" : "Continue →"}
      </button>
    </div>
  );
}

// ── Derive the right initial step from server state ───────────────────────────
// Used as a lazy useState initializer so that on remount (e.g. after the edit
// funnel finishes) the shell opens at the correct step rather than always
// resetting to TikTok.

function deriveInitialStep(persona: PersonaResponse | null): OnboardingStep {
  // No persona row yet → start at TikTok.
  if (!persona) return 1;

  const status = persona.persona_status;

  // Persona already produced → always land on the reveal/Style step.
  // A returning user who has a persona should never see TikTok again.
  if (status === "ready" || status === "edited" || status === "failed" || status === "generating") {
    return 3;
  }

  // chat_pending: did the user already pick "What you make"?
  // footage_type_bias is written by patchPersonaFootageType (Step 2 completion).
  const bias = persona.persona?.footage_type_bias;
  const madeChoice = Array.isArray(bias) && bias.length > 0;
  if (madeChoice) return 3; // interview already started

  // chat_pending with no bias → What you make (TikTok was already done; a persona
  // row only exists once the TikTok step or chatStart created it).
  return 2;
}

// ── Main shell ────────────────────────────────────────────────────────────────

export default function OnboardingShell({
  onTikTokContinue,
  tiktokBusy,
  persona,
  planBusy,
  onSavePersona,
  onChatComplete,
  onContinueToPlan,
  onRetune,
  error,
}: OnboardingShellProps) {
  // Lazy initializers: derive the right step/tiktokStatus from server state on
  // mount so that a remount (e.g. after page navigates back from the edit funnel)
  // doesn't drop the user back to TikTok.
  const [step, setStep] = useState<OnboardingStep>(() => deriveInitialStep(persona));
  const [tiktokStatus, setTiktokStatus] = useState<TikTokStatus>(() =>
    persona ? "done" : "pending",
  );
  // Holds footage_type_bias selections made in step 2 when persona doesn't exist yet.
  // Patched to the persona row as soon as chatStart() creates it.
  const pendingBiasRef = useRef<string[]>([]);

  // ── Step 1: TikTok ────────────────────────────────────────────────────────

  async function handleTikTok(handle: string) {
    await onTikTokContinue(handle);
    setTiktokStatus(handle ? "done" : "skipped");
    setStep(2);
  }

  // ── Step 2: What you make ─────────────────────────────────────────────────

  async function handleWhatYouMake(values: string[]) {
    if (persona) {
      // Best-effort persist — non-blocking; onboarding continues regardless.
      // Backend keeps chat_pending status when only footage_type_bias is being set,
      // so the interview still runs after this write.
      await patchPersonaFootageType(persona.id, values).catch(() => undefined);
    } else {
      // No persona row yet (user skipped TikTok). Store values for patching
      // once chatStart() creates the row and fires onPersonaCreated.
      pendingBiasRef.current = values;
    }
    setStep(3);
  }

  function handlePersonaCreated(personaId: string) {
    if (pendingBiasRef.current.length > 0) {
      patchPersonaFootageType(personaId, pendingBiasRef.current).catch(() => undefined);
      pendingBiasRef.current = [];
    }
  }

  // ── Step 3: Chat complete (persona generation starts) ─────────────────────

  function handleChatComplete() {
    onChatComplete();
    // Stay on step 3; PersonaEditor will appear once persona_status is ready/edited.
  }

  // ── Step 3: PersonaEditor continue → plan ─────────────────────────────────

  function handlePersonaContinue() {
    setStep(4);
    onContinueToPlan();
  }

  // ── Go-back handler ───────────────────────────────────────────────────────

  function goBack(target: OnboardingStep) {
    // Only allow going back to done steps (enforced in StepRail via disabled).
    setStep(target);
  }

  // ── Determine what to show in Step 3 right pane ───────────────────────────
  // persona_status drives: chat_pending → ChatInterview, generating → spinner,
  // ready/edited/failed → PersonaEditor.
  function renderStep3() {
    if (!persona) {
      // No persona row yet — show ChatInterview so chatStart() fires and creates it.
      // (TikTok was skipped or scrape returned nothing; interview creates the row.)
      return (
        <ChatInterview onComplete={handleChatComplete} onPersonaCreated={handlePersonaCreated} />
      );
    }

    const status = persona.persona_status;

    if (status === "generating") {
      return <GeneratingStateLight label="Building your persona…" />;
    }

    if ((status === "ready" || status === "edited" || status === "failed") && persona.persona) {
      return (
        <PersonaEditor
          persona={persona.persona}
          status={persona.persona_status}
          onSave={onSavePersona}
          onContinue={handlePersonaContinue}
          continueLabel="Get my ideas →"
          continuing={planBusy}
          onRetuneFromFeedback={onRetune}
          tiktokProfile={persona.tiktok_profile}
        />
      );
    }

    // chat_pending or any other status → show ChatInterview.
    return <ChatInterview onComplete={handleChatComplete} onPersonaCreated={handlePersonaCreated} />;
  }

  // ── Layout ────────────────────────────────────────────────────────────────

  return (
    <div className="flex min-h-screen bg-[#fafaf8]">
      <StepRail current={step} tiktokStatus={tiktokStatus} onGoBack={goBack} />

      {/* Right pane */}
      <main className="flex flex-1 items-start justify-center px-12 py-16">
        <div className="w-full max-w-lg">
          {/* Error banner (outside the slide so it doesn't re-animate on step change) */}
          {error && (
            <div className="mb-6 rounded border border-zinc-200 bg-white px-4 py-3 text-sm text-[#3f3f46]">
              {error}
            </div>
          )}

          {/* StepSlide: key=step remounts on each advance, replaying the
              slide-from-right entrance (t-page values) per step. */}
          <StepSlide key={step}>
            {/* Step 1 — TikTok */}
            {step === 1 && (
              <TikTokPreScreen onContinue={handleTikTok} submitting={tiktokBusy} />
            )}

            {/* Step 2 — What you make (multi-select) */}
            {step === 2 && (
              <WhatYouMakeStep
                preselected={persona?.persona?.footage_type_bias as string[] | undefined}
                onContinue={handleWhatYouMake}
              />
            )}

            {/* Step 3 — Style */}
            {step === 3 && renderStep3()}

            {/* Step 4 — Navigating to plan (transient) */}
            {step === 4 && <GeneratingStateLight label="Building your ideas…" />}
          </StepSlide>
        </div>
      </main>
    </div>
  );
}
