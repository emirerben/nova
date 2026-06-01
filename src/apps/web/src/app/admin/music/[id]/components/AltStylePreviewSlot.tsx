"use client";

import { useCallback, useState } from "react";

import {
  type LyricsPreviewStatus,
  type LyricsStyle,
  adminCreateLyricsPreview,
  adminGetLyricsPreviewStatus,
} from "@/lib/music-api";
import { JobIdChip } from "@/app/admin/_shared/JobIdChip";
import { useJobPoller } from "@/hooks/useJobPoller";

import { formatMSS } from "@/lib/format-time";

import { StatusPill, TERMINAL_STATUSES, resolveMusicJobOutputUrl } from "./musicJobStatus";

/**
 * One preview slot for a non-Line lyric style (Pop-up or Karaoke).
 *
 * Each slot owns its own job ID + poller so the three styles can render
 * independently — kicking off a Karaoke preview while a Pop-up is still
 * rendering does not interrupt the Pop-up. The Line preview keeps its
 * existing `LyricsTimingPanel` workflow (it carries the line-only tuning
 * knobs); this component is the symmetric surface for the two styles that
 * have no per-knob admin controls of their own yet.
 *
 * No `LyricsConfigOverride` is sent: line-only knobs (pre_roll_s,
 * fade_in_ms, …) would be rejected by the backend validator for non-Line
 * styles. Track-level visual config (position, text_color, …) still flows
 * through `effective_lyrics_config` server-side, so the LyricsConfigPanel
 * saves still affect these previews.
 */

interface AltStylePreviewSlotProps {
  trackId: string;
  style: LyricsStyle;
  label: string;
  helper: string;
}

export function AltStylePreviewSlot({
  trackId,
  style,
  label,
  helper,
}: AltStylePreviewSlotProps) {
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const fetchStatus = useCallback(
    (jobId: string) => adminGetLyricsPreviewStatus(trackId, jobId),
    [trackId],
  );
  const poller = useJobPoller<LyricsPreviewStatus>(activeJobId, {
    fetchStatus,
    isTerminal: (d) => TERMINAL_STATUSES.has(d.status),
    activeIntervalMs: 1000,
  });

  async function generate() {
    setSubmitError(null);
    setSubmitting(true);
    try {
      const resp = await adminCreateLyricsPreview(trackId, undefined, style);
      setActiveJobId(resp.job_id);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : "Lyrics preview failed");
    } finally {
      setSubmitting(false);
    }
  }

  const currentJob = poller.data;
  const isPolling = poller.polling;
  const pollError = poller.error;
  const { outputUrl, outputLegacy } = resolveMusicJobOutputUrl(currentJob);

  return (
    <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5 space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-semibold text-zinc-100">{label}</h3>
          <p className="mt-1 text-xs text-zinc-400">{helper}</p>
        </div>
        <button
          type="button"
          onClick={generate}
          disabled={submitting || isPolling}
          className="rounded-lg bg-violet-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-violet-500 disabled:bg-zinc-700 disabled:text-zinc-500"
        >
          {submitting || isPolling ? "Rendering…" : "Preview"}
        </button>
      </div>

      {submitError && (
        <p className="text-xs text-red-400">{submitError}</p>
      )}

      {pollError && (
        <p className="text-xs text-red-400">{pollError}</p>
      )}

      {activeJobId && currentJob && (
        <div className="space-y-2 text-sm">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs text-zinc-500">Job</span>
            <JobIdChip jobId={currentJob.job_id} truncateChars={20} />
            <StatusPill status={currentJob.status} />
            {isPolling && <span className="text-xs text-zinc-500">polling…</span>}
          </div>

          {currentJob.error_detail && (
            <pre className="text-xs text-red-400 bg-red-950/30 rounded p-2 whitespace-pre-wrap break-all">
              {currentJob.error_detail}
            </pre>
          )}

          {outputUrl && (
            <div className="space-y-2">
              {currentJob.preview_start_s !== null &&
                currentJob.preview_duration_s !== null && (
                  <p className="text-xs text-zinc-400 font-mono">
                    {formatMSS(currentJob.preview_start_s)} –{" "}
                    {formatMSS(currentJob.preview_start_s + currentJob.preview_duration_s)}
                  </p>
                )}
              <video
                src={outputUrl}
                controls
                className="w-full max-h-[40vh] rounded-lg bg-black"
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
            <div className="p-2 rounded bg-zinc-800/60 text-xs text-zinc-400">
              Output stored in legacy format. Re-render to view.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
