"use client";

import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  getTemplateJobStatus,
  type AssemblyPlanData,
  type TemplateJobStatus,
  type TemplateJobStatusResponse,
} from "@/lib/api";

const POLL_INTERVAL_MS = 4000;
const POLL_TIMEOUT_MS = 10 * 60 * 1000; // 10 min

const STAGE_LABELS: Record<TemplateJobStatus, string> = {
  queued: "Waiting in queue...",
  processing: "AI is analyzing and assembling your clips...",
  template_ready: "Your video is ready!",
  processing_failed: "Processing failed",
};

const TERMINAL: Set<TemplateJobStatus> = new Set(["template_ready", "processing_failed"]);

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

  if (error) {
    return (
      <ErrorScreen message={error} jobId={id} />
    );
  }

  if (!job) {
    return <LoadingScreen message="Loading..." />;
  }

  if (job.status === "processing_failed") {
    return (
      <ErrorScreen
        message={job.error_detail ?? "Processing failed. Please try again."}
        jobId={id}
      />
    );
  }

  if (job.status !== "template_ready" || !job.assembly_plan?.output_url) {
    return <LoadingScreen message={STAGE_LABELS[job.status]} />;
  }

  return <ResultView job={job} plan={job.assembly_plan} />;
}

// ── Sub-components ────────────────────────────────────────────────────────────

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

function ResultView({
  job,
  plan,
}: {
  job: TemplateJobStatusResponse;
  plan: AssemblyPlanData;
}) {
  const copy = plan.platform_copy;

  return (
    <main className="min-h-screen bg-black text-white px-4 py-16">
      <div className="max-w-2xl mx-auto">
        <h1 className="text-2xl font-bold mb-2 text-center">Your template video is ready</h1>
        <p className="text-zinc-400 text-sm text-center mb-8">
          {plan.steps.length} shot{plan.steps.length !== 1 ? "s" : ""} assembled from your clips
        </p>

        {/* Video player */}
        <div className="rounded-2xl overflow-hidden bg-zinc-900 mb-8">
          <video
            src={plan.output_url}
            controls
            className="w-full max-h-[70vh] object-contain"
            autoPlay={false}
          />
        </div>

        {/* Download */}
        <div className="flex justify-center mb-8">
          <a
            href={plan.output_url}
            download
            className="px-6 py-2.5 bg-white text-black rounded-lg text-sm font-semibold hover:bg-zinc-200 transition-colors"
          >
            Download video
          </a>
        </div>

        {/* Platform copy */}
        {copy && (
          <div className="space-y-4">
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

        {/* Shot breakdown */}
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
