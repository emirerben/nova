"use client";

import { useSession } from "next-auth/react";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState, Suspense } from "react";
import {
  type ContentPlan,
  createContentPlan,
  getContentPlan,
  getPersona,
  getStyle,
  NotAuthenticatedError,
  type PersonaContent,
  type PersonaResponse,
  type StyleResponse,
  retunePersonaFromFeedback,
  tiktokScrape,
  updatePersona,
} from "@/lib/plan-api";
import { resolvePlanMode } from "./_lib/route";
import { GeneratingStateLight } from "./_components/GeneratingStateLight";
import OnboardingShell from "./_components/OnboardingShell";
import { LightShell } from "./_components/ui/LightShell";
import SignInPrompt from "./_components/SignInPrompt";
import { WorkspaceHome } from "./_components/workspace/WorkspaceHome";

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
  const [styleResponse, setStyleResponse] = useState<StyleResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [error, setError] = useState<string | null>(null);
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

  /** OnboardingShell Step 3 — ChatInterview completed (persona generation fires). */
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

  // ── Plan-generating state (after OnboardingShell Step 4 fires) ──────────────
  if (mode === "setup:plan-generating") {
    return (
      <LightShell>
        <GeneratingStateLight
          horizonDays={plan?.horizon_days}
          label={`Building your ${plan?.horizon_days ?? 30} days`}
        />
      </LightShell>
    );
  }

  // ── Setup modes → OnboardingShell (split-rail) ────────────────────────────
  // All setup modes (prescreen / chat / persona-generating / persona-failed /
  // plan-intro / plan-failed) are handled inside the split-rail shell.
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
