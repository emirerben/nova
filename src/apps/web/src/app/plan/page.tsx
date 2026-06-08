"use client";

import { useSession } from "next-auth/react";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState, Suspense } from "react";
import {
  type ContentPlan,
  createContentPlan,
  createPersona,
  getContentPlan,
  getPersona,
  getStyle,
  NotAuthenticatedError,
  type PersonaContent,
  type PersonaQuestionnaire,
  type PersonaResponse,
  type StyleResponse,
  retunePersonaFromFeedback,
  tiktokScrape,
  updatePersona,
} from "@/lib/plan-api";
import { resolvePlanMode } from "./_lib/route";
import ChatInterview from "./_components/ChatInterview";
import { GeneratingStateLight } from "./_components/GeneratingStateLight";
import OnboardingStep from "./_components/OnboardingStep";
import PersonaEditor from "./_components/PersonaEditor";
import { LightShell } from "./_components/ui/LightShell";
import SignInPrompt from "./_components/SignInPrompt";
import TikTokPreScreen from "./_components/TikTokPreScreen";
import { WorkspaceHome } from "./_components/workspace/WorkspaceHome";

// Sub-steps within the "you" wizard step.
type YouSubStep = "tiktok-pre-screen" | "chat" | "form";

const POLL_MS = 2000;

export default function PlanPage() {
  return (
    <Suspense>
      <PlanPageInner />
    </Suspense>
  );
}

function PlanPageInner() {
  const { status: authStatus } = useSession();
  const router = useRouter();
  const searchParams = useSearchParams();

  // callbackUrl audit (PR3): all existing callbackUrls point to "/" or "/plan" —
  // both are correct. "/plan" is the mode router; "/plan?step=*" falls through
  // the shim below. No callbackUrl changes needed.

  // Legacy ?step= shim — redirect old deeplinks to their canonical routes.
  const stepParam = searchParams.get("step");
  useEffect(() => {
    if (stepParam === "persona") {
      router.replace("/plan/persona");
      return;
    }
    if (stepParam === "you") {
      router.replace("/plan/setup");
      return;
    }
    if (stepParam === "plan") {
      router.replace("/plan");
      return;
    }
  }, [stepParam, router]);

  const [persona, setPersona] = useState<PersonaResponse | null>(null);
  const [plan, setPlan] = useState<ContentPlan | null>(null);
  const [styleResponse, setStyleResponse] = useState<StyleResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [subStep, setSubStep] = useState<YouSubStep>("tiktok-pre-screen");
  const [busy, setBusy] = useState(false);

  // Track when the plan flips from generating → ready in-session (for banner).
  const [planJustReady, setPlanJustReady] = useState(false);

  const load = useCallback(async () => {
    try {
      // Fetch style best-effort: API returns 404 when USER_STYLE_ENABLED=false.
      // Map any 404 (or NotAuthenticatedError rethrow) → absent so the page
      // never crashes when the feature flag is off.
      const stylePromise = getStyle().catch((err) => {
        const msg: string = err instanceof Error ? err.message : String(err);
        if (msg.includes("404") || msg.includes("Not Found")) {
          return { style: null, status: "absent" as const };
        }
        // For any other error (e.g. network), treat as absent — non-critical.
        return { style: null, status: "absent" as const };
      });

      const [p, pl, sr] = await Promise.all([getPersona(), getContentPlan(), stylePromise]);
      setPersona(p);
      setStyleResponse(sr);
      setPlan((prevPlan) => {
        // Detect in-session generating → ready flip
        const prevStatus = prevPlan?.plan_status ?? null;
        const newStatus = pl?.plan_status ?? null;
        if (prevStatus === "generating" && newStatus === "ready") {
          setPlanJustReady(true);
        }
        return pl;
      });
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
    persona?.persona_status === "generating" ||
    plan?.plan_status === "generating" ||
    (plan?.items ?? []).some((i) => i.status === "rerolling") ||
    styleResponse?.status === "deriving";

  useEffect(() => {
    if (!isGenerating) return;
    const id = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(id);
  }, [isGenerating, load]);

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
  }

  async function handleOnboardingSubmit(answers: PersonaQuestionnaire) {
    setBusy(true);
    setError(null);
    try {
      const p = await createPersona(answers);
      setPersona(p);
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
      <LightShell>
        <SignInPrompt callbackUrl="/plan" />
      </LightShell>
    );
  }

  if (loading) {
    return (
      <LightShell>
        <p className="py-24 text-center text-[#71717a]">Loading…</p>
      </LightShell>
    );
  }

  const mode = resolvePlanMode(persona, plan);

  // ── Workspace modes ──────────────────────────────────────────────────────
  if (
    mode === "workspace" ||
    mode === "workspace:regenerating" ||
    mode === "workspace:refresh-failed"
  ) {
    return (
      <WorkspaceHome
        plan={plan!}
        persona={persona!}
        planJustReady={planJustReady}
        regenerating={mode === "workspace:regenerating"}
        onRefresh={load}
        onError={setError}
        onBannerDismiss={() => setPlanJustReady(false)}
        styleResponse={styleResponse}
      />
    );
  }

  // ── Setup modes ──────────────────────────────────────────────────────────
  return (
    <LightShell>
      {error && (
        <div className="mb-6 rounded border border-zinc-200 bg-[#fafaf8] px-4 py-3 text-[#3f3f46]">
          {error}
        </div>
      )}

      {mode === "setup:prescreen" && subStep === "tiktok-pre-screen" && (
        <TikTokPreScreen onContinue={handleTikTokPreScreen} submitting={busy} />
      )}

      {(mode === "setup:chat" || (mode === "setup:prescreen" && subStep === "chat")) && (
        <ChatInterview onComplete={handleChatComplete} />
      )}

      {mode === "setup:prescreen" && subStep === "form" && (
        <OnboardingStep
          onSubmit={handleOnboardingSubmit}
          submitting={busy}
          initialAnswers={persona?.questionnaire ?? null}
        />
      )}

      {mode === "setup:persona-generating" && (
        <GeneratingStateLight label="Setting up your persona" />
      )}

      {mode === "setup:persona-failed" && (
        <PersonaFailedView
          persona={persona}
          busy={busy}
          onSave={handleSavePersona}
          onStartOver={() => setSubStep("tiktok-pre-screen")}
          onContinue={() => void handleCreatePlan("")}
        />
      )}

      {mode === "setup:plan-intro" && (
        <PersonaReadyView
          persona={persona}
          busy={busy}
          onSave={handleSavePersona}
          onContinue={() => void handleCreatePlan("")}
          onRetune={handleRetunePersona}
          onUpdateAnswers={() => setSubStep("tiktok-pre-screen")}
        />
      )}

      {mode === "setup:plan-generating" && (
        <GeneratingStateLight
          horizonDays={plan?.horizon_days}
          label={`Building your ${plan?.horizon_days ?? 30} days`}
        />
      )}

      {mode === "setup:plan-failed" && (
        <PlanIntroView
          plan={null}
          busy={busy}
          onCreatePlan={handleCreatePlan}
          isFailed
        />
      )}
    </LightShell>
  );
}

// ── Persona failed view ──────────────────────────────────────────────────────
function PersonaFailedView({
  persona,
  busy,
  onSave,
  onStartOver,
  onContinue,
}: {
  persona: PersonaResponse | null;
  busy: boolean;
  onSave: (draft: PersonaContent) => Promise<void>;
  onStartOver: () => void;
  onContinue: () => void;
}) {
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

  return (
    <div className="animate-fade-up py-16">
      <h1 className="mb-3 font-display text-3xl text-[#0c0c0e]">
        Generation didn&apos;t finish
      </h1>
      <p className="mb-2 text-[#71717a]">
        {persona?.error_detail ?? "The persona generator hit an error."}
      </p>
      <p className="mb-4 text-[#71717a]">
        Your answers are saved.{" "}
        <button
          onClick={onStartOver}
          className="text-lime-700 underline transition-colors hover:text-lime-600"
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

// ── Persona ready view (plan intro) ──────────────────────────────────────────
function PersonaReadyView({
  persona,
  busy,
  onSave,
  onContinue,
  onRetune,
  onUpdateAnswers,
}: {
  persona: PersonaResponse | null;
  busy: boolean;
  onSave: (draft: PersonaContent) => Promise<void>;
  onContinue: () => void;
  onRetune: () => Promise<void>;
  onUpdateAnswers: () => void;
}) {
  if (!persona) {
    return (
      <div className="animate-fade-up py-20 text-center">
        <h1 className="mb-3 font-display text-3xl text-[#0c0c0e]">No persona yet</h1>
        <p className="mb-8 text-[#71717a]">Answer a few questions to get started.</p>
        <button
          onClick={onUpdateAnswers}
          className="inline-flex items-center justify-center rounded-full bg-[#0c0c0e] px-9 py-[15px] text-[15px] font-semibold text-white transition-opacity hover:opacity-80"
        >
          Start
        </button>
      </div>
    );
  }

  const personaData = persona.persona;
  if (!personaData) return null;

  return (
    <PersonaEditor
      persona={personaData}
      status={persona.persona_status}
      onSave={onSave}
      onContinue={onContinue}
      continueLabel="Plan my 30 days →"
      continuing={busy}
      onRetuneFromFeedback={onRetune}
      tiktokProfile={persona.tiktok_profile}
      onUpdateAnswers={onUpdateAnswers}
    />
  );
}

// ── Plan intro view (create plan) ─────────────────────────────────────────────
function PlanIntroView({
  plan,
  busy,
  onCreatePlan,
  isFailed = false,
}: {
  plan: null;
  busy: boolean;
  onCreatePlan: (events: string) => void;
  isFailed?: boolean;
}) {
  const [events, setEvents] = useState("");

  return (
    <div className="animate-fade-up py-2">
      <h1 className="mb-2 font-display text-3xl text-[#0c0c0e]">Plan your next 30 days</h1>
      <p className="mb-6 text-[#71717a]">
        Anything coming up we should lean into? Trips, launches, exams, events — optional, but it
        makes the plan feel like yours.
      </p>
      {isFailed && (
        <div className="mb-6 rounded border border-zinc-200 bg-[#fafaf8] px-4 py-3 text-[#3f3f46]">
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
          className="w-full resize-y rounded-lg border border-zinc-200 bg-white px-4 py-3 text-[#0c0c0e] placeholder-zinc-400 transition-colors focus:border-lime-600/60 focus:outline-none"
        />
      </label>
      <div className="mt-4 flex items-center gap-4">
        <button
          onClick={() => onCreatePlan(events)}
          disabled={busy}
          className="inline-flex items-center justify-center rounded-full bg-[#0c0c0e] px-9 py-[15px] text-[15px] font-semibold text-white transition-opacity hover:opacity-80 disabled:opacity-40"
        >
          {busy ? "Starting…" : "Generate my 30-day plan"}
        </button>
      </div>
    </div>
  );
}
