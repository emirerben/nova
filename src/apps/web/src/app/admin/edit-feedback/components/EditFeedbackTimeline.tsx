"use client";

import { useMemo } from "react";
import type { EditFeedbackTimelineEvent } from "@/lib/admin-edit-feedback-api";
import { fitPxPerSecond, secondsToPx } from "@/lib/timeline/timeline-scale";
import { formatTimecode } from "@/lib/timeline/time-format";

interface EditFeedbackTimelineProps {
  duration: number;
  events: EditFeedbackTimelineEvent[];
  currentTime: number;
  playing: boolean;
  onSeek: (seconds: number) => void;
  onPlayPause: () => void;
}

const MIN_TIMELINE_WIDTH = 320;

/** Read-only, keyboard-operable timeline for the exact final render. */
export function EditFeedbackTimeline({
  duration,
  events,
  currentTime,
  playing,
  onSeek,
  onPlayPause,
}: EditFeedbackTimelineProps) {
  const safeDuration = Math.max(duration || 0, 0.1);
  const width = useMemo(
    () => Math.max(MIN_TIMELINE_WIDTH, Math.round(safeDuration * fitPxPerSecond(720, safeDuration))),
    [safeDuration],
  );
  const playheadLeft = Math.min(100, Math.max(0, (currentTime / safeDuration) * 100));

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      const delta = event.shiftKey ? 5 : 1;
      onSeek(Math.max(0, Math.min(safeDuration, currentTime + (event.key === "ArrowLeft" ? -delta : delta))));
    } else if (event.key === "Home") {
      event.preventDefault();
      onSeek(0);
    } else if (event.key === "End") {
      event.preventDefault();
      onSeek(safeDuration);
    } else if (event.key === " " || event.key === "Spacebar") {
      event.preventDefault();
      onPlayPause();
    }
  };

  return (
    <section aria-label="Final render timeline" className="space-y-2">
      <div className="flex items-center justify-between text-xs text-zinc-500">
        <span>Timeline</span>
        <span aria-live="off" className="tabular-nums">
          {formatTimecode(currentTime)} / {formatTimecode(safeDuration)}
        </span>
      </div>
      <div
        tabIndex={0}
        onKeyDown={handleKeyDown}
        aria-label="Timeline keyboard controls: arrow keys seek, shift plus arrow seeks five seconds, home and end jump, space plays or pauses"
        className="relative min-h-14 overflow-x-auto rounded-md border border-zinc-800 bg-zinc-950 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white"
      >
        <div className="relative h-14" style={{ width }}>
          {events.map((item) => {
            const left = (secondsToPx(Math.max(0, item.start_s), width / safeDuration) / width) * 100;
            const end = item.end_s == null ? item.start_s + 0.25 : item.end_s;
            const eventWidth = Math.max(1.5, ((Math.max(item.start_s, end) - item.start_s) / safeDuration) * 100);
            return (
              <button
                key={item.id}
                type="button"
                className="absolute inset-y-3 min-w-2 rounded-sm bg-zinc-600 text-left hover:bg-zinc-400 focus-visible:z-10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white"
                style={{ left: `${left}%`, width: `${eventWidth}%` }}
                aria-label={`${item.label || item.kind} at ${formatTimecode(item.start_s)}`}
                onClick={() => onSeek(item.start_s)}
              />
            );
          })}
          <div
            aria-hidden="true"
            className="pointer-events-none absolute inset-y-0 w-px bg-white"
            style={{ left: `${playheadLeft}%` }}
          />
        </div>
      </div>
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="min-h-11 rounded-md border border-zinc-700 px-3 text-sm text-white hover:bg-zinc-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white"
          onClick={onPlayPause}
          aria-label={playing ? "Pause preview" : "Play preview"}
        >
          {playing ? "Pause" : "Play"}
        </button>
        <span className="text-xs text-zinc-600">Arrow keys seek · Shift + Arrow 5s · Home / End jump</span>
      </div>
    </section>
  );
}
