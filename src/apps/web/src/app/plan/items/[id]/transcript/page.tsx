"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { AudioTake } from "@/hooks/useAudioRecorder";
import {
  StepRail,
  type StepRailState,
  type StepRailStep,
} from "../../../_components/ui/StepRail";
import {
  getPlanItem,
  getPlanItemJobStatus,
  NotAuthenticatedError,
  type PlanItem,
} from "@/lib/plan-api";
import BriefStep, { type BriefResult } from "./BriefStep";
import QuestionsStep from "./QuestionsStep";
import ScriptStep, { type ScriptState } from "./ScriptStep";
import TeleprompterRecorder from "./TeleprompterRecorder";
import ReviewStep from "./ReviewStep";

// ── Steps ─────────────────────────────────────────────────────────────────────

type Step = 0 | 1 | 2 | 3 | 4;
const STEP_LABELS = ["Brief", "Questions", "Script", "Record", "Review"] as const;

/** Narrated formats that get the transcript helper (mirror of the item page gate). */
function isNarratedFormat(fmt: string | null | undefined): boolean {
  return fmt === "narrated" || fmt === "narrated_planned" || fmt === "narrated_ready";
}

/** Rough read-time fallback when footage analysis can't give a duration. */
function guideDurationS(item: PlanItem | null): number {
  const total = (item?.filming_guide ?? []).reduce(
    (sum, s) => sum + (Number.isFinite(s.duration_s) ? s.duration_s : 0),
    0,
  );
  return total > 0 ? total : 30;
}

// ── Step slide transition (mirrors OnboardingShell.StepSlide, t-page tokens) ──

function StepSlide({ children }: { children: React.ReactNode }) {
  const [entered, setEntered] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(raf);
  }, []);
  return <div className={`step-slide${entered ? " is-entered" : ""}`}>{children}</div>;
}

// ── Step rail ─────────────────────────────────────────────────────────────────

/** Map record-flow state onto the shared rail's step model. */
function transcriptSteps(current: Step): StepRailStep[] {
  return STEP_LABELS.map((label, i) => {
    const n = i as Step;
    const isDone = n < current;

    let state: StepRailState;
    if (isDone) state = "done";
    else if (n === current) state = "active";
    else state = "upcoming";

    return {
      key: n,
      label,
      state,
      clickable: isDone,
      note: isDone ? ({ text: "✓", tone: "lime" } as const) : undefined,
    };
  });
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function TranscriptTakeoverPage() {
  const params = useParams<{ id: string }>();
  const itemId = params.id;
  const router = useRouter();

  const [item, setItem] = useState<PlanItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);

  const [step, setStep] = useState<Step>(0);
  const [brief, setBrief] = useState<BriefResult | null>(null);
  const [answers, setAnswers] = useState<string[]>([]);
  const [script, setScript] = useState<ScriptState | null>(null);
  const [take, setTake] = useState<AudioTake | null>(null);
  const [recordedVersion, setRecordedVersion] = useState<number | null>(null);

  // Muted footage reference: the latest rendered variant output (already signed).
  const [footageSrc, setFootageSrc] = useState<string | null>(null);
  const [footageIdentity, setFootageIdentity] = useState<string | null>(null);

  // Load the item; resolve a muted footage reference if a render exists.
  useEffect(() => {
    let alive = true;
    getPlanItem(itemId)
      .then(async (it) => {
        if (!alive) return;
        setItem(it);
        if (it.current_job_id) {
          try {
            const status = await getPlanItemJobStatus(it.current_job_id);
            const ready = status.variants.find(
              (v) => v.render_status === "ready" && v.output_url,
            );
            if (alive && ready?.output_url) {
              setFootageSrc(ready.output_url);
              setFootageIdentity(ready.variant_id);
            }
          } catch {
            // No footage reference — the recorder degrades to transcript-only.
          }
        }
      })
      .catch((e: unknown) => {
        if (!alive) return;
        if (e instanceof NotAuthenticatedError) {
          window.location.href = `/api/auth/signin?callbackUrl=/plan/items/${itemId}/transcript`;
          return;
        }
        setNotFound(true);
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [itemId]);

  const hasClips = (item?.clip_gcs_paths.length ?? 0) > 0;
  const fallbackDurationS = useMemo(() => guideDurationS(item), [item]);

  const goBack = useCallback((target: Step) => setStep(target), []);

  const backToItem = `/plan/items/${itemId}`;

  // ── Render ──────────────────────────────────────────────────────────────────

  let body: React.ReactNode;
  if (loading) {
    body = (
      <div className="flex items-center gap-2 py-10">
        <span className="h-1.5 w-1.5 motion-safe:animate-ping rounded-full bg-lime-600" />
        <span className="text-sm text-[#71717a]">Loading…</span>
      </div>
    );
  } else if (notFound || !item) {
    body = (
      <div className="max-w-xl">
        <h1 className="font-display text-3xl leading-snug text-[#0c0c0e]">
          We couldn&apos;t open this item.
        </h1>
        <div className="mt-6">
          <Link
            href="/plan"
            className="text-sm text-[#71717a] underline underline-offset-4 hover:text-[#0c0c0e]"
          >
            Back to your plan
          </Link>
        </div>
      </div>
    );
  } else if (!isNarratedFormat(item.edit_format)) {
    body = (
      <div className="max-w-xl">
        <p className="mb-3 text-xs font-medium uppercase tracking-wide text-lime-700">
          Get a transcript
        </p>
        <h1 className="font-display text-3xl leading-snug text-[#0c0c0e]">
          This item isn&apos;t set up for a voiceover yet.
        </h1>
        <p className="mt-4 text-[#71717a]">
          Switch it to Voiceover on the item page, then come back to write your script.
        </p>
        <div className="mt-8">
          <Link
            href={backToItem}
            className="inline-flex min-h-[44px] items-center rounded-full bg-[#0c0c0e] px-9 py-[15px] text-[15px] font-semibold text-white transition-opacity hover:opacity-80"
          >
            Back to item →
          </Link>
        </div>
      </div>
    );
  } else {
    body = (
      <StepSlide key={step}>
        {step === 0 && (
          <BriefStep
            itemId={itemId}
            hasClips={hasClips}
            fallbackDurationS={fallbackDurationS}
            onDone={(result) => {
              setBrief(result);
              setStep(1);
            }}
          />
        )}

        {step === 1 && brief && (
          <QuestionsStep
            itemId={itemId}
            brief={brief.brief}
            footageSummary={brief.footageSummary}
            onDone={(a) => {
              setAnswers(a);
              setStep(2);
            }}
          />
        )}

        {step === 2 && brief && (
          <ScriptStep
            itemId={itemId}
            brief={brief.brief}
            footageSummary={brief.footageSummary}
            answers={answers}
            durationS={brief.durationS}
            initialScript={script}
            recordedAgainstVersion={recordedVersion}
            onScript={setScript}
            onRecord={() => setStep(3)}
          />
        )}

        {/* dvh, not vh: on mobile Safari/Chrome `100vh` ignores the browser
            toolbar, which pushed the 44px record targets off-screen. Matches
            the editor's dvh convention (EditorShell, CopilotDrawer). */}
        {step === 3 && script && (
          <div className="h-[calc(100dvh-8rem)]">
            <TeleprompterRecorder
              itemId={itemId}
              script={script}
              footageSrc={footageSrc}
              footageIdentity={footageIdentity}
              onRecorded={(t) => {
                setTake(t);
                setRecordedVersion(script.version);
                setStep(4);
              }}
              onError={setPageError}
            />
          </div>
        )}

        {step === 4 && take && brief && (
          <ReviewStep
            take={take}
            estimateS={script?.readTimeS ?? brief.durationS}
            onUse={() => router.push(backToItem)}
            onRetake={() => setStep(3)}
          />
        )}
      </StepSlide>
    );
  }

  return (
    <div className="flex min-h-screen flex-col bg-[#fafaf8] md:flex-row">
      <StepRail
        steps={transcriptSteps(step)}
        onGoBack={(key) => goBack(key as Step)}
      />

      {/* min-w-0 lets this flex child shrink instead of overflowing on phones. */}
      <main className="flex min-w-0 flex-1 flex-col px-5 py-8 md:px-12 md:py-10">
        <div className="mb-8">
          <Link
            href={backToItem}
            className="text-sm text-[#71717a] underline underline-offset-4 hover:text-[#0c0c0e]"
          >
            ← Back to item
          </Link>
        </div>

        {pageError && (
          <div className="mb-6 max-w-2xl rounded border border-zinc-200 bg-white px-4 py-3 text-sm text-[#3f3f46]">
            {pageError}
          </div>
        )}

        <div className="flex-1">{body}</div>
      </main>
    </div>
  );
}
