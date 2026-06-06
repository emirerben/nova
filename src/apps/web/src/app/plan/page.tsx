"use client";

import { useSession } from "next-auth/react";
import { useCallback, useEffect, useState } from "react";
import {
  type ContentPlan,
  createContentPlan,
  createPersona,
  getContentPlan,
  getPersona,
  NotAuthenticatedError,
  type PersonaContent,
  type PersonaQuestionnaire,
  type PersonaResponse,
  retunePersonaFromFeedback,
  tiktokScrape,
  updatePersona,
} from "@/lib/plan-api";
import ChatInterview from "./_components/ChatInterview";
import GeneratingState from "./_components/GeneratingState";
import OnboardingStep from "./_components/OnboardingStep";
import PersonaEditor from "./_components/PersonaEditor";
import PlanCalendar from "./_components/PlanCalendar";
import PlanShell from "./_components/PlanShell";
import SeedUploadCard from "./_components/SeedUploadCard";
import SignInPrompt from "./_components/SignInPrompt";
import Stepper, { type WizardStep } from "./_components/Stepper";
import TikTokPreScreen from "./_components/TikTokPreScreen";

// Sub-steps within the "you" wizard step (not shown in the Stepper dots).
type YouSubStep = "tiktok-pre-screen" | "chat" | "form";

const POLL_MS = 2000;
const ORDER: Record<WizardStep, number> = { you: 0, persona: 1, plan: 2 };

/** Furthest step unlocked by the user's data. */
function dataReached(p: PersonaResponse | null, pl: ContentPlan | null): WizardStep {
  if (pl) return "plan";
  // chat_pending means the user is still in onboarding — they haven't reached Persona yet.
  if (p && p.persona_status !== "chat_pending") return "persona";
  return "you";
}

/** Where a returning user should land based purely on their data. */
function naturalStep(p: PersonaResponse | null, pl: ContentPlan | null): WizardStep {
  if (!p) return "you";
  if (p.persona_status === "chat_pending") return "you";
  if (p.persona_status === "generating") return "persona";
  if (pl) return "plan";
  return "persona";
}

/** Sub-step within "you" derived from saved persona state (for resume). */
function naturalSubStep(p: PersonaResponse | null): YouSubStep {
  if (p?.persona_status === "chat_pending") return "chat";
  return "tiktok-pre-screen";
}

export default function PlanWizardPage() {
  const { status: authStatus } = useSession();

  const [persona, setPersona] = useState<PersonaResponse | null>(null);
  const [plan, setPlan] = useState<ContentPlan | null>(null);
  const [loading, setLoading] = useState(true);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [step, setStep] = useState<WizardStep | null>(null);
  const [subStep, setSubStep] = useState<YouSubStep>("tiktok-pre-screen");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [p, pl] = await Promise.all([getPersona(), getContentPlan()]);
      setPersona(p);
      setPlan(pl);
      return { p, pl };
    } catch (err) {
      if (err instanceof NotAuthenticatedError) setNeedsAuth(true);
      else setError(err instanceof Error ? err.message : "Failed to load your plan");
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial load (once authenticated). Skip the round-trip if we already know
  // the user is signed out.
  useEffect(() => {
    if (authStatus === "unauthenticated") {
      setNeedsAuth(true);
      setLoading(false);
      return;
    }
    if (authStatus === "authenticated") void load();
  }, [authStatus, load]);

  // Poll while either generation is in flight. Keyed on the boolean (not the
  // status string) so the interval keeps firing across polls where the status
  // stays "generating" — a status-string dependency would re-arm only when the
  // value *changes*, killing the poll after one tick. The interval clears the
  // moment nothing is generating (or on unmount).
  const isGenerating =
    persona?.persona_status === "generating" || plan?.plan_status === "generating";
  useEffect(() => {
    if (!isGenerating) return;
    const id = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(id);
  }, [isGenerating, load]);

  // Pick the initial step from ?step= (if unlocked) or the user's data.
  useEffect(() => {
    if (loading || step !== null || needsAuth) return;
    const reached = dataReached(persona, plan);
    const urlStep = new URLSearchParams(window.location.search).get("step") as WizardStep | null;
    const valid = urlStep && urlStep in ORDER && ORDER[urlStep] <= ORDER[reached];
    const ns = valid ? (urlStep as WizardStep) : naturalStep(persona, plan);
    setStep(ns);
    if (ns === "you") setSubStep(naturalSubStep(persona));
  }, [loading, step, needsAuth, persona, plan]);

  // Keep the URL in sync so refresh / share lands on the same step.
  useEffect(() => {
    if (!step) return;
    const u = new URL(window.location.href);
    u.searchParams.set("step", step);
    window.history.replaceState(null, "", u);
  }, [step]);

  // ── Handlers ────────────────────────────────────────────────────────────

  async function handleTikTokPreScreen(handle: string) {
    if (handle) {
      setBusy(true);
      try {
        const p = await tiktokScrape(handle);
        setPersona(p);
      } catch {
        // Best-effort — scrape failure is non-blocking; proceed to chat regardless.
      } finally {
        setBusy(false);
      }
    }
    setSubStep("chat");
  }

  function handleChatComplete() {
    // ChatInterview fires generate_persona on the backend; navigate to the
    // persona step and let the polling loop surface the result.
    void load();
    setStep("persona");
  }

  async function handleOnboardingSubmit(answers: PersonaQuestionnaire) {
    setBusy(true);
    setError(null);
    try {
      const p = await createPersona(answers);
      setPersona(p);
      setStep("persona");
    } catch (err) {
      if (err instanceof NotAuthenticatedError) setNeedsAuth(true);
      else setError(err instanceof Error ? err.message : "Couldn't build your persona");
    } finally {
      setBusy(false);
    }
  }

  async function handleSavePersona(draft: PersonaContent) {
    if (!persona) return;
    const updated = await updatePersona(persona.id, draft);
    setPersona(updated);
  }

  async function handleRetunePersona() {
    if (!persona) return;
    // Kicks off async re-tune (status → generating); reload to surface it + poll.
    const updated = await retunePersonaFromFeedback(persona.id);
    setPersona(updated);
  }

  async function handleCreatePlan(events: string) {
    setBusy(true);
    setError(null);
    try {
      const p = await createContentPlan(events);
      setPlan(p);
      setStep("plan");
    } catch (err) {
      if (err instanceof NotAuthenticatedError) setNeedsAuth(true);
      else setError(err instanceof Error ? err.message : "Couldn't start your plan");
    } finally {
      setBusy(false);
    }
  }

  // ── Render ──────────────────────────────────────────────────────────────
  if (needsAuth) {
    return (
      <PlanShell>
        <SignInPrompt callbackUrl="/plan" />
      </PlanShell>
    );
  }

  if (loading || step === null) {
    return (
      <PlanShell>
        <p className="py-24 text-center text-zinc-400">Loading…</p>
      </PlanShell>
    );
  }

  const reached = (() => {
    const dr = dataReached(persona, plan);
    return ORDER[step] > ORDER[dr] ? step : dr;
  })();

  return (
    <PlanShell>
      <Stepper current={step} reached={reached} onNavigate={setStep} />

      {error && (
        <div className="mb-6 rounded border border-red-700 bg-red-950/50 px-4 py-3 text-red-200">
          {error}
        </div>
      )}

      {step === "you" && subStep === "tiktok-pre-screen" && (
        <TikTokPreScreen onContinue={handleTikTokPreScreen} submitting={busy} />
      )}

      {step === "you" && subStep === "chat" && (
        <ChatInterview onComplete={handleChatComplete} />
      )}

      {step === "you" && subStep === "form" && (
        <OnboardingStep
          onSubmit={handleOnboardingSubmit}
          submitting={busy}
          initialAnswers={persona?.questionnaire ?? null}
        />
      )}

      {step === "persona" && (
        <PersonaStepView
          persona={persona}
          busy={busy}
          onSave={handleSavePersona}
          onContinue={() => setStep("plan")}
          onStartOver={() => setStep("you")}
          onRetune={handleRetunePersona}
          onUpdateAnswers={() => {
            setSubStep("tiktok-pre-screen");
            setStep("you");
          }}
        />
      )}

      {step === "plan" && (
        <PlanStepView
          plan={plan}
          busy={busy}
          onCreatePlan={handleCreatePlan}
          onError={setError}
          onRefresh={load}
          onReviewPersona={() => setStep("persona")}
        />
      )}
    </PlanShell>
  );
}

// ── Persona step ────────────────────────────────────────────────────────────
function PersonaStepView({
  persona,
  busy,
  onSave,
  onContinue,
  onStartOver,
  onRetune,
  onUpdateAnswers,
}: {
  persona: PersonaResponse | null;
  busy: boolean;
  onSave: (draft: PersonaContent) => Promise<void>;
  onContinue: () => void;
  onStartOver: () => void;
  onRetune: () => Promise<void>;
  onUpdateAnswers: () => void;
}) {
  if (!persona) {
    return (
      <div className="animate-fade-up py-20 text-center">
        <h1 className="mb-3 font-display text-3xl text-white">No persona yet</h1>
        <p className="mb-8 text-zinc-400">Answer a few questions to get started.</p>
        <button
          onClick={onStartOver}
          className="rounded-full bg-white px-6 py-3 font-medium text-black hover:bg-zinc-200"
        >
          Start
        </button>
      </div>
    );
  }

  if (persona.persona_status === "generating") {
    const q = persona.questionnaire as unknown as Record<string, unknown> | null;
    const fromChat = !!(q?.interview_turns);
    return (
      <GeneratingState
        title={fromChat ? "Reading everything you shared…" : "Crafting your persona…"}
        subtitle="Turning your answers into a voice and themes for your videos. Usually 15-30 seconds."
        lines={4}
        startedAt={persona.generation_started_at ?? null}
      />
    );
  }

  if (persona.persona_status === "failed" && !persona.persona) {
    return (
      <div className="animate-fade-up py-16">
        <h1 className="mb-3 font-display text-3xl text-white">
          Generation didn&apos;t finish
        </h1>
        <p className="mb-2 text-zinc-400">
          {persona.error_detail ?? "The persona generator hit an error."}
        </p>
        <p className="mb-4 text-zinc-400">
          Your answers are saved.{" "}
          <button
            onClick={onStartOver}
            className="text-amber-300 underline transition-colors hover:text-amber-200"
          >
            Edit your answers and try again
          </button>
          , or write the persona by hand below — either unblocks the rest of the flow.
        </p>
        <PersonaEditor
          persona={blankPersona()}
          status="failed"
          onSave={onSave}
          onContinue={onContinue}
          continueLabel="Plan my 30 days →"
          continuing={busy}
          startInEdit
        />
      </div>
    );
  }

  const personaData = persona.persona;
  return (
    <PersonaEditor
      persona={personaData ?? blankPersona()}
      status={persona.persona_status}
      onSave={onSave}
      onContinue={onContinue}
      continueLabel="Plan my 30 days →"
      continuing={busy}
      onRetuneFromFeedback={onRetune}
      tiktokProfile={persona.tiktok_profile}
      signatureQuote={personaData?.signature_quote}
      onUpdateAnswers={onUpdateAnswers}
    />
  );
}

// ── Plan step ─────────────────────────────────────────────────────────────
function PlanStepView({
  plan,
  busy,
  onCreatePlan,
  onError,
  onRefresh,
  onReviewPersona,
}: {
  plan: ContentPlan | null;
  busy: boolean;
  onCreatePlan: (events: string) => void;
  onError: (msg: string) => void;
  onRefresh: () => void;
  onReviewPersona: () => void;
}) {
  const [events, setEvents] = useState("");

  if (plan === null || plan.plan_status === "failed") {
    return (
      <div className="animate-fade-up py-2">
        <h1 className="mb-2 font-display text-3xl text-white">Plan your next 30 days</h1>
        <p className="mb-6 text-zinc-400">
          Anything coming up we should lean into? Trips, launches, exams, events — optional,
          but it makes the plan feel like yours.
        </p>
        {plan?.plan_status === "failed" && (
          <div className="mb-6 rounded border border-amber-700 bg-amber-950/40 px-4 py-3 text-amber-200">
            Last generation didn&apos;t finish. Try again.
          </div>
        )}
        <label className="block">
          <span className="sr-only">Upcoming events to weave into your plan (optional)</span>
          <textarea
            value={events}
            onChange={(e) => setEvents(e.target.value)}
            rows={4}
            placeholder="e.g. moving apartments in week 2, gym comp at the end of the month"
            className="w-full resize-y rounded-lg border border-zinc-700 bg-zinc-900 px-4 py-3 text-white placeholder-zinc-600 transition-colors focus:border-amber-400/60 focus:outline-none"
          />
        </label>
        <div className="mt-4 flex items-center gap-4">
          <button
            onClick={() => onCreatePlan(events)}
            disabled={busy}
            className="rounded-full bg-amber-400 px-6 py-3 font-medium text-black transition-colors hover:bg-amber-300 disabled:bg-zinc-700 disabled:text-zinc-400"
          >
            {busy ? "Starting…" : "Generate my 30-day plan"}
          </button>
          <button
            onClick={onReviewPersona}
            className="text-sm text-zinc-400 underline transition-colors hover:text-white"
          >
            Review persona first
          </button>
        </div>
      </div>
    );
  }

  if (plan.plan_status === "generating") {
    return (
      <GeneratingState
        title="Building your 30-day plan…"
        subtitle="Scripting a month of video ideas around your persona. This usually takes up to a minute."
        lines={6}
        startedAt={plan.generation_started_at ?? null}
      />
    );
  }

  return (
    <>
      <SeedUploadCard plan={plan} onError={onError} onRefresh={onRefresh} />
      <PlanCalendar plan={plan} onError={onError} onRefresh={onRefresh} />
    </>
  );
}

function blankPersona(): PersonaContent {
  return {
    summary: "",
    content_pillars: [],
    tone: "",
    audience: "",
    posting_cadence: "",
    posts_per_week: null,
    sample_topics: [],
  };
}
