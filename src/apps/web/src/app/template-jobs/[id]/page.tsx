"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  getTemplateJobStatus,
  getTemplatePlaybackUrl,
  rerollTemplateJob,
  type AssemblyPlanData,
  type TemplateJobStatus,
  type TemplateJobStatusResponse,
} from "@/lib/api";

const POLL_INTERVAL_MS = 4000;
const POLL_TIMEOUT_MS = 10 * 60 * 1000;

const STAGE_LABELS: Record<TemplateJobStatus, string> = {
  queued: "Waiting in queue...",
  processing: "AI is analyzing and assembling your clips...",
  template_ready: "Your video is ready!",
  processing_failed: "Processing failed",
};

const TERMINAL = new Set<TemplateJobStatus>(["template_ready", "processing_failed"]);

export default function TemplateJobPage() {
  const { id } = useParams<{ id: string }>();
  const [job, setJob] = useState<TemplateJobStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startTimeRef = useRef(Date.now());

  useEffect(() => {
    async function poll() {
      if (Date.now() - startTimeRef.current > POLL_TIMEOUT_MS) {
        setError("Processing is taking unusually long. The worker may be down — check server logs.");
        clearInterval(intervalRef.current!);
        return;
      }
      try {
        const data = await getTemplateJobStatus(id);
        setJob(data);
        if (TERMINAL.has(data.status)) {
          clearInterval(intervalRef.current!);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to fetch status");
        clearInterval(intervalRef.current!);
      }
    }

    poll();
    intervalRef.current = setInterval(poll, POLL_INTERVAL_MS);
    return () => clearInterval(intervalRef.current!);
  }, [id]);

  if (error) return <ErrorScreen message={error} jobId={id} />;
  if (!job) return <LoadingScreen message="Loading..." />;
  if (job.status === "processing_failed") {
    return <ErrorScreen message={job.error_detail ?? "Processing failed. Please try again."} jobId={id} />;
  }
  if (job.status !== "template_ready" || !job.assembly_plan?.output_url) {
    return <LoadingScreen message={STAGE_LABELS[job.status]} />;
  }

  return <ResultView job={job} plan={job.assembly_plan} />;
}

// ── Loading & Error ──────────────────────────────────────────────────────────

function LoadingScreen({ message }: { message: string }) {
  return (
    <main className="min-h-screen bg-black text-white flex flex-col items-center justify-center px-4">
      <div className="flex flex-col items-center gap-4">
        <div className="w-10 h-10 border-2 border-zinc-600 border-t-white rounded-full animate-spin" />
        <p className="text-zinc-400 text-sm">{message}</p>
      </div>
    </main>
  );
}

function ErrorScreen({ message, jobId }: { message: string; jobId: string }) {
  return (
    <main className="min-h-screen bg-black text-white flex flex-col items-center justify-center px-4">
      <div className="max-w-md text-center">
        <p className="text-red-400 mb-4">{message}</p>
        <a
          href="/template"
          className="inline-block px-6 py-2 bg-zinc-800 text-white rounded-lg text-sm hover:bg-zinc-700 transition-colors"
        >
          Upload again
        </a>
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

function TimelinePlayer({
  steps,
  videoRef,
}: {
  steps: AssemblyPlanData["steps"];
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
              title={`${step.slot.slot_type} · ${step.slot.target_duration_s.toFixed(1)}s`}
            >
              {widthPercent > 8 && step.slot.slot_type}
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
        Slot {steps[activeSlot]?.slot.position} · Clip {activeSlot + 1} · {steps[activeSlot]?.slot.slot_type} · {steps[activeSlot]?.slot.target_duration_s.toFixed(1)}s
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
        {expanded ? "▾" : "▸"} Compare with original template
      </button>

      {expanded && (
        <div className="mt-4">
          <div className="flex gap-2 mb-3">
            <button
              onClick={syncPlay}
              className="px-3 py-1.5 bg-zinc-800 text-zinc-300 rounded text-xs hover:bg-zinc-700"
            >
              ▶ Play both
            </button>
            <button
              onClick={syncPause}
              className="px-3 py-1.5 bg-zinc-800 text-zinc-300 rounded text-xs hover:bg-zinc-700"
            >
              ⏸ Pause both
            </button>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <p className="text-xs text-zinc-500 mb-1">Original Template</p>
              {loading ? (
                <div className="h-48 bg-zinc-900 rounded-lg flex items-center justify-center">
                  <div className="w-6 h-6 border-2 border-zinc-600 border-t-white rounded-full animate-spin" />
                </div>
              ) : templateUrl ? (
                <video
                  ref={templateVideoRef}
                  src={templateUrl}
                  controls
                  className="w-full rounded-lg bg-zinc-900"
                />
              ) : (
                <div className="h-48 bg-zinc-900 rounded-lg flex items-center justify-center text-zinc-600 text-xs">
                  Template video unavailable
                </div>
              )}
            </div>
            <div>
              <p className="text-xs text-zinc-500 mb-1">Your Output</p>
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
  const [error, setError] = useState<string | null>(null);
  const [rerollCount, setRerollCount] = useState(0);

  const MAX_REROLLS = 2;

  async function handleReroll() {
    if (rerollCount >= MAX_REROLLS) return;
    setLoading(true);
    setError(null);
    try {
      const { job_id } = await rerollTemplateJob(jobId);
      setRerollCount((c) => c + 1);
      router.push(`/template-jobs/${job_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reroll failed");
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
        {expanded ? "▾" : "▸"} These don&apos;t look right?
      </button>

      {expanded && (
        <div className="mt-3 bg-zinc-900 rounded-lg p-4">
          <p className="text-xs text-zinc-400 mb-3">
            Re-rolling uses the same clips but produces a different assembly.
            {rerollCount > 0 && ` (${MAX_REROLLS - rerollCount} re-roll${MAX_REROLLS - rerollCount !== 1 ? "s" : ""} remaining)`}
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
            {loading ? "Re-rolling..." : rerollCount >= MAX_REROLLS ? "No re-rolls left" : "Try different clips"}
          </button>
          {error && <p className="text-red-400 text-xs mt-2">{error}</p>}
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

  return (
    <main className="min-h-screen bg-black text-white px-4 py-16">
      <div className="max-w-2xl mx-auto">
        <h1 className="text-2xl font-bold mb-2 text-center">Your template video is ready</h1>
        <p className="text-zinc-400 text-sm text-center mb-8">
          {plan.steps.length} shot{plan.steps.length !== 1 ? "s" : ""} assembled from your clips
        </p>

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

        {/* Slot-Aware Timeline */}
        {plan.steps.length > 0 && (
          <TimelinePlayer steps={plan.steps} videoRef={videoRef} />
        )}

        {/* Download */}
        <div className="flex justify-center mt-6 mb-4">
          <a
            href={plan.output_url}
            download
            className="px-6 py-2.5 bg-white text-black rounded-lg text-sm font-semibold hover:bg-zinc-200 transition-colors"
          >
            Download video
          </a>
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
            <h2 className="text-lg font-semibold">Caption copy</h2>
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

        {/* Assembly breakdown */}
        <div className="mt-8">
          <h2 className="text-lg font-semibold mb-3">Assembly breakdown</h2>
          <div className="space-y-2">
            {plan.steps.map((step, i) => (
              <div
                key={i}
                className="bg-zinc-900 rounded-lg px-4 py-3 text-sm flex items-center justify-between"
              >
                <span className="text-zinc-300">
                  Shot {step.slot.position} · {step.slot.slot_type}
                </span>
                <span className="text-zinc-500">
                  {step.moment.start_s.toFixed(1)}s – {step.moment.end_s.toFixed(1)}s
                </span>
              </div>
            ))}
          </div>
        </div>

        <p className="mt-8 text-center text-xs text-zinc-600">
          <a href="/template" className="underline hover:text-zinc-400">← Create another</a>
          {" · "}
          <a href="/template-jobs" className="underline hover:text-zinc-400">QA Dashboard</a>
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
