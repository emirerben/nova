"use client";

import { useEffect, useState } from "react";
import {
  getMusicTracks,
  createMusicJob,
  getMusicJobStatus,
  type MusicTrackSummary,
  type MusicJobStatus,
} from "@/lib/music-api";

const TERMINAL_STATUSES = ["music_ready", "processing_failed"] as const;

// ── Track Card ────────────────────────────────────────────────────────────────

function TrackCard({
  track,
  selected,
  onClick,
}: {
  track: MusicTrackSummary;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`w-full text-left rounded-xl border-2 transition-colors overflow-hidden bg-zinc-900 ${
        selected ? "border-violet-500" : "border-zinc-700 hover:border-zinc-500"
      }`}
    >
      {track.thumbnail_url ? (
        <img
          src={track.thumbnail_url}
          alt={track.title}
          className="w-full aspect-video object-cover"
        />
      ) : (
        <div className="w-full aspect-video bg-zinc-800 flex items-center justify-center">
          <span className="text-4xl">🎵</span>
        </div>
      )}
      <div className="p-3">
        <p className="font-semibold text-white truncate">{track.title}</p>
        <p className="text-sm text-zinc-400 truncate">{track.artist || "Unknown artist"}</p>
        <p className="text-xs text-zinc-500 mt-1">
          {track.section_duration_s}s · {track.required_clips_min}–{track.required_clips_max} clips
        </p>
      </div>
    </button>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function MusicPage() {
  const [tracks, setTracks] = useState<MusicTrackSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [selectedTrack, setSelectedTrack] = useState<MusicTrackSummary | null>(null);
  const [clipPaths, setClipPaths] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [jobStatus, setJobStatus] = useState<MusicJobStatus | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    getMusicTracks()
      .then((r) => setTracks(r.tracks))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  // Poll job status until terminal state
  useEffect(() => {
    if (!jobStatus) return;
    if ((TERMINAL_STATUSES as readonly string[]).includes(jobStatus.status)) return;

    const id = setInterval(async () => {
      try {
        const updated = await getMusicJobStatus(jobStatus.job_id);
        setJobStatus(updated);
        if ((TERMINAL_STATUSES as readonly string[]).includes(updated.status)) clearInterval(id);
      } catch {
        // keep polling
      }
    }, 3000);
    return () => clearInterval(id);
  }, [jobStatus]);

  async function handleSubmit() {
    if (!selectedTrack) return;
    const paths = clipPaths
      .split("\n")
      .map((p) => p.trim())
      .filter(Boolean);

    if (paths.length < selectedTrack.required_clips_min) {
      setSubmitError(
        `Need at least ${selectedTrack.required_clips_min} clips, got ${paths.length}.`,
      );
      return;
    }

    setSubmitting(true);
    setSubmitError(null);
    try {
      const job = await createMusicJob(selectedTrack.id, paths);
      const status = await getMusicJobStatus(job.job_id);
      setJobStatus(status);
    } catch (e: unknown) {
      setSubmitError(e instanceof Error ? e.message : "Unknown error");
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) {
    return (
      <div className="min-h-screen bg-zinc-950 text-zinc-100 flex items-center justify-center">
        <p className="text-zinc-400">Loading music gallery…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen bg-zinc-950 text-zinc-100 flex items-center justify-center">
        <p className="text-red-400">Error: {error}</p>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100 p-6 max-w-5xl mx-auto">
      <h1 className="text-3xl font-bold mb-2">Music Beat-Sync</h1>
      <p className="text-zinc-400 mb-8">
        Pick a track, upload your clips, and get a beat-synced video.
      </p>

      {/* Gallery */}
      {tracks.length === 0 ? (
        <p className="text-zinc-500">No published tracks yet. Check back soon.</p>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-4 mb-10">
          {tracks.map((t) => (
            <TrackCard
              key={t.id}
              track={t}
              selected={selectedTrack?.id === t.id}
              onClick={() => {
                setSelectedTrack(t);
                setJobStatus(null);
                setSubmitError(null);
              }}
            />
          ))}
        </div>
      )}

      {/* Submission form */}
      {selectedTrack && !jobStatus && (
        <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-6">
          <h2 className="text-xl font-semibold mb-1">{selectedTrack.title}</h2>
          <p className="text-sm text-zinc-400 mb-4">
            Upload {selectedTrack.required_clips_min}–{selectedTrack.required_clips_max} clips as
            GCS paths (one per line)
          </p>

          <textarea
            className="w-full h-32 bg-zinc-800 border border-zinc-600 rounded-lg p-3 text-sm font-mono text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-violet-500"
            placeholder={"clips/abc123/clip1.mp4\nclips/abc123/clip2.mp4"}
            value={clipPaths}
            onChange={(e) => setClipPaths(e.target.value)}
          />

          {submitError && (
            <p className="text-red-400 text-sm mt-2">{submitError}</p>
          )}

          <button
            onClick={handleSubmit}
            disabled={submitting || !clipPaths.trim()}
            className="mt-4 w-full bg-violet-600 hover:bg-violet-500 disabled:opacity-40 disabled:cursor-not-allowed text-white font-semibold py-3 rounded-lg transition-colors"
          >
            {submitting ? "Creating job…" : "Create beat-sync video"}
          </button>
        </div>
      )}

      {/* Job status */}
      {jobStatus && (
        <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-6 mt-4">
          <h2 className="text-xl font-semibold mb-2">Job status</h2>
          <p className="text-sm text-zinc-400 font-mono mb-1">ID: {jobStatus.job_id}</p>
          <StatusBadge status={jobStatus.status} />

          {jobStatus.status === "processing_failed" && (
            <p className="text-red-400 text-sm mt-2">{jobStatus.error_detail}</p>
          )}

          {jobStatus.status === "music_ready" && jobStatus.assembly_plan && (
            <p className="text-green-400 text-sm mt-2">
              Video ready:{" "}
              <code className="font-mono">
                {(jobStatus.assembly_plan as { output_url?: string }).output_url ?? "—"}
              </code>
            </p>
          )}

          {!(TERMINAL_STATUSES as readonly string[]).includes(jobStatus.status) && (
            <p className="text-zinc-400 text-sm mt-2 animate-pulse">Processing…</p>
          )}
        </div>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    queued: "bg-zinc-700 text-zinc-300",
    processing: "bg-blue-900 text-blue-300",
    music_ready: "bg-green-900 text-green-300",
    processing_failed: "bg-red-900 text-red-300",
  };
  const cls = colors[status] ?? "bg-zinc-700 text-zinc-300";
  return (
    <span className={`inline-block px-3 py-1 rounded-full text-xs font-semibold ${cls}`}>
      {status}
    </span>
  );
}
