"use client";

import { useSession } from "next-auth/react";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState, Suspense } from "react";
import {
  type ContentPlan,
  createContentPlan,
  getContentPlan,
  getPersona,
  NotAuthenticatedError,
  type PersonaContent,
  type PersonaResponse,
  retunePersonaFromFeedback,
  tiktokScrape,
  updatePersona,
  recordOnboardingFork,
} from "@/lib/plan-api";
import { createGenerativeJob } from "@/lib/generative-api";
import { resolvePlanMode } from "./_lib/route";
import { GeneratingStateLight } from "./_components/GeneratingStateLight";
import OnboardingShell from "./_components/OnboardingShell";
import { LightShell } from "./_components/ui/LightShell";
import SignInPrompt from "./_components/SignInPrompt";
import { WorkspaceHome } from "./_components/workspace/WorkspaceHome";
import { ForkScreen } from "./_components/onboarding/ForkScreen";
import { EditUploadStep } from "./_components/onboarding/EditUploadStep";
import { ClipGroupStep, type ClipItem } from "./_components/onboarding/ClipGroupStep";
import { EditPayoff } from "./_components/onboarding/EditPayoff";

const POLL_MS = 2000;
const POLL_REQUEST_TIMEOUT_MS = 10000;

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

  // Legacy ?step= shim — redirect old deeplinks to the unified /plan route.
  // /plan/setup and /plan/onboarding are also deleted; /plan handles everything.
  const stepParam = searchParams.get("step");
  useEffect(() => {
    if (stepParam === "persona" || stepParam === "you" || stepParam === "plan") {
      router.replace("/plan");
    }
  }, [stepParam, router]);

  const [persona, setPersona] = useState<PersonaResponse | null>(null);
  const [plan, setPlan] = useState<ContentPlan | null>(null);
  const [loading, setLoading] = useState(true);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const loadEpochRef = useRef(0);
  const pollInFlightRef = useRef(false);
  const pollRequestIdRef = useRef(0);
  const pollTimeoutRef = useRef<number | null>(null);

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
  // subStep: null = normal OnboardingShell flow; "upload-offer" = show footage fork
  // after ChatInterview; "uploading" = user chose footage, upload step is active.
  const [subStep, setSubStep] = useState<"upload-offer" | "uploading" | null>(null);

  const load = useCallback(async () => {
    const epoch = ++loadEpochRef.current;
    try {
      const [p, pl] = await Promise.all([getPersona(), getContentPlan()]);
      if (epoch !== loadEpochRef.current) return null;
      setPersona(p);
      setPlan(pl);
      return { p, pl };
    } catch (err) {
      if (epoch !== loadEpochRef.current) return null;
      if (err instanceof NotAuthenticatedError) setNeedsAuth(true);
      else setError(err instanceof Error ? err.message : "Failed to load your plan");
      return null;
    } finally {
      if (epoch === loadEpochRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => () => {
    loadEpochRef.current += 1;
    pollRequestIdRef.current += 1;
    if (pollTimeoutRef.current !== null) {
      window.clearTimeout(pollTimeoutRef.current);
      pollTimeoutRef.current = null;
    }
  }, []);

  const handlePlanChange = useCallback((updatedPlan: ContentPlan) => {
    loadEpochRef.current += 1;
    // The accepted POST is newer than any in-flight GET. Release the polling
    // gate too: a hung older request must not prevent this generation attempt
    // from being polled.
    pollRequestIdRef.current += 1;
    pollInFlightRef.current = false;
    if (pollTimeoutRef.current !== null) {
      window.clearTimeout(pollTimeoutRef.current);
      pollTimeoutRef.current = null;
    }
    setPlan(updatedPlan);
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
    (plan?.items ?? []).some((i) => i.status === "rerolling");

  useEffect(() => {
    if (!isGenerating) return;
    const id = setInterval(() => {
      if (pollInFlightRef.current) return;
      pollInFlightRef.current = true;
      const requestId = ++pollRequestIdRef.current;
      // A fetch can remain pending after a network interruption. Bound only
      // the single-flight gate (not the request itself); the next poll advances
      // the epoch so any eventual late response is ignored.
      pollTimeoutRef.current = window.setTimeout(() => {
        if (requestId === pollRequestIdRef.current) {
          pollInFlightRef.current = false;
          pollTimeoutRef.current = null;
        }
      }, POLL_REQUEST_TIMEOUT_MS);
      void load().finally(() => {
        if (requestId !== pollRequestIdRef.current) return;
        pollInFlightRef.current = false;
        if (pollTimeoutRef.current !== null) {
          window.clearTimeout(pollTimeoutRef.current);
          pollTimeoutRef.current = null;
        }
      });
    }, POLL_MS);
    return () => {
      clearInterval(id);
      pollRequestIdRef.current += 1;
      pollInFlightRef.current = false;
      if (pollTimeoutRef.current !== null) {
        window.clearTimeout(pollTimeoutRef.current);
        pollTimeoutRef.current = null;
      }
    };
  }, [isGenerating, load]);

  // ── Handlers ────────────────────────────────────────────────────────────

  /** OnboardingShell Step 1 — TikTok handle submit (may be empty = skip). */
  async function handleTikTokContinue(handle: string) {
    if (handle) {
      setBusy(true);
      try {
        const p = await tiktokScrape(handle);
        setPersona(p);
      } catch {
        // Best-effort — scrape failure is non-blocking; proceed regardless.
      } finally {
        setBusy(false);
      }
    }
  }

  /** OnboardingShell Step 3 — ChatInterview completed (persona generation fires).
   *  Reload so the polling loop surfaces generating → ready → PersonaEditor reveal. */
  function handleChatComplete() {
    void load();
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

  async function handleCreatePlan() {
    setBusy(true);
    setError(null);
    try {
      const p = await createContentPlan("");
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
      <WorkspaceHomeSkeleton />
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
        onRefresh={load}
        onPlanChange={handlePlanChange}
        onError={setError}
      />
    );
  }

  // ── Edit funnel: post-interview upload offer ─────────────────────────────
  // After ChatInterview completes, we show ForkScreen as an upload offer.
  if (subStep === "upload-offer") {
    return (
      <LightShell>
        <ForkScreen
          onFootage={async () => {
            setSubStep("uploading");
            try {
              await recordOnboardingFork({ content_mode: "existing_footage" });
            } catch {}
          }}
          onFresh={() => {
            setSubStep(null);
            void load();
          }}
          onSkip={() => {
            setSubStep(null);
            void load();
          }}
        />
      </LightShell>
    );
  }

  // ── Edit funnel: footage path (upload / group / generate / payoff) ───────
  const inEditFunnel =
    subStep === "uploading" ||
    mode === "setup:edit-upload" ||
    mode === "setup:edit-generating" ||
    mode === "setup:edit-payoff" ||
    mode === "setup:fork" ||
    editJobs.length > 0;

  if (inEditFunnel) {
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

        {/* Fallback fork screen for returning users with no content_mode */}
        {mode === "setup:fork" && (
          <ForkScreen
            onFootage={async () => {
              try {
                await recordOnboardingFork({ content_mode: "existing_footage" });
              } catch {}
              void load();
            }}
            onFresh={async () => {
              try {
                await recordOnboardingFork({ content_mode: "create_new" });
              } catch {}
              void load();
            }}
            onSkip={async () => {
              try {
                await recordOnboardingFork({ content_mode: "existing_footage" });
              } catch {}
              void load();
            }}
          />
        )}

        {/* Upload: shown after upload-offer ("yes"), or when server says edit-upload */}
        {(mode === "setup:edit-upload" || subStep === "uploading") &&
          localStep !== "group" &&
          editJobs.length === 0 && (
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
                const allClipPaths = groups.flatMap((g) => g.clips);
                try {
                  await recordOnboardingFork({
                    content_mode: "existing_footage",
                    onboarding_edit_job_id: jobs[0].jobId,
                    onboarding_clip_paths: allClipPaths,
                  });
                } catch {}
                void load();
              }
            }}
          />
        )}

        {/* Multiple job payoffs — one section per group/clip */}
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
                          j.jobId === job.jobId ? { ...j, jobId: result.job_id } : j,
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
                    } catch {}
                    setEditJobs([]);
                    void load();
                  }}
                  className="w-full rounded-xl bg-[#0c0c0e] text-white py-3 font-medium hover:opacity-80 focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 min-h-[44px]"
                >
                  Continue creating with Kria →
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Fallback single-job payoff — when resuming from server state (e.g. after refresh) */}
        {editJobs.length === 0 &&
          localStep !== "group" &&
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
                      } catch {}
                      void load();
                    }}
                    className="w-full rounded-xl bg-[#0c0c0e] text-white py-3 font-medium hover:opacity-80 focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 min-h-[44px]"
                  >
                    Continue creating with Kria →
                  </button>
                </div>
              </div>
            </div>
          )}
      </LightShell>
    );
  }

  // ── Plan-generating state (after OnboardingShell Step 4 fires) ──────────────
  if (mode === "setup:plan-generating") {
    return (
      <LightShell>
        <GeneratingStateLight
          horizonDays={plan?.horizon_days}
          label="Building your ideas"
        />
      </LightShell>
    );
  }

  // ── Setup modes → OnboardingShell (split-rail) ────────────────────────────
  // All setup modes (prescreen / chat / persona-generating / persona-failed /
  // plan-intro / plan-failed) are handled inside the split-rail shell.
  // onChatComplete is intercepted to show the footage upload offer.
  return (
    <OnboardingShell
      onTikTokContinue={handleTikTokContinue}
      tiktokBusy={busy}
      persona={persona}
      planBusy={busy}
      onSavePersona={handleSavePersona}
      onChatComplete={handleChatComplete}
      onContinueToPlan={() => void handleCreatePlan()}
      onRetune={handleRetunePersona}
      error={error}
    />
  );
}

function WorkspaceHomeSkeleton() {
  return (
    <div className="min-h-screen bg-[#fafaf8]">
      <div className="mx-auto max-w-[760px] px-6 pb-24 pt-14">
        <div className="flex items-baseline justify-between gap-6">
          <div className="h-11 w-32 rounded bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer" />
          <div className="hidden h-4 w-48 rounded bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer sm:block" />
        </div>
        <div className="mt-4 h-4 w-56 rounded bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer" />
        <div className="mt-10 flex flex-col gap-0">
          {[0, 1, 2, 3].map((row) => (
            <div
              key={row}
              className="grid min-h-[48px] grid-cols-[2rem_1fr_auto] items-start gap-3 border-t border-zinc-100 py-2.5"
            >
              <div className="h-5 w-4 rounded bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer" />
              <div className="h-4 w-full max-w-md rounded bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer" />
              <div className="h-5 w-20 rounded-full bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer" />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
