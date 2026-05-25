"use client";

import { useCallback, useEffect, useState } from "react";

import {
  type LyricsConfig,
  type LyricsConfigOverride,
  type LyricsPreviewStatus,
  type MusicTrackDetail,
  adminCreateLyricsPreview,
  adminGetLyricsPreviewStatus,
} from "@/lib/music-api";
import LyricsConfigPanel from "@/app/admin/_shared/LyricsConfigPanel";
import { JobIdChip } from "@/app/admin/_shared/JobIdChip";
import { useJobPoller } from "@/hooks/useJobPoller";

import { LyricsTimingPanel } from "./LyricsTimingPanel";
import { StatusPill, TERMINAL_STATUSES, resolveMusicJobOutputUrl } from "./musicJobStatus";

/**
 * Dedicated "Line Lyric Templates" dashboard.
 *
 * Sits next to the Test tab on `/admin/music/[id]?tab=lyrics`. Composes the
 * shipped `LyricsConfigPanel` + `LyricsTimingPanel` and wires the existing
 * `adminCreateLyricsPreview` flow into a focused workflow: see + tune the
 * line-style overlay, preview it on a 20s black background with track audio.
 *
 * Reuse contract:
 *   - lyric extraction, alignment, ASS generation, render — all shipped.
 *   - this component owns no rendering logic; it only composes UI and routes
 *     state through the existing admin API client.
 *
 * Scope note: "line" is the highlighted workflow per product requirement.
 * Karaoke and per-word-pop remain visible inside `LyricsConfigPanel`'s style
 * dropdown so admins can switch styles for production music jobs, but the
 * preview button always renders in line style (the backend forces
 * `style: "line"` on the lyrics-preview job — admin_music.py).
 */

interface LyricsTabProps {
  trackId: string;
  track: MusicTrackDetail;
  onTrackUpdated: (t: MusicTrackDetail) => void;
}

export function LyricsTab({ trackId, track, onTrackUpdated }: LyricsTabProps) {
  const [savedLyricsConfig, setSavedLyricsConfig] = useState<Partial<LyricsConfig>>(
    track.track_config?.lyrics_config ?? {},
  );
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Re-sync the LyricsTimingPanel's "saved" baseline whenever the parent's
  // `track` prop changes its lyrics_config. This fires after LyricsConfigPanel
  // saves: the panel calls onTrackUpdated → page-level setTrack → a fresh
  // track prop flows down here. Without this effect, the timing panel keeps
  // showing the original (now-stale) values and reports "Rendering with
  // unsaved overrides" on every render even though the admin just saved.
  // Mirrors the TestTab.tsx:91-93 pattern.
  useEffect(() => {
    setSavedLyricsConfig(track.track_config?.lyrics_config ?? {});
  }, [track.track_config?.lyrics_config]);

  const fetchStatus = useCallback(
    (jobId: string) => adminGetLyricsPreviewStatus(trackId, jobId),
    [trackId],
  );
  const poller = useJobPoller<LyricsPreviewStatus>(activeJobId, {
    fetchStatus,
    isTerminal: (d) => TERMINAL_STATUSES.has(d.status),
    activeIntervalMs: 1000,
  });

  async function previewLyrics(override?: LyricsConfigOverride) {
    setSubmitError(null);
    try {
      const resp = await adminCreateLyricsPreview(trackId, override);
      setActiveJobId(resp.job_id);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : "Lyrics preview failed");
    }
  }

  const trackReady = track.analysis_status === "ready";
  const lyricsReady = track.lyrics_status === "ready" && (track.lyrics_cached?.lines?.length ?? 0) > 0;
  const currentJob = poller.data;
  const isPolling = poller.polling;
  const pollError = poller.error;

  // Shared resolver — keeps TestTab and LyricsTab from drifting on what counts
  // as a renderable preview URL. Carries the assembly_plan.output_url fallback
  // that legacy lyrics_preview rows depend on.
  const { outputUrl, outputLegacy } = resolveMusicJobOutputUrl(currentJob);

  if (!trackReady) {
    return (
      <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-6">
        <p className="text-sm text-zinc-400">
          This track is not ready yet (status:{" "}
          <span className="font-mono text-amber-400">{track.analysis_status}</span>). Beat
          detection must finish before you can extract lyrics or generate a preview. Run
          Re-analyze on the Config tab if it failed.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header — anchors the workflow as "Line Lyric Templates".
          Uses <h2> not <h1>: the page already renders <h1>{track.title}</h1>
          higher up the tree. Flat bg-zinc-900 + rounded-xl border to match
          every other panel on this dashboard (TestTab, LyricsConfigPanel,
          LyricsTimingPanel). */}
      <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5">
        <h2 className="font-semibold text-zinc-100">Line Lyric Templates</h2>
        <p className="mt-1 text-sm text-zinc-400">
          Tune the line-style lyric overlay independently from full music jobs. Previews
          render against a 20s black background with the track&apos;s own audio so timing
          decisions stay isolated from clip selection.
        </p>
        <p className="mt-2 text-xs text-zinc-500">
          Karaoke and per-word-pop styles remain available in the visual config below for
          production music jobs, but the preview button always renders the{" "}
          <span className="font-mono text-zinc-300">line</span> style.
        </p>
      </div>

      {/* Visual config — same panel used on the Config tab. */}
      <LyricsConfigPanel
        kind="track"
        track={track}
        onTrackUpdated={onTrackUpdated}
      />

      {/* Timing controls + preview action. */}
      {lyricsReady ? (
        <LyricsTimingPanel
          trackId={trackId}
          savedConfig={savedLyricsConfig}
          fullTestDisabled
          fullTestHint="Open the Test tab to render a full music job."
          onSaved={setSavedLyricsConfig}
          onSubmit={(action, override) => {
            if (action === "preview") {
              previewLyrics(override);
            }
            // "full_test" is intentionally inert here — guidance hint points
            // users to the Test tab for full music renders.
          }}
        />
      ) : (
        <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5">
          <p className="text-sm text-zinc-400">
            No cached lyrics yet for this track (status:{" "}
            <span className="font-mono text-amber-400">{track.lyrics_status}</span>).
            Trigger extraction from the visual config above (Re-extract lyrics), then come
            back to tune timing and preview.
          </p>
        </div>
      )}

      {submitError && (
        <div className="rounded-lg border border-red-800 bg-red-950/40 p-3 text-sm text-red-300">
          {submitError}
        </div>
      )}

      {/* Current preview job status + playback. */}
      {activeJobId && (
        <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5">
          <h2 className="font-semibold text-sm uppercase tracking-wide text-zinc-400 mb-3">
            Preview render
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
                  <video
                    src={outputUrl}
                    controls
                    className="w-full max-h-[60vh] rounded-lg bg-black"
                  />
                  <a
                    href={outputUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-violet-400 hover:text-violet-300"
                  >
                    Open in new tab
                  </a>
                </div>
              )}

              {outputLegacy && (
                <div className="mt-4 p-3 rounded bg-zinc-800/60 text-xs text-zinc-400">
                  Output stored in legacy format. Re-render to view.
                </div>
              )}
            </div>
          ) : (
            <p className="text-xs text-zinc-500">Waiting for status…</p>
          )}
        </div>
      )}
    </div>
  );
}

