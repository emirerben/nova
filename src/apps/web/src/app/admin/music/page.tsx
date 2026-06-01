"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  adminListMusicTracks,
  adminCreateMusicTrack,
  adminUploadMusicTrack,
  detectExtension,
  extensionIngest,
  ExtensionDedupError,
  type IngestProgress,
  type MusicTrackListItem,
} from "@/lib/music-api";

const STATUS_COLORS: Record<string, string> = {
  // "pending" = init created the row but bytes haven't landed in GCS yet.
  // Distinct color so admins don't confuse this with "queued" (which means
  // Celery has the work and analysis is in flight). Amber communicates
  // "waiting on something external (the browser upload)".
  pending: "bg-amber-900 text-amber-300",
  queued: "bg-zinc-700 text-zinc-300",
  analyzing: "bg-blue-900 text-blue-300",
  ready: "bg-green-900 text-green-300",
  failed: "bg-red-900 text-red-300",
};

export default function AdminMusicPage() {
  const [tracks, setTracks] = useState<MusicTrackListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [addMode, setAddMode] = useState<"url" | "upload">("upload");
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [artist, setArtist] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);

  // Browser-extension ingest state. Detection runs once on mount; the button
  // stays disabled with an "Install extension" hint when not reachable.
  const [extensionAvailable, setExtensionAvailable] = useState<boolean | null>(null);
  const [extProgress, setExtProgress] = useState<IngestProgress | null>(null);
  const [extIngesting, setExtIngesting] = useState(false);

  async function loadTracks() {
    setLoading(true);
    try {
      const data = await adminListMusicTracks(50, 0);
      setTracks(data.tracks);
      setTotal(data.total);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load tracks");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadTracks();
  }, []);

  useEffect(() => {
    // One-shot detection. If the user installs the extension mid-session they
    // can hit "Recheck" — we don't poll, polling burns the extension's runtime
    // wakeup budget for no reason.
    detectExtension().then(setExtensionAvailable);
  }, []);

  async function handleExtensionIngest() {
    if (!url.trim()) {
      setCreateError("Enter a YouTube URL first");
      return;
    }
    setExtIngesting(true);
    setCreateError(null);
    setExtProgress({ stage: "extension_check" });
    try {
      await extensionIngest(
        { url: url.trim(), title: title || undefined, artist: artist || undefined },
        (p) => setExtProgress(p),
      );
      setUrl("");
      setTitle("");
      setArtist("");
      setExtProgress(null);
      await loadTracks();
    } catch (e: unknown) {
      if (e instanceof ExtensionDedupError) {
        setCreateError(
          `Already ingested in the last 24h (status: ${e.existing_status}). ` +
            `Existing track: ${e.existing_track_id}`,
        );
      } else {
        setCreateError(e instanceof Error ? e.message : "Extension ingest failed");
      }
    } finally {
      setExtIngesting(false);
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setCreating(true);
    setCreateError(null);
    try {
      if (addMode === "upload") {
        if (!file) return;
        setExtProgress({ stage: "uploading", percent: 0 });
        await adminUploadMusicTrack(
          file,
          title || undefined,
          artist || undefined,
          // Reuse the extension flow's 3-stage progress UI so the admin never
          // sees a single spinner — uploads of ~10 MB on slow uplinks otherwise
          // look like the browser hung.
          (p) => setExtProgress(p),
        );
        setFile(null);
        setExtProgress(null);
      } else {
        await adminCreateMusicTrack(url, title || undefined, artist || undefined);
        setUrl("");
      }
      setTitle("");
      setArtist("");
      await loadTracks();
    } catch (err: unknown) {
      // Fallback chain: prefer Error.message, then a generic message. The
      // previous fetch wrapper used `?? "Failed..."` which let an empty string
      // through (HTTP/2 + Vercel 413 returns "" statusText) so the red error
      // box rendered "" — falsy — and the user saw "no reaction". Keeping the
      // guard here belt-and-suspenders so any future blank-message error still
      // surfaces something.
      const raw = err instanceof Error ? err.message : "";
      setCreateError(raw || "Failed to create track");
      if (addMode === "upload") {
        setExtProgress({ stage: "failed", detail: raw || "Upload failed" });
      }
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100 p-6 max-w-4xl mx-auto">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin" className="text-zinc-400 hover:text-zinc-200 text-sm">
          ← Admin
        </Link>
        <h1 className="text-2xl font-bold">Music Tracks</h1>
        <span className="text-zinc-500 text-sm ml-auto">{total} total</span>
      </div>

      {/* Add track form */}
      <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5 mb-8">
        <div className="flex items-center gap-1 mb-4">
          <button
            type="button"
            onClick={() => setAddMode("upload")}
            className={`text-sm font-semibold px-3 py-1.5 rounded-lg transition-colors ${
              addMode === "upload"
                ? "bg-violet-600 text-white"
                : "bg-zinc-800 text-zinc-400 hover:text-zinc-200"
            }`}
          >
            Upload file
          </button>
          <button
            type="button"
            onClick={() => setAddMode("url")}
            className={`text-sm font-semibold px-3 py-1.5 rounded-lg transition-colors ${
              addMode === "url"
                ? "bg-violet-600 text-white"
                : "bg-zinc-800 text-zinc-400 hover:text-zinc-200"
            }`}
          >
            From URL
          </button>
        </div>
        <form onSubmit={handleCreate} className="space-y-3">
          {addMode === "upload" ? (
            <div>
              <input
                type="file"
                accept=".m4a,.mp3,.wav,.ogg,.aac,.mp4,.webm,.opus,audio/*"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                className="w-full text-sm text-zinc-400 file:mr-3 file:py-2 file:px-4 file:rounded-lg file:border-0 file:bg-zinc-700 file:text-zinc-200 file:font-semibold file:text-sm hover:file:bg-zinc-600 file:cursor-pointer file:transition-colors"
              />
              {file && (
                <p className="text-xs text-zinc-500 mt-1">
                  {file.name} ({(file.size / 1024 / 1024).toFixed(1)} MB)
                </p>
              )}
            </div>
          ) : (
            <input
              required={addMode === "url"}
              type="url"
              placeholder="YouTube or SoundCloud URL"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              className="w-full bg-zinc-800 border border-zinc-600 rounded-lg px-3 py-2 text-sm font-mono text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-violet-500"
            />
          )}
          <div className="flex gap-3">
            <input
              type="text"
              placeholder="Title (optional)"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="flex-1 bg-zinc-800 border border-zinc-600 rounded-lg px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-violet-500"
            />
            <input
              type="text"
              placeholder="Artist (optional)"
              value={artist}
              onChange={(e) => setArtist(e.target.value)}
              className="flex-1 bg-zinc-800 border border-zinc-600 rounded-lg px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-violet-500"
            />
          </div>
          {createError && <p className="text-red-400 text-sm mt-2">{createError}</p>}
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="submit"
              disabled={
                creating ||
                extIngesting ||
                (addMode === "upload" ? !file : !url.trim())
              }
              className="bg-violet-600 hover:bg-violet-500 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-semibold px-5 py-2 rounded-lg transition-colors"
            >
              {creating
                ? addMode === "upload" ? "Uploading..." : "Downloading..."
                : addMode === "upload" ? "Upload & analyze" : "Add track (server)"}
            </button>

            {addMode === "url" && (
              <>
                <button
                  type="button"
                  onClick={handleExtensionIngest}
                  disabled={
                    creating ||
                    extIngesting ||
                    !url.trim() ||
                    extensionAvailable === false
                  }
                  title={
                    extensionAvailable === false
                      ? "Nova extension not detected — install it to ingest from your browser"
                      : "Pull audio via the Nova Chrome extension (your IP + your YouTube cookies)"
                  }
                  className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-semibold px-5 py-2 rounded-lg transition-colors"
                >
                  {extIngesting ? "Ingesting..." : "Ingest via extension"}
                </button>
                {extensionAvailable === false && (
                  <a
                    href="/admin/extension/install"
                    className="text-xs text-emerald-400 hover:text-emerald-300 underline"
                  >
                    Install Nova extension
                  </a>
                )}
                {extensionAvailable === null && (
                  <span className="text-xs text-zinc-500">Detecting extension…</span>
                )}
              </>
            )}
          </div>

          {/* 3-stage progress UI. NEVER a single spinner — admins need to see
              whether bytes are leaving their machine, landing on our GCS, or
              waiting on Celery. Conflating those into "Processing..." has
              historically caused tab-close panic. The extension flow starts at
              "extracting" (YouTube pull); the direct-file flow starts at
              "uploading" (bytes already on disk locally). */}
          {extProgress && (
            <div className="mt-3 bg-zinc-800/60 border border-zinc-700 rounded-lg p-3 text-xs">
              <ExtensionProgressBar
                progress={extProgress}
                stages={
                  addMode === "upload"
                    ? FILE_UPLOAD_STAGES
                    : EXTENSION_INGEST_STAGES
                }
              />
            </div>
          )}
        </form>
      </div>

      {/* Track list */}
      {/* (Progress widget is defined below the main component to keep
          the page render free of inline JSX-defined components.) */}
      {loading ? (
        <p className="text-zinc-400">Loading…</p>
      ) : error ? (
        <p className="text-red-400">{error}</p>
      ) : tracks.length === 0 ? (
        <p className="text-zinc-500">No tracks yet.</p>
      ) : (
        <div className="space-y-3">
          {tracks.map((t) => (
            <Link
              key={t.id}
              href={`/admin/music/${t.id}`}
              className="flex items-center gap-4 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 rounded-xl p-4 transition-colors"
            >
              {t.thumbnail_url ? (
                <img
                  src={t.thumbnail_url}
                  alt={t.title}
                  className="w-14 h-14 rounded-lg object-cover shrink-0"
                />
              ) : (
                <div className="w-14 h-14 rounded-lg bg-zinc-800 flex items-center justify-center shrink-0">
                  <span className="text-2xl">🎵</span>
                </div>
              )}
              <div className="flex-1 min-w-0">
                <p className="font-semibold truncate">{t.title}</p>
                <p className="text-sm text-zinc-400 truncate">
                  {t.artist || "Unknown artist"} · {t.beat_count} beats
                </p>
              </div>
              <div className="flex flex-col items-end gap-1 shrink-0">
                <span
                  className={`text-xs font-semibold px-2 py-0.5 rounded-full ${
                    STATUS_COLORS[t.analysis_status] ?? STATUS_COLORS.queued
                  }`}
                >
                  {t.analysis_status}
                </span>
                {t.published_at && (
                  <span className="text-xs text-green-500">published</span>
                )}
                {t.archived_at && (
                  <span className="text-xs text-zinc-500">archived</span>
                )}
                <span
                  className={`text-xs font-semibold px-2 py-0.5 rounded-full ${
                    t.generative_matchable
                      ? "bg-emerald-500/15 text-emerald-400"
                      : "bg-zinc-800 text-zinc-500"
                  }`}
                  title={
                    t.generative_matchable
                      ? "Eligible for generative auto-match"
                      : "Not matchable — missing/stale AI labels or sections"
                  }
                >
                  {t.generative_matchable ? "matchable" : "not matchable"}
                </span>
                <span className="text-[10px] text-zinc-500">
                  labels: {t.has_ai_labels ? (t.label_version ?? "?") : "none"} ·
                  sections: {t.section_version ?? "none"}
                </span>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Extension ingest progress UI ─────────────────────────────────────────────
//
// Three discrete stages, each clearly labeled with what's happening on whose
// machine. The plan calls these out as non-negotiable: a single spinner causes
// admins to close the tab during the upload phase (their internet, not ours).

const STAGE_LABELS: Record<IngestProgress["stage"], string> = {
  extension_check: "Checking extension…",
  extracting: "Downloading from YouTube (your browser, your IP)",
  uploading: "Uploading to Nova (your → our GCS bucket)",
  confirming: "Verifying upload on the server",
  analyzing: "Analyzing (beat detect, sections, classifier)",
  ready: "Ready",
  failed: "Failed",
};

// Extension ingest: browser pulls from googlevideo → uploads to our GCS →
// Celery analyses. Three stages so the admin sees which leg is in flight.
const EXTENSION_INGEST_STAGES: IngestProgress["stage"][] = [
  "extracting",
  "uploading",
  "analyzing",
];

// Direct file upload: bytes already on the admin's disk, so there's no
// "extracting" leg. The "confirming" leg (GCS HEAD + ffprobe on the server) is
// surfaced explicitly because it can briefly stall on cold Celery before
// "analyzing" starts.
const FILE_UPLOAD_STAGES: IngestProgress["stage"][] = [
  "uploading",
  "confirming",
  "analyzing",
];

function ExtensionProgressBar({
  progress,
  stages = EXTENSION_INGEST_STAGES,
}: {
  progress: IngestProgress;
  stages?: IngestProgress["stage"][];
}) {
  const currentIdx = stages.indexOf(progress.stage);
  const isFailed = progress.stage === "failed";
  const isReady = progress.stage === "ready";
  return (
    <div>
      <div className="flex items-center gap-2 font-mono">
        {stages.map((stage, i) => {
          const done = isReady || (currentIdx > i && !isFailed);
          const active = currentIdx === i && !isFailed && !isReady;
          const cls = isFailed
            ? "bg-red-700 text-red-200"
            : done
            ? "bg-emerald-700 text-emerald-100"
            : active
            ? "bg-amber-600 text-amber-50 animate-pulse"
            : "bg-zinc-700 text-zinc-400";
          return (
            <div key={stage} className="flex items-center gap-2">
              <span className={`px-2 py-0.5 rounded ${cls}`}>
                {i + 1}. {STAGE_LABELS[stage]}
                {active && progress.percent != null
                  ? ` (${Math.round(progress.percent * 100)}%)`
                  : ""}
              </span>
              {i < stages.length - 1 && (
                <span className="text-zinc-600">→</span>
              )}
            </div>
          );
        })}
      </div>
      {progress.detail && (
        <p className={`mt-2 ${isFailed ? "text-red-300" : "text-zinc-400"}`}>
          {progress.detail}
        </p>
      )}
    </div>
  );
}
