"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  type AdminMusicTestJobSummary,
  type LyricsConfig,
  type LyricsConfigOverride,
  type LyricsPreviewStatus,
  type MusicJobStatus,
  type MusicTrackDetail,
  adminCreateLyricsPreview,
  adminCreateMusicTestJob,
  adminGetLyricsPreviewStatus,
  adminGetMusicJobStatus,
  adminListMusicTestJobs,
  adminRerenderMusicJob,
  uploadMusicSlot,
} from "@/lib/music-api";
import { JobIdChip } from "@/app/admin/_shared/JobIdChip";
import { useJobPoller } from "@/hooks/useJobPoller";
import { formatMSS } from "@/lib/format-time";
import { LyricsTimingPanel } from "./LyricsTimingPanel";
import { StatusPill, TERMINAL_STATUSES, resolveMusicJobOutputUrl } from "./musicJobStatus";

type ActiveJobKind = "full" | "lyrics_preview";
type ActiveJobStatus = MusicJobStatus | LyricsPreviewStatus;

interface TestTabProps {
  trackId: string;
  track: MusicTrackDetail;
  // Set by the page-top component when the Config tab's best_start_s /
  // best_end_s form state differs from the persisted track_config. Gates
  // the embedded LyricsTimingPanel's "Preview lyrics only" button so a
  // user who clicked a section band on the Config tab without clicking
  // Save can't fire a preview against stale section bounds (the Beat It
  // bug, job 616d3e53). Defaults to false for callers that never set it.
  sectionBoundsDirty?: boolean;
}

interface UploadedClip {
  fileName: string;
  gcsPath: string;
  kind: "video" | "image";
}

/**
 * Derive the expected number of clips from the track shape.
 *
 * Templated tracks (Love-From-Moon style) have a fixed user_upload slot count
 * encoded in `recipe_cached.slots`. Beat-sync tracks declare a soft
 * required_clips_min/max range in `track_config`. The submit endpoint
 * (_validate_clip_count) enforces these; this helper just surfaces a hint to
 * the admin before they submit.
 */
function describeExpectedClipCount(track: MusicTrackDetail): {
  message: string;
  min: number;
  max: number;
} {
  const cfg = track.track_config ?? null;
  const min = cfg?.required_clips_min ?? 1;
  const max = cfg?.required_clips_max ?? 20;
  if (min === max) return { message: `Expects ${min} clip${min === 1 ? "" : "s"}`, min, max };
  return { message: `Expects ${min}–${max} clips`, min, max };
}

export function TestTab({ trackId, track, sectionBoundsDirty = false }: TestTabProps) {
  const [uploads, setUploads] = useState<UploadedClip[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const [submitError, setSubmitError] = useState<string | null>(null);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [activeJobKind, setActiveJobKind] = useState<ActiveJobKind>("full");
  const [savedLyricsConfig, setSavedLyricsConfig] = useState<Partial<LyricsConfig>>(
    track.track_config?.lyrics_config ?? {},
  );
  const [currentLyricsOverride, setCurrentLyricsOverride] =
    useState<LyricsConfigOverride | null>(null);
  const [prevJobs, setPrevJobs] = useState<AdminMusicTestJobSummary[]>([]);
  const [loadingPrev, setLoadingPrev] = useState(false);

  const expected = useMemo(() => describeExpectedClipCount(track), [track]);

  const fetchStatusForTrack = useCallback(
    (jobId: string) =>
      activeJobKind === "lyrics_preview"
        ? adminGetLyricsPreviewStatus(trackId, jobId)
        : adminGetMusicJobStatus(trackId, jobId),
    [trackId, activeJobKind],
  );
  const poller = useJobPoller<ActiveJobStatus>(activeJobId, {
    fetchStatus: fetchStatusForTrack,
    isTerminal: (d) => TERMINAL_STATUSES.has(d.status),
    activeIntervalMs: 1000,
  });

  useEffect(() => {
    setSavedLyricsConfig(track.track_config?.lyrics_config ?? {});
  }, [track.track_config?.lyrics_config]);

  const refreshPrevJobs = useCallback(async () => {
    setLoadingPrev(true);
    try {
      const jobs = await adminListMusicTestJobs(trackId, 10);
      setPrevJobs(jobs);
    } catch {
      // non-fatal; admins can still kick off new jobs without the history list
    } finally {
      setLoadingPrev(false);
    }
  }, [trackId]);

  useEffect(() => {
    refreshPrevJobs();
  }, [refreshPrevJobs]);

  // When a job terminates, refresh the history list so the new run appears.
  useEffect(() => {
    if (poller.data && TERMINAL_STATUSES.has(poller.data.status)) {
      refreshPrevJobs();
    }
  }, [poller.data, refreshPrevJobs]);

  const trackReady = track.analysis_status === "ready";

  async function handleFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploadError(null);
    setUploading(true);
    // allSettled so a single failed upload doesn't discard the others —
    // partial successes are saved, failures are surfaced in one summary line.
    const settled = await Promise.allSettled(
      Array.from(files).map(async (f) => {
        const r = await uploadMusicSlot(f);
        return { fileName: f.name, gcsPath: r.gcs_path, kind: r.kind };
      }),
    );
    const successes: UploadedClip[] = [];
    const failures: string[] = [];
    settled.forEach((res, i) => {
      if (res.status === "fulfilled") {
        successes.push(res.value);
      } else {
        const fname = files[i]?.name ?? `file ${i + 1}`;
        const msg = res.reason instanceof Error ? res.reason.message : String(res.reason);
        failures.push(`${fname}: ${msg}`);
      }
    });
    if (successes.length > 0) setUploads((prev) => [...prev, ...successes]);
    if (failures.length > 0) setUploadError(failures.join(" · "));
    setUploading(false);
  }

  function removeClip(index: number) {
    setUploads((prev) => prev.filter((_, i) => i !== index));
  }

  function clearClips() {
    setUploads([]);
  }

  async function submitJob(lyricsConfigOverride?: LyricsConfigOverride) {
    setSubmitError(null);
    try {
      const resp = await adminCreateMusicTestJob(
        trackId,
        uploads.map((u) => u.gcsPath),
        lyricsConfigOverride,
      );
      setActiveJobKind("full");
      setActiveJobId(resp.job_id);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : "Submit failed");
    }
  }

  async function previewLyrics(lyricsConfigOverride?: LyricsConfigOverride) {
    setSubmitError(null);
    try {
      const resp = await adminCreateLyricsPreview(trackId, lyricsConfigOverride);
      setActiveJobKind("lyrics_preview");
      setActiveJobId(resp.job_id);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : "Lyrics preview failed");
    }
  }

  async function rerenderFrom(sourceJobId: string) {
    setSubmitError(null);
    try {
      const resp = await adminRerenderMusicJob(
        trackId,
        sourceJobId,
        currentLyricsOverride ?? undefined,
      );
      setActiveJobKind("full");
      setActiveJobId(resp.job_id);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : "Re-render failed");
    }
  }

  const clipCount = uploads.length;
  // Gate the full test render against stale section bounds in addition to
  // the upload-count guards. music_orchestrate.py reads
  // `track_config.best_start_s` / `best_end_s` from the DB at job submit
  // time (see _run_templated_music_job, ~line 463). If the Config tab's
  // form state diverges from the persisted track, the orchestrator would
  // render against the OLD section the admin thought they had replaced —
  // the same trap the lyric-preview button gates against, just with a
  // longer/heavier blast radius (full clip render + audio mix).
  const submitDisabled =
    !trackReady ||
    uploading ||
    clipCount < expected.min ||
    clipCount > expected.max ||
    sectionBoundsDirty;
  // The upload-state hints take priority because they tell the admin what to
  // do RIGHT NOW about clips. Section-dirty appears only when the upload
  // state is otherwise valid, so the admin always sees the most actionable
  // blocker first.
  const fullTestHint =
    clipCount > 0 && clipCount < expected.min
      ? `Need ${expected.min - clipCount} more clip${expected.min - clipCount === 1 ? "" : "s"}`
      : clipCount > expected.max
        ? `Too many clips (${clipCount} > ${expected.max})`
        : sectionBoundsDirty
          ? "Save section bounds on the Config tab first — full render reads the persisted window."
          : null;

  const currentJob = poller.data;
  const isPolling = poller.polling;
  const pollError = poller.error;
  // Shared resolver — see musicJobStatus.ts for the legacy-row defense.
  const { outputUrl, outputLegacy } = resolveMusicJobOutputUrl(currentJob);

  if (track.analysis_status !== "ready") {
    return (
      <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-6">
        <p className="text-sm text-zinc-400">
          This track is not ready yet (status:{" "}
          <span className="font-mono text-amber-400">{track.analysis_status}</span>). Beat
          detection must finish before you can render a test edit. Run Re-analyze on the
          Config tab if it failed.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Upload area */}
      <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-semibold text-sm uppercase tracking-wide text-zinc-400">
            Clips
          </h2>
          <span className="text-xs text-zinc-500">{expected.message}</span>
        </div>

        <label
          className={`block w-full border-2 border-dashed rounded-lg p-6 text-center cursor-pointer transition-colors ${
            uploading
              ? "border-violet-600 bg-violet-950/30"
              : "border-zinc-700 hover:border-zinc-500 hover:bg-zinc-800/40"
          }`}
        >
          <input
            type="file"
            multiple
            accept="video/*,image/*"
            className="hidden"
            disabled={uploading}
            onChange={(e) => {
              handleFiles(e.target.files);
              e.target.value = ""; // allow re-uploading the same file
            }}
          />
          <p className="text-sm text-zinc-300">
            {uploading ? "Uploading…" : "Click or drop video/image clips to upload"}
          </p>
          <p className="text-xs text-zinc-500 mt-1">
            Max 200 MB each · mp4/mov/jpg/png/webp
          </p>
        </label>

        {uploadError && (
          <p className="text-sm text-red-400 mt-3">{uploadError}</p>
        )}

        {uploads.length > 0 && (
          <div className="mt-4">
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs text-zinc-500">
                {uploads.length} clip{uploads.length === 1 ? "" : "s"} ready
              </span>
              <button
                onClick={clearClips}
                className="text-xs text-zinc-500 hover:text-red-400"
              >
                Clear all
              </button>
            </div>
            <ul className="space-y-1">
              {uploads.map((u, i) => (
                <li
                  key={u.gcsPath}
                  className="flex items-center justify-between bg-zinc-800/60 rounded px-3 py-2 text-xs"
                >
                  <span className="font-mono text-zinc-300 truncate flex-1">
                    {i + 1}. {u.fileName}
                  </span>
                  <span className="text-zinc-500 uppercase mx-2">{u.kind}</span>
                  <button
                    onClick={() => removeClip(i)}
                    className="text-zinc-500 hover:text-red-400"
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        {submitError && (
          <p className="text-sm text-red-400 mt-3">{submitError}</p>
        )}
      </div>

      <LyricsTimingPanel
        trackId={trackId}
        savedConfig={savedLyricsConfig}
        fullTestDisabled={submitDisabled}
        fullTestHint={fullTestHint}
        previewDisabled={sectionBoundsDirty}
        previewHint={
          sectionBoundsDirty
            ? "Save section bounds on the Config tab first — preview reads the persisted window."
            : undefined
        }
        onSaved={setSavedLyricsConfig}
        onWorkingChange={setCurrentLyricsOverride}
        onSubmit={(action, override) => {
          if (action === "preview") {
            previewLyrics(override);
          } else {
            submitJob(override);
          }
        }}
      />

      {/* Current job status */}
      {activeJobId && (
        <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5">
          <h2 className="font-semibold text-sm uppercase tracking-wide text-zinc-400 mb-3">
            Render
          </h2>

          {pollError && (
            <p className="text-sm text-red-400 mb-3">{pollError}</p>
          )}

          {currentJob ? (
            <div className="text-sm space-y-2">
              <div className="flex items-center gap-3">
                <span className="text-zinc-500">Job</span>
                <JobIdChip jobId={currentJob.job_id} truncateChars={36} />
                <StatusPill status={currentJob.status} />
                {isPolling && <span className="text-xs text-zinc-500">polling…</span>}
              </div>

              {currentJob.error_detail && (
                <pre className="text-xs text-red-400 bg-red-950/30 rounded p-3 whitespace-pre-wrap break-all">
                  {currentJob.error_detail}
                </pre>
              )}

              {outputUrl && (
                <div className="mt-4 space-y-3">
                  {/* Resolved window the lyric preview rendered. Only present
                      on lyrics_preview jobs — full music jobs assemble clips
                      from t=0 and don't carry preview_start_s / _duration_s.
                      Narrow on activeJobKind so TypeScript permits the field
                      access (MusicJobStatus has no such fields). Without this
                      caption, the auto-anchor change is silent: an admin
                      previewing a song with a 30s instrumental intro would
                      hear the body and assume the wrong track was loaded. */}
                  {activeJobKind === "lyrics_preview" &&
                    "preview_start_s" in currentJob &&
                    currentJob.preview_start_s !== null &&
                    currentJob.preview_duration_s !== null && (
                      <p className="text-xs text-zinc-400 font-mono">
                        Previewing {formatMSS(currentJob.preview_start_s)} –{" "}
                        {formatMSS(currentJob.preview_start_s + currentJob.preview_duration_s)}
                      </p>
                    )}
                  <video
                    src={outputUrl}
                    controls
                    className="w-full max-h-[60vh] rounded-lg bg-black"
                  />
                  <div className="flex gap-2">
                    <a
                      href={outputUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-xs text-violet-400 hover:text-violet-300"
                    >
                      Open in new tab
                    </a>
                    {activeJobKind === "full" && (
                      <button
                        onClick={() => rerenderFrom(currentJob.job_id)}
                        className="ml-auto text-xs font-semibold px-3 py-1.5 rounded-lg bg-zinc-700 hover:bg-zinc-600 text-zinc-100"
                      >
                        Re-render with same clips
                      </button>
                    )}
                  </div>
                </div>
              )}

              {outputLegacy && (
                <div className="mt-4 p-3 rounded bg-zinc-800/60 text-xs text-zinc-400">
                  Output stored in legacy format (pre-URL-fix). Re-render to view.
                </div>
              )}
            </div>
          ) : (
            <p className="text-xs text-zinc-500">Waiting for status…</p>
          )}
        </div>
      )}

      {/* Previous jobs */}
      <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-semibold text-sm uppercase tracking-wide text-zinc-400">
            Previous renders
          </h2>
          <button
            onClick={refreshPrevJobs}
            disabled={loadingPrev}
            className="text-xs text-zinc-500 hover:text-zinc-300"
          >
            {loadingPrev ? "Refreshing…" : "Refresh"}
          </button>
        </div>
        {prevJobs.length === 0 ? (
          <p className="text-xs text-zinc-500">No prior test renders for this track.</p>
        ) : (
          <ul className="space-y-2">
            {prevJobs.map((j) => (
              <li
                key={j.job_id}
                className="flex items-center gap-3 bg-zinc-800/60 rounded px-3 py-2 text-xs"
              >
                <span className="flex-shrink-0 w-24">
                  <JobIdChip jobId={j.job_id} />
                </span>
                <StatusPill status={j.status} />
                <span className="text-zinc-500">{j.clip_count} clips</span>
                <span className="text-zinc-600 flex-1">
                  {new Date(j.created_at).toLocaleString()}
                </span>
                {j.output_url && (
                  <a
                    href={j.output_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-violet-400 hover:text-violet-300"
                  >
                    View
                  </a>
                )}
                <button
                  onClick={() => rerenderFrom(j.job_id)}
                  className="text-zinc-400 hover:text-zinc-100"
                  title="Re-render using this job's clips against the current track config"
                >
                  ↻
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

