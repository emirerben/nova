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

import { Button } from "@/components/ui/button";
import { Slider } from "@/components/ui/slider";
import { formatTimecode } from "@/lib/timeline/time-format";

/** Zoom factor envelope: 1 = fit-to-width, MAX = deepest zoom (plan §6). */
export const MIN_ZOOM = 1;
export const MAX_ZOOM = 12;

export interface TransportBarProps {
  playing: boolean;
  currentTime: number;
  duration: number;
  onPlayPause: () => void;
  clipTimingDirty?: boolean;
  clipPreviewMode?: "rendered" | "virtual";
  clipPreviewHint?: string;

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

export default function TransportBar({
  playing,
  currentTime,
  duration,
  onPlayPause,
  clipTimingDirty = false,
  clipPreviewMode = "rendered",
  clipPreviewHint,
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
    <div className="flex h-12 items-center gap-2 border-t border-border bg-background px-3">
      {/* ── Left: split / delete ── */}
      <div className="flex flex-1 items-center gap-1">
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Split at playhead"
          title={
            !canSplit
              ? (splitReason ?? "Select a clip or text bar to split")
              : "Split at playhead"
          }
          disabled={!canSplit}
          onClick={onSplit}
        >
          ⿻
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Delete selected"
          title={canDelete ? "Delete selected" : "Select something to delete"}
          disabled={!canDelete}
          onClick={onDelete}
        >
          🗑
        </Button>
      </div>

      {/* ── Center: play/pause + timecode ── */}
      <div className="flex items-center gap-2">
        <Button
          type="button"
          size="icon"
          aria-label={playing ? "Pause" : "Play"}
          aria-pressed={playing}
          onClick={onPlayPause}
        >
          {playing ? "❚❚" : "▶"}
        </Button>
        <span
          className="text-sm tabular-nums text-muted-foreground"
          aria-label="Playback position"
        >
          {formatTimecode(currentTime)}{" "}
          <span className="text-muted-foreground/60">/ {formatTimecode(duration)}</span>
        </span>
        {clipTimingDirty && (
          <span className="hidden max-w-[180px] truncate text-[11px] text-muted-foreground sm:inline">
            {clipPreviewHint ??
              (clipPreviewMode === "virtual"
                ? "Music and transitions preview after Save"
                : "Clip changes preview after Save")}
          </span>
        )}
      </div>

      {/* ── Right: zoom ── */}
      <div className="flex flex-1 items-center justify-end gap-1.5">
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Zoom out"
          title="Zoom out"
          disabled={zoom <= MIN_ZOOM}
          onClick={zoomOut}
        >
          −
        </Button>
        <Slider
          aria-label="Timeline zoom"
          min={MIN_ZOOM}
          max={MAX_ZOOM}
          step={0.1}
          value={[zoom]}
          onValueChange={([next]) => onZoom(next)}
          className="w-28"
        />
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Zoom in"
          title="Zoom in"
          disabled={zoom >= MAX_ZOOM}
          onClick={zoomIn}
        >
          +
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Fit timeline to width"
          title="Fit to width"
          onClick={onFit}
          className="text-[11px]"
        >
          ⬓
        </Button>
      </div>
    </div>
  );
}
