"use client";

/**
 * TransportBar — the timeline toolbar row above the ruler (plan §6).
 *
 *   left:   split-at-playhead · delete-selected  (enabled per selection;
 *           tooltips; music-bed split disabled with an honest reason)
 *   center: play/pause · `M:SS / M:SS` timecode  (drives the canvas <video>
 *           through the shell's callbacks; reuses formatTimecode)
 *   right:  zoom-out · slider · zoom-in · fit-to-width
 *
 * Pure presentational + view state only — no video ref here; the shell owns
 * the <video> and hands down play/seek callbacks so one element is the source
 * of truth for both the canvas and this bar.
 */

import { formatTimecode } from "@/lib/timeline/time-format";

/** Zoom factor envelope: 1 = fit-to-width, MAX = deepest zoom (plan §6). */
export const MIN_ZOOM = 1;
export const MAX_ZOOM = 12;

export interface TransportBarProps {
  playing: boolean;
  currentTime: number;
  duration: number;
  onPlayPause: () => void;

  /** Split enablement — false disables the button; `reason` fills the tooltip
   * (e.g. the music bed: "music fits the cut automatically"). */
  canSplit: boolean;
  splitReason?: string;
  onSplit: () => void;

  canDelete: boolean;
  onDelete: () => void;

  /** Zoom factor (1 = fit). */
  zoom: number;
  onZoom: (zoom: number) => void;
  onFit: () => void;
}

function iconBtn(enabled: boolean) {
  return [
    "flex h-11 min-w-11 items-center justify-center rounded-lg px-2 text-[13px]",
    "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500",
    enabled
      ? "text-[#3f3f46] hover:bg-zinc-100"
      : "cursor-not-allowed text-[#d4d4d8]",
  ].join(" ");
}

export default function TransportBar({
  playing,
  currentTime,
  duration,
  onPlayPause,
  canSplit,
  splitReason,
  onSplit,
  canDelete,
  onDelete,
  zoom,
  onZoom,
  onFit,
}: TransportBarProps) {
  const zoomIn = () => onZoom(Math.min(MAX_ZOOM, Math.round(zoom * 1.5 * 10) / 10));
  const zoomOut = () => onZoom(Math.max(MIN_ZOOM, Math.round((zoom / 1.5) * 10) / 10));

  return (
    <div className="flex h-12 items-center gap-2 border-b border-zinc-200 bg-white px-3">
      {/* ── Left: split / delete ── */}
      <div className="flex flex-1 items-center gap-1">
        <button
          type="button"
          aria-label="Split at playhead"
          title={
            !canSplit
              ? (splitReason ?? "Select a clip or text bar to split")
              : "Split at playhead"
          }
          disabled={!canSplit}
          onClick={onSplit}
          className={iconBtn(canSplit)}
        >
          ⿻
        </button>
        <button
          type="button"
          aria-label="Delete selected"
          title={canDelete ? "Delete selected" : "Select something to delete"}
          disabled={!canDelete}
          onClick={onDelete}
          className={iconBtn(canDelete)}
        >
          🗑
        </button>
      </div>

      {/* ── Center: play/pause + timecode ── */}
      <div className="flex items-center gap-2">
        <button
          type="button"
          aria-label={playing ? "Pause" : "Play"}
          aria-pressed={playing}
          onClick={onPlayPause}
          className="flex h-11 w-11 items-center justify-center rounded-lg bg-[#0c0c0e] text-[12px] text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
        >
          {playing ? "❚❚" : "▶"}
        </button>
        <span
          className="tabular-nums text-[12px] text-[#3f3f46]"
          aria-label="Playback position"
        >
          {formatTimecode(currentTime)}{" "}
          <span className="text-[#a1a1aa]">/ {formatTimecode(duration)}</span>
        </span>
      </div>

      {/* ── Right: zoom ── */}
      <div className="flex flex-1 items-center justify-end gap-1.5">
        <button
          type="button"
          aria-label="Zoom out"
          title="Zoom out"
          disabled={zoom <= MIN_ZOOM}
          onClick={zoomOut}
          className={iconBtn(zoom > MIN_ZOOM)}
        >
          −
        </button>
        <input
          type="range"
          aria-label="Timeline zoom"
          min={MIN_ZOOM}
          max={MAX_ZOOM}
          step={0.1}
          value={zoom}
          onChange={(e) => onZoom(Number(e.target.value))}
          className="h-11 w-28 cursor-pointer accent-lime-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
        />
        <button
          type="button"
          aria-label="Zoom in"
          title="Zoom in"
          disabled={zoom >= MAX_ZOOM}
          onClick={zoomIn}
          className={iconBtn(zoom < MAX_ZOOM)}
        >
          +
        </button>
        <button
          type="button"
          aria-label="Fit timeline to width"
          title="Fit to width"
          onClick={onFit}
          className={`${iconBtn(true)} text-[11px]`}
        >
          ⬓
        </button>
      </div>
    </div>
  );
}
