"use client";

import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { getJobStatus, type ClipStatus, type JobStatus, type JobStatusResponse } from "@/lib/api";

const POLL_INTERVAL_MS = 3000;

const STAGE_LABELS: Record<string, string> = {
  queued: "Waiting in queue...",
  processing: "Processing video...",
  clips_ready: "Clips ready!",
  clips_ready_partial: "Clips ready (partial)",
  posting: "Posting...",
  posting_partial: "Posted with some errors",
  done: "All posted!",
  posting_failed: "Posting failed",
  processing_failed: "Processing failed",
};

const TERMINAL_STATES = new Set<JobStatus>([
  "clips_ready",
  "clips_ready_partial",
  "done",
  "posting_failed",
  "processing_failed",
]);

export default function JobPage() {
  const { id } = useParams<{ id: string }>();
  const [job, setJob] = useState<JobStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    async function poll() {
      try {
        const data = await getJobStatus(id);
        setJob(data);
        if (TERMINAL_STATES.has(data.status)) {
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
      <main className="min-h-screen bg-black text-white flex items-center justify-center">
        <p className="text-red-400">{error}</p>
      </main>
    );
  }

  if (!job) {
    return (
      <main className="min-h-screen bg-black text-white flex items-center justify-center">
        <p className="text-zinc-400">Loading...</p>
      </main>
    );
  }

  const isProcessing = !TERMINAL_STATES.has(job.status);
  const readyClips = job.clips.filter((c) => c.render_status === "ready");
  const failedClips = job.clips.filter((c) => c.render_status === "failed");

  return (
    <main className="min-h-screen bg-black text-white p-6">
      <div className="max-w-2xl mx-auto">
        <h1 className="text-2xl font-bold mb-2">Nova</h1>

        {/* Status bar */}
        <div className="mb-8">
          <p className="text-zinc-400 text-sm mb-1">{STAGE_LABELS[job.status] ?? job.status}</p>
          {isProcessing && (
            <div className="w-full h-1 bg-zinc-800 rounded">
              <div className="h-1 bg-white rounded animate-pulse w-1/3" />
            </div>
          )}
          {job.status === "processing_failed" && (
            <p className="text-red-400 text-sm mt-2">
              {job.error_detail ?? "Something went wrong. Try re-uploading your video."}
            </p>
          )}
        </div>

        {/* Clip cards */}
        {readyClips.length > 0 && (
          <>
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold">
                {readyClips.length === 3 ? "3 clips ready to post" : `${readyClips.length} clip${readyClips.length > 1 ? "s" : ""} ready`}
              </h2>
              {readyClips.length > 1 && (
                <button className="px-5 py-2 bg-white text-black rounded-full text-sm font-semibold hover:bg-zinc-200 transition-colors">
                  Post All {readyClips.length}
                </button>
              )}
            </div>
            <div className="space-y-4">
              {readyClips.map((clip) => (
                <ClipCard key={clip.id} clip={clip} />
              ))}
            </div>
          </>
        )}

        {/* Failed clip notice */}
        {failedClips.length > 0 && (
          <p className="text-zinc-500 text-sm mt-4">
            {failedClips.length} clip{failedClips.length > 1 ? "s" : ""} failed to render.
          </p>
        )}

        {/* Processing placeholder cards */}
        {isProcessing && (
          <div className="space-y-4 mt-4">
            {[1, 2, 3].map((n) => (
              <div key={n} className="border border-zinc-800 rounded-2xl p-5 animate-pulse">
                <div className="h-4 bg-zinc-800 rounded w-3/4 mb-3" />
                <div className="h-3 bg-zinc-800 rounded w-1/2" />
              </div>
            ))}
          </div>
        )}
      </div>
    </main>
  );
}

function ClipCard({ clip }: { clip: ClipStatus }) {
  const [showCopy, setShowCopy] = useState(false);

  return (
    <div className="border border-zinc-800 rounded-2xl p-5">
      <div className="flex items-start justify-between mb-3">
        <div>
          <span className="text-xs text-zinc-500 uppercase tracking-wider">
            #{clip.rank} · {clip.duration_s?.toFixed(0)}s
          </span>
          {clip.hook_text && (
            <p className="text-white font-medium mt-1">&ldquo;{clip.hook_text}&rdquo;</p>
          )}
        </div>
        <div className="flex gap-2 shrink-0 ml-4">
          <button
            onClick={() => setShowCopy(!showCopy)}
            className="text-xs text-zinc-400 border border-zinc-700 rounded-full px-3 py-1 hover:border-zinc-500 transition-colors"
          >
            {showCopy ? "Hide copy" : "See copy"}
          </button>
          <button className="text-xs bg-white text-black rounded-full px-3 py-1 font-semibold hover:bg-zinc-200 transition-colors">
            Post
          </button>
        </div>
      </div>

      {/* Score indicators */}
      <div className="flex gap-4 text-xs text-zinc-500">
        <span>Hook {clip.hook_score.toFixed(1)}</span>
        <span>Engagement {clip.engagement_score.toFixed(1)}</span>
        <span>Score {clip.combined_score.toFixed(1)}</span>
      </div>

      {/* Copy fallback warning */}
      {clip.copy_status === "generated_fallback" && (
        <p className="text-amber-400 text-xs mt-2">
          Auto-copy failed — edit before posting
        </p>
      )}

      {/* Platform copy */}
      {showCopy && clip.platform_copy && (
        <div className="mt-4 space-y-3 text-sm border-t border-zinc-800 pt-4">
          {clip.platform_copy.instagram && (
            <div>
              <p className="text-zinc-500 text-xs mb-1">Instagram</p>
              <p className="text-zinc-200">{clip.platform_copy.instagram.hook}</p>
              <p className="text-zinc-400 text-xs mt-1">
                {clip.platform_copy.instagram.hashtags.map((h) => `#${h}`).join(" ")}
              </p>
            </div>
          )}
          {clip.platform_copy.youtube && (
            <div>
              <p className="text-zinc-500 text-xs mb-1">YouTube</p>
              <p className="text-zinc-200">{clip.platform_copy.youtube.title}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
