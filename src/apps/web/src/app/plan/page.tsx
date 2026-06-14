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
  recordOnboardingFork,
} from "@/lib/plan-api";
import { createGenerativeJob } from "@/lib/generative-api";
import { resolvePlanMode } from "./_lib/route";
import ChatInterview from "./_components/ChatInterview";
import { GeneratingStateLight } from "./_components/GeneratingStateLight";
import OnboardingStep from "./_components/OnboardingStep";
import PersonaEditor from "./_components/PersonaEditor";
import { LightShell } from "./_components/ui/LightShell";
import SignInPrompt from "./_components/SignInPrompt";
import TikTokPreScreen from "./_components/TikTokPreScreen";
import { WorkspaceHome } from "./_components/workspace/WorkspaceHome";
import { ForkScreen } from "./_components/onboarding/ForkScreen";
import { EditUploadStep } from "./_components/onboarding/EditUploadStep";
import { ClipGroupStep, type ClipItem } from "./_components/onboarding/ClipGroupStep";
import { EditPayoff } from "./_components/onboarding/EditPayoff";

// Sub-steps within the "you" wizard step.
type YouSubStep = "tiktok-pre-screen" | "chat" | "upload-offer" | "uploading" | "form";

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

  // Edits-first onboarding funnel state.
  // uploadedClips: full clip objects (gcsPath + objectUrl) from the upload step.
  const [uploadedClips, setUploadedClips] = useState<ClipItem[]>([]);
  // localStep: client-only override so back button from group → upload works
  // without a server roundtrip. null = let resolvePlanMode drive the UI.
  const [localStep, setLocalStep] = useState<"group" | null>(null);
  // editJobs: one entry per group (or ungrouped clip). Purely local — on refresh
  // the user returns to the upload step, which is acceptable.
  const [editJobs, setEditJobs] = useState<{ jobId: string; topic: string; clipPaths: string[] }[]>([]);
  // onboardingTopic: fallback topic for createGenerativeJob when no per-group topic is set.
  const [onboardingTopic] = useState("");

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
    setBusy(true);
    try {
      if (handle) {
        try {
          const p = await tiktokScrape(handle);
          setPersona(p);
        } catch {
          // scrape failure is non-blocking
        }
      }
    } finally {
      setBusy(false);
    }
    setSubStep("chat");
  }

  function handleChatComplete() {
    // Interview done — offer the footage upload path before advancing to the plan.
    setSubStep("upload-offer");
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
  // Payoff needs the wide shell so the video+editor two-column layout has
  // room; other setup steps self-constrain to max-w-lg inside the shell.
  const isPayoffStep =
    editJobs.length > 0 ||
    mode === "setup:edit-generating" ||
    mode === "setup:edit-payoff";

  return (
    <LightShell size={isPayoffStep ? "wide" : "narrow"}>
      {error && (
        <div className="mb-6 rounded border border-zinc-200 bg-[#fafaf8] px-4 py-3 text-[#3f3f46]">
          {error}
        </div>
      )}

      {/* Step 1 (new user): TikTok handle */}
      {mode === "setup:prescreen" && subStep === "tiktok-pre-screen" && (
        <TikTokPreScreen onContinue={handleTikTokPreScreen} submitting={busy} />
      )}

      {/* Fallback fork screen for returning users with no content_mode */}
      {mode === "setup:fork" && (
        <ForkScreen
          onFootage={async () => {
            try {
              await recordOnboardingFork({ content_mode: "existing_footage" });
            } catch { }
            void load();
          }}
          onFresh={async () => {
            try {
              await recordOnboardingFork({ content_mode: "create_new" });
            } catch { }
            setSubStep("chat");
          }}
          onSkip={async () => {
            try {
              await recordOnboardingFork({ content_mode: "existing_footage" });
            } catch { }
            void load();
          }}
        />
      )}

      {/* After interview: offer footage upload before advancing to the plan */}
      {subStep === "upload-offer" && (
        <ForkScreen
          onFootage={async () => {
            setSubStep("uploading");
            try { await recordOnboardingFork({ content_mode: "existing_footage" }); } catch {}
          }}
          onFresh={() => void load()}
          onSkip={() => void load()}
        />
      )}

      {(mode === "setup:chat" || (mode === "setup:prescreen" && subStep === "chat")) && subStep !== "upload-offer" && (
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

      {/* FOOTAGE PATH: upload → group (with per-group context) → generate */}

      {/* Upload: shown after interview upload-offer ("yes"), or when server says edit-upload */}
      {(mode === "setup:edit-upload" || subStep === "uploading") && localStep !== "group" && editJobs.length === 0 && (
        <EditUploadStep
          onSubmit={(clips) => {
            setUploadedClips(clips);
            setLocalStep("group");
          }}
        />
      )}

      {/* Group: client-only step, back returns to upload */}
      {localStep === "group" && (
        <ClipGroupStep
          clips={uploadedClips}
          onBack={() => setLocalStep(null)}
          onSubmit={async (groups) => {
            setLocalStep(null);
            const jobs: { jobId: string; topic: string; clipPaths: string[] }[] = [];
            for (const group of groups) {
              try {
                const result = await createGenerativeJob(group.clips, null, {
                  topic: group.topic || onboardingTopic || undefined,
                });
                jobs.push({ jobId: result.job_id, topic: group.topic, clipPaths: group.clips });
              } catch {
                // skip failed submissions silently
              }
            }
            if (jobs.length > 0) {
              setEditJobs(jobs);
              // Persist first job id so resolvePlanMode advances past edit-upload.
              const allClipPaths = groups.flatMap((g) => g.clips);
              try {
                await recordOnboardingFork({
                  content_mode: "existing_footage",
                  onboarding_edit_job_id: jobs[0].jobId,
                  onboarding_clip_paths: allClipPaths,
                });
              } catch {
                // best-effort
              }
              void load();
            }
          }}
        />
      )}

      {/* Step 3 — multiple job payoffs, one section per group/clip */}
      {editJobs.length > 0 && localStep !== "group" && (
        <div className="flex flex-col gap-16 pb-4">
          {editJobs.map((job) => (
            <div key={job.jobId}>
              {job.topic && (
                <p className="px-4 mb-2 text-xs text-[#71717a] font-medium uppercase tracking-wide">
                  {job.topic}
                </p>
              )}
              <EditPayoff
                jobId={job.jobId}
                hidePlanCta={true}
                onMakePlan={() => {}}
                onReRoll={async () => {
                  if (job.clipPaths.length === 0) return;
                  try {
                    const result = await createGenerativeJob(job.clipPaths, null, {
                      topic: job.topic || undefined,
                    });
                    setEditJobs((prev) =>
                      prev.map((j) =>
                        j.jobId === job.jobId
                          ? { ...j, jobId: result.job_id }
                          : j,
                      ),
                    );
                  } catch (err) {
                    setError(err instanceof Error ? err.message : "Couldn't re-roll");
                  }
                }}
              />
            </div>
          ))}
          <div className="px-4 max-w-2xl mx-auto w-full">
            <div className="border-t border-[#e4e4e7] pt-6">
              <button
                onClick={async () => {
                  try {
                    await recordOnboardingFork({
                      content_mode: "existing_footage",
                      onboarding_payoff_done: true,
                    });
                  } catch {
                    // best-effort
                  }
                  setEditJobs([]);
                  void load();
                }}
                className="w-full rounded-xl bg-[#0c0c0e] text-white py-3 font-medium hover:opacity-80 focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 min-h-[44px]"
              >
                Continue creating with Nova →
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Fallback single-job payoff — when resuming from server state (e.g. after refresh) */}
      {editJobs.length === 0 && localStep !== "group" &&
        (mode === "setup:edit-generating" || mode === "setup:edit-payoff") && (
        <div className="flex flex-col gap-0 pb-4">
          <EditPayoff
            jobId={persona?.questionnaire?.onboarding_edit_job_id ?? ""}
            hidePlanCta={true}
            onMakePlan={() => {}}
            onReRoll={async () => {
              const clips = persona?.questionnaire?.onboarding_clip_paths ?? [];
              if (clips.length > 0) {
                try {
                  const result = await createGenerativeJob(clips, null);
                  await recordOnboardingFork({
                    content_mode: "existing_footage",
                    onboarding_edit_job_id: result.job_id,
                  });
                  void load();
                } catch (err) {
                  setError(err instanceof Error ? err.message : "Couldn't re-roll");
                }
              }
            }}
          />
          <div className="px-4 max-w-2xl mx-auto w-full">
            <div className="border-t border-[#e4e4e7] pt-6 pb-8">
              <button
                onClick={async () => {
                  try {
                    await recordOnboardingFork({
                      content_mode: "existing_footage",
                      onboarding_payoff_done: true,
                    });
                  } catch {
                    // best-effort
                  }
                  void load();
                }}
                className="w-full rounded-xl bg-[#0c0c0e] text-white py-3 font-medium hover:opacity-80 focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 min-h-[44px]"
              >
                Continue creating with Nova →
              </button>
            </div>
          </div>
        </div>
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
