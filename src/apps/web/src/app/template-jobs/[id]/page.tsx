"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  getTemplatePlaybackUrl,
  rerollTemplateJob,
  type AssemblyPlanData,
  type PhaseLogEntry,
  type TemplateJobStatusResponse,
} from "@/lib/api";
import { jobFailureCopy } from "@/lib/job-failure-copy";
import { useJobStream } from "@/hooks/useJobStream";
import {
  humanisePhase,
  phaseProgress,
  PHASE_LABEL,
  PHASE_ORDER,
} from "@/lib/template-job-phases";
import { BeamLoader } from "@/components/progress";

export default function TemplateJobPage() {
  const { id } = useParams<{ id: string }>();
  const { data: job, error } = useJobStream(id);

  if (error) {
    const failure = jobFailureCopy(error);
    return <ErrorScreen title={failure.title} message={failure.detail} supportReference={id} reload />;
  }
  if (!job) return <ProgressScreen job={null} />;
  if (job.status === "processing_failed") {
    const failure = jobFailureCopy(job.failure_reason ?? job.error_detail);
    return <ErrorScreen title={failure.title} message={failure.detail} supportReference={id} />;
  }
  // Admin-initiated cancel via POST /admin/jobs/{id}/cancel flips status
  // to "cancelled". Render distinctly from a pipeline failure so the user
  // doesn't think their video errored — it was deliberately stopped.
  if (job.status === "cancelled") {
    return (
      <ErrorScreen
        title="Video generation stopped"
        message="An administrator stopped this job before it finished. Choose a template to start again."
      />
    );
  }
  if (job.status !== "template_ready" || !job.assembly_plan?.output_url) {
    return <ProgressScreen job={job} />;
  }

  return <ResultView job={job} plan={job.assembly_plan} />;
}

// ── Progress + Error screens ─────────────────────────────────────────────────

/**
 * Live progress UI. Shows the current pipeline phase + a percent-style bar
 * driven off `phaseProgress(current_phase)`. Renders a queued placeholder
 * until the worker writes the first phase event. The bar fills smoothly
 * thanks to CSS `transition-all`; SSE delivers updates every ~750ms so the
 * user sees motion every couple of seconds during a 60s render.
 */
function ProgressScreen({ job }: { job: TemplateJobStatusResponse | null }) {
  const currentPhase = job?.current_phase ?? null;
  const status = job?.status ?? "queued";
  // Treat "queued" status as 0% — the worker hasn't picked it up yet.
  // Once a phase fires we lean on the phase index for the bar position.
  const progress = status === "queued" ? 0.02 : phaseProgress(currentPhase);
  const label =
    status === "queued"
      ? PHASE_LABEL.queued
      : humanisePhase(currentPhase);

  const completedPhases = new Set(
    (job?.phase_log ?? []).map((entry) => entry.name),
  );

  return (
    <main className="min-h-screen bg-black text-white flex flex-col items-center justify-center px-4">
      <div className="w-full max-w-md flex flex-col items-center gap-6">
        <BeamLoader
          tone="dark"
          mode="pulse"
          strength="medium"
          ariaLabel={label}
          className="h-12 w-12 rounded-full bg-zinc-950"
        >
          <div className="flex h-12 w-12 items-center justify-center">
            <span className="h-2 w-2 rounded-full bg-amber-300" />
          </div>
        </BeamLoader>
        <p className="text-zinc-200 text-base text-center">{label}</p>

        {/* Bar */}
        <div className="w-full h-1.5 bg-zinc-800 rounded-full overflow-hidden">
          <div
            className="h-full bg-white transition-all duration-700 ease-out"
            style={{ width: `${Math.round(progress * 100)}%` }}
          />
        </div>

        {/* Per-phase chips. Render the phases the user can actually see —
            queued is implicit, and we hide phases that don't apply to this
            template kind (single_video skips match_clips/mix_audio). */}
        <PhaseChips
          phaseLog={job?.phase_log ?? []}
          currentPhase={currentPhase}
          completedPhases={completedPhases}
        />

        {job?.started_at && (
          <ElapsedTimer startedAt={job.started_at} />
        )}
      </div>
    </main>
  );
}

function PhaseChips({
  phaseLog,
  currentPhase,
  completedPhases,
}: {
  phaseLog: PhaseLogEntry[];
  currentPhase: string | null;
  completedPhases: Set<string>;
}) {
  // Hide queued + finalize — they're internal book-ends, not user-meaningful
  // progress markers.
  const visible = PHASE_ORDER.filter(
    (p) => p !== "queued" && p !== "finalize",
  );
  if (!completedPhases.size && !currentPhase) return null;

  return (
    <div className="flex flex-wrap gap-1.5 justify-center w-full">
      {visible.map((phase) => {
        const isDone = completedPhases.has(phase);
        const isActive = phase === currentPhase;
        const entry = phaseLog.find((e) => e.name === phase);
        return (
          <span
            key={phase}
            className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] transition-colors ${
              isDone
                ? "bg-zinc-800 text-zinc-400"
                : isActive
                  ? "bg-white/10 text-white border border-white/30"
                  : "bg-zinc-900 text-zinc-600"
            }`}
            title={
              entry?.elapsed_ms != null
                ? `Took ${(entry.elapsed_ms / 1000).toFixed(1)}s`
                : undefined
            }
          >
            {isDone && (
              <span aria-hidden="true" className="text-green-400">✓</span>
            )}
            {isActive && (
              <span
                aria-hidden="true"
                className="w-1.5 h-1.5 rounded-full bg-white animate-pulse"
              />
            )}
            {PHASE_LABEL[phase].replace(/…$/, "")}
          </span>
        );
      })}
    </div>
  );
}

/** Ticking elapsed clock since the worker stamped started_at. Light-weight —
 *  1Hz update, only re-renders this small subtree. */
function ElapsedTimer({ startedAt }: { startedAt: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const startMs = new Date(startedAt).getTime();
  if (Number.isNaN(startMs)) return null;
  const elapsedS = Math.max(0, Math.round((now - startMs) / 1000));
  const mm = Math.floor(elapsedS / 60);
  const ss = elapsedS % 60;
  const formatted = mm > 0
    ? `${mm}:${ss.toString().padStart(2, "0")}`
    : `${ss}s`;
  return (
    <p className="text-xs text-zinc-600 tabular-nums" aria-label={`Elapsed time: ${formatted}`}>
      Elapsed {formatted}
    </p>
  );
}

function ErrorScreen({
  title,
  message,
  supportReference,
  reload = false,
}: {
  title: string;
  message: string;
  supportReference?: string;
  reload?: boolean;
}) {
  return (
    <main className="min-h-screen bg-black text-white flex flex-col items-center justify-center px-4" role="alert">
      <div className="max-w-md text-center">
        <h1 className="text-xl font-semibold mb-2">{title}</h1>
        <p className="text-zinc-300 mb-6">{message}</p>
        {reload ? (
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="inline-block px-6 py-2 bg-zinc-800 text-white rounded-lg text-sm hover:bg-zinc-700 transition-colors"
          >
            Reload page
          </button>
        ) : (
          <a
            href="/"
            className="inline-block px-6 py-2 bg-zinc-800 text-white rounded-lg text-sm hover:bg-zinc-700 transition-colors"
          >
            Choose another template
          </a>
        )}
        {supportReference && (
          <p className="mt-5 break-all font-mono text-xs text-zinc-500">
            Support reference: {supportReference}
          </p>
        )}
      </div>
    </main>
  );
}

// ── Slot-Aware Timeline Player ───────────────────────────────────────────────

const SLOT_COLORS: Record<string, string> = {
  hook: "bg-blue-500",
  broll: "bg-zinc-500",
  b_roll: "bg-zinc-500",
  outro: "bg-green-500",
  intro: "bg-purple-500",
  transition: "bg-yellow-500",
};

// Slot types are internal snake_case identifiers (e.g. "b_roll") — humanize
// for display without touching the underlying data contract.
function humanizeSlotType(slotType: string): string {
  return slotType
    .split("_")
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

// `steps` is required here — the parent always guards `steps.length > 0`
// before rendering this component. Use NonNullable so TS knows.
function TimelinePlayer({
  steps,
  videoRef,
}: {
  steps: NonNullable<AssemblyPlanData["steps"]>;
  videoRef: React.RefObject<HTMLVideoElement | null>;
}) {
  const [currentTime, setCurrentTime] = useState(0);
  const totalDuration = steps.reduce((sum, s) => sum + s.slot.target_duration_s, 0);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const handler = () => setCurrentTime(video.currentTime);
    video.addEventListener("timeupdate", handler);
    return () => video.removeEventListener("timeupdate", handler);
  }, [videoRef]);

  function seekToSlot(slotIndex: number) {
    const video = videoRef.current;
    if (!video) return;
    let cumulative = 0;
    for (let i = 0; i < slotIndex; i++) {
      cumulative += steps[i].slot.target_duration_s;
    }
    video.currentTime = cumulative;
    video.play().catch(() => {});
  }

  // Find active slot
  let cumTime = 0;
  let activeSlot = 0;
  for (let i = 0; i < steps.length; i++) {
    if (currentTime >= cumTime && currentTime < cumTime + steps[i].slot.target_duration_s) {
      activeSlot = i;
      break;
    }
    cumTime += steps[i].slot.target_duration_s;
    if (i === steps.length - 1) activeSlot = i;
  }

  const scrubberPercent = totalDuration > 0 ? (currentTime / totalDuration) * 100 : 0;

  return (
    <div className="mt-4">
      {/* Timeline bar */}
      <div className="relative flex h-8 rounded-lg overflow-hidden bg-zinc-900">
        {steps.map((step, i) => {
          const widthPercent = (step.slot.target_duration_s / totalDuration) * 100;
          const color = SLOT_COLORS[step.slot.slot_type] || "bg-zinc-600";
          const isActive = i === activeSlot;
          return (
            <button
              key={i}
              onClick={() => seekToSlot(i)}
              className={`${color} relative flex items-center justify-center text-[10px] font-medium text-white transition-all ${
                isActive ? "opacity-100 ring-1 ring-white" : "opacity-60 hover:opacity-80"
              }`}
              style={{
                width: `${widthPercent}%`,
                borderWidth: step.slot.priority ? `${Math.min(step.slot.priority, 10) * 0.3}px` : "1px",
                borderColor: "rgba(255,255,255,0.2)",
              }}
              title={`${humanizeSlotType(step.slot.slot_type)} · ${step.slot.target_duration_s.toFixed(1)}s`}
            >
              {widthPercent > 8 && humanizeSlotType(step.slot.slot_type)}
            </button>
          );
        })}
        {/* Scrubber line */}
        <div
          className="absolute top-0 bottom-0 w-0.5 bg-white z-10 pointer-events-none transition-all"
          style={{ left: `${Math.min(scrubberPercent, 100)}%` }}
        />
      </div>

      {/* Current slot info */}
      <div className="mt-2 text-xs text-zinc-400">
        Slot {steps[activeSlot]?.slot.position} · Clip {activeSlot + 1} · {steps[activeSlot] && humanizeSlotType(steps[activeSlot].slot.slot_type)} · {steps[activeSlot]?.slot.target_duration_s.toFixed(1)}s
      </div>
    </div>
  );
}

// ── Side-by-Side Comparison ──────────────────────────────────────────────────

function SideBySideComparison({
  templateId,
  outputUrl,
}: {
  templateId: string | null;
  outputUrl: string;
}) {
  const [templateUrl, setTemplateUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const templateVideoRef = useRef<HTMLVideoElement>(null);
  const outputVideoRef = useRef<HTMLVideoElement>(null);

  async function loadTemplateVideo() {
    if (!templateId || templateUrl) return;
    setLoading(true);
    try {
      const { url } = await getTemplatePlaybackUrl(templateId);
      setTemplateUrl(url);
    } catch {
      // Silently fail — template video is optional
    } finally {
      setLoading(false);
    }
  }

  function toggleExpanded() {
    setExpanded(!expanded);
    if (!expanded && !templateUrl) loadTemplateVideo();
  }

  function syncPlay() {
    templateVideoRef.current?.play().catch(() => {});
    outputVideoRef.current?.play().catch(() => {});
  }

  function syncPause() {
    templateVideoRef.current?.pause();
    outputVideoRef.current?.pause();
  }

  if (!templateId) return null;

  return (
    <div className="mt-8">
      <button
        onClick={toggleExpanded}
        className="text-sm text-zinc-400 hover:text-white transition-colors"
      >
        {expanded ? "Hide original template" : "Compare with original template"}
      </button>

      {expanded && (
        <div className="mt-4">
          <div className="flex gap-2 mb-3">
            <button
              onClick={syncPlay}
              className="px-3 py-1.5 bg-zinc-800 text-zinc-300 rounded text-xs hover:bg-zinc-700"
            >
              Play both videos
            </button>
            <button
              onClick={syncPause}
              className="px-3 py-1.5 bg-zinc-800 text-zinc-300 rounded text-xs hover:bg-zinc-700"
            >
              Pause both videos
            </button>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <p className="text-xs text-zinc-500 mb-1">Original template</p>
              {loading ? (
                <BeamLoader
                  tone="dark"
                  mode="frame"
                  strength="subtle"
                  ariaLabel="Loading original template"
                  className="h-48 rounded-lg bg-zinc-900"
                >
                  <div className="flex h-48 items-center justify-center">
                    <span className="h-2 w-2 rounded-full bg-amber-300" />
                  </div>
                </BeamLoader>
              ) : templateUrl ? (
                <video
                  ref={templateVideoRef}
                  src={templateUrl}
                  controls
                  className="w-full rounded-lg bg-zinc-900"
                />
              ) : (
                <div className="h-48 bg-zinc-900 rounded-lg flex items-center justify-center text-zinc-600 text-xs">
                  Original template unavailable
                </div>
              )}
            </div>
            <div>
              <p className="text-xs text-zinc-500 mb-1">Your video</p>
              <video
                ref={outputVideoRef}
                src={outputUrl}
                controls
                className="w-full rounded-lg bg-zinc-900"
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Reroll Button ────────────────────────────────────────────────────────────

function RerollButton({ jobId }: { jobId: string }) {
  const router = useRouter();
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ReturnType<typeof jobFailureCopy> | null>(null);
  const [rerollCount, setRerollCount] = useState(0);

  const MAX_REROLLS = 2;

  async function handleReroll() {
    if (rerollCount >= MAX_REROLLS) return;
    if (error && error.action !== "retry_render" && error.action !== "contact_support") {
      router.push("/");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const { job_id } = await rerollTemplateJob(jobId);
      setRerollCount((c) => c + 1);
      router.push(`/template-jobs/${job_id}`);
    } catch (err) {
      setError(jobFailureCopy(err instanceof Error ? err.message : null));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="mt-6">
      <button
        onClick={() => setExpanded(!expanded)}
        className="text-sm text-zinc-500 hover:text-zinc-300 transition-colors"
      >
        {expanded ? "Hide options" : "Create another version"}
      </button>

      {expanded && (
        <div className="mt-3 bg-zinc-900 rounded-lg p-4">
          <p className="text-xs text-zinc-400 mb-3">
            Create another version from the same clips with a different edit.
            {rerollCount > 0 && ` (${MAX_REROLLS - rerollCount} version${MAX_REROLLS - rerollCount !== 1 ? "s" : ""} remaining)`}
          </p>
          <button
            onClick={handleReroll}
            disabled={loading || rerollCount >= MAX_REROLLS}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              loading || rerollCount >= MAX_REROLLS
                ? "bg-zinc-800 text-zinc-500 cursor-not-allowed"
                : "bg-zinc-700 text-white hover:bg-zinc-600"
            }`}
          >
            {loading
              ? "Creating another version…"
              : rerollCount >= MAX_REROLLS
                ? "No more versions available"
                : error
                  ? error.actionLabel
                  : "Create another version"}
          </button>
          {error && (
            <div className="mt-2 text-xs text-red-400" role="alert">
              <p className="font-medium">{error.title}</p>
              <p className="mt-1">{error.detail}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Result View (main) ───────────────────────────────────────────────────────

function ResultView({
  job,
  plan,
}: {
  job: TemplateJobStatusResponse;
  plan: AssemblyPlanData;
}) {
  const copy = plan.platform_copy;
  const videoRef = useRef<HTMLVideoElement>(null);
  // single_video templates write no `steps` array — only multi-clip
  // templates have slots. Default to [] so the timeline + breakdown
  // sections collapse cleanly instead of crashing the render.
  const steps = plan.steps ?? [];

  return (
    <main className="min-h-screen bg-black text-white px-4 py-16">
      <div className="max-w-2xl mx-auto">
        <h1 className="text-2xl font-bold mb-2 text-center">Your video is ready</h1>
        {steps.length > 0 && (
          <p className="text-zinc-400 text-sm text-center mb-8">
            Built from {steps.length} clip{steps.length !== 1 ? "s" : ""}
          </p>
        )}

        {/* Video player */}
        <div className="rounded-2xl overflow-hidden bg-zinc-900">
          <video
            ref={videoRef}
            src={plan.output_url}
            controls
            className="w-full max-h-[70vh] object-contain"
            autoPlay={false}
          />
        </div>

        {/* Slot-Aware Timeline (multi-clip templates only) */}
        {steps.length > 0 && (
          <TimelinePlayer steps={steps} videoRef={videoRef} />
        )}

        {/* Download */}
        <div className="flex justify-center mt-6 mb-4">
          <button
            onClick={async () => {
              try {
                const res = await fetch(plan.output_url!);
                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = `kria-${job.job_id.slice(0, 8)}.mp4`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
              } catch {
                window.open(plan.output_url!, "_blank");
              }
            }}
            className="px-6 py-2.5 bg-white text-black rounded-lg text-sm font-semibold hover:bg-zinc-200 transition-colors"
          >
            Download video
          </button>
        </div>

        {/* Reroll */}
        <RerollButton jobId={job.job_id} />

        {/* Side-by-side comparison */}
        <SideBySideComparison
          templateId={job.template_id}
          outputUrl={plan.output_url!}
        />

        {/* Platform copy */}
        {copy && (
          <div className="mt-8 space-y-4">
            <h2 className="text-lg font-semibold">Suggested captions</h2>
            {copy.tiktok && (
              <CopyCard
                platform="TikTok"
                fields={[
                  { label: "Hook", value: copy.tiktok.hook },
                  { label: "Caption", value: copy.tiktok.caption },
                  { label: "Hashtags", value: copy.tiktok.hashtags.map((h) => `#${h}`).join(" ") },
                ]}
              />
            )}
            {copy.instagram && (
              <CopyCard
                platform="Instagram"
                fields={[
                  { label: "Hook", value: copy.instagram.hook },
                  { label: "Caption", value: copy.instagram.caption },
                  { label: "Hashtags", value: copy.instagram.hashtags.map((h) => `#${h}`).join(" ") },
                ]}
              />
            )}
            {copy.youtube && (
              <CopyCard
                platform="YouTube"
                fields={[
                  { label: "Title", value: copy.youtube.title },
                  { label: "Description", value: copy.youtube.description },
                  { label: "Tags", value: copy.youtube.tags.join(", ") },
                ]}
              />
            )}
          </div>
        )}

        <p className="mt-8 text-center text-xs text-zinc-600">
          <a href="/" className="underline hover:text-zinc-400">Choose another template</a>
        </p>
      </div>
    </main>
  );
}

function CopyCard({
  platform,
  fields,
}: {
  platform: string;
  fields: Array<{ label: string; value: string }>;
}) {
  return (
    <div className="bg-zinc-900 rounded-xl p-4">
      <p className="text-xs text-zinc-500 font-medium uppercase tracking-wider mb-3">{platform}</p>
      <dl className="space-y-2">
        {fields.map(({ label, value }) => (
          <div key={label}>
            <dt className="text-xs text-zinc-500">{label}</dt>
            <dd className="text-sm text-zinc-200 mt-0.5">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
