"use client";

/**
 * MiniStrip — the pocket editor's 44px scrubbable clip strip (mobile Lane A).
 *
 * One gesture surface (the container) owns pointer interactions via
 * setPointerCapture: a sub-8px tap selects the clip under the finger AND
 * seeks there; travel beyond 8px enters scrub mode (onScrubStart once, then
 * onScrub per move). Each segment is ALSO a real <button> so keyboard and
 * assistive-tech users get a per-clip activation path (click → select at the
 * clip's start). After a handled pointer tap the browser's synthetic click is
 * suppressed so one tap never double-selects.
 */

import { useRef, type PointerEvent as ReactPointerEvent } from "react";
import {
  useEditorPlaybackTime,
  type EditorPlaybackClock,
} from "./editor-playback-clock";

export interface MiniStripSegment {
  id: string;
  startS: number;
  endS: number;
  hasMarks?: boolean;
}

export interface MiniStripProps {
  segments: MiniStripSegment[];
  durationS: number;
  currentTimeS: number;
  playbackClock?: EditorPlaybackClock | null;
  selectedClipId?: string | null;
  marks?: Array<{ id: string; startS: number; endS: number; label: string }>;
  selectedMarkId?: string | null;
  onScrubStart?: () => void;
  onScrub: (seconds: number) => void;
  onSelectClip: (id: string, seconds: number) => void;
  onSelectMark?: (id: string, seconds: number) => void;
}

/** Travel (px) beyond which a pointer gesture becomes a scrub, not a tap. */
export const MINI_STRIP_TAP_SLOP_PX = 8;

/** Map a pointer x to a time in seconds, clamped to [0, durationS]. */
export function miniStripTimeAtX(
  clientX: number,
  rectLeft: number,
  rectWidth: number,
  durationS: number,
): number {
  if (rectWidth <= 0 || durationS <= 0) return 0;
  const t = ((clientX - rectLeft) / rectWidth) * durationS;
  return Math.min(durationS, Math.max(0, t));
}

function segmentAtTime(
  segments: MiniStripSegment[],
  seconds: number,
): MiniStripSegment {
  const hit = segments.find((s) => seconds >= s.startS && seconds < s.endS);
  if (hit) return hit;
  // Off the end (or in a gap past the last segment start): nearest by start.
  const last = segments[segments.length - 1];
  if (seconds >= last.startS) return last;
  return segments[0];
}

interface DragState {
  pointerId: number;
  startX: number;
  startY: number;
  rectLeft: number;
  rectWidth: number;
  scrubbing: boolean;
}

export function MiniStrip({
  segments,
  durationS,
  currentTimeS,
  playbackClock,
  selectedClipId,
  marks = [],
  selectedMarkId,
  onScrubStart,
  onScrub,
  onSelectClip,
  onSelectMark,
}: MiniStripProps): JSX.Element | null {
  const playbackTimeS = useEditorPlaybackTime(playbackClock, currentTimeS);
  const dragRef = useRef<DragState | null>(null);
  /** Swallow the synthetic click that follows a pointer tap we handled. */
  const suppressClickRef = useRef(false);

  if (durationS <= 0 || segments.length === 0) return null;

  const pct = (seconds: number) =>
    `${Math.min(100, Math.max(0, (seconds / durationS) * 100))}%`;

  const handlePointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    suppressClickRef.current = false;
    const rect = e.currentTarget.getBoundingClientRect();
    dragRef.current = {
      pointerId: e.pointerId,
      startX: e.clientX,
      startY: e.clientY,
      rectLeft: rect.left,
      rectWidth: rect.width,
      scrubbing: false,
    };
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };

  const handlePointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || e.pointerId !== drag.pointerId) return;
    const travel = Math.hypot(e.clientX - drag.startX, e.clientY - drag.startY);
    if (!drag.scrubbing) {
      if (travel <= MINI_STRIP_TAP_SLOP_PX) return;
      drag.scrubbing = true;
      onScrubStart?.();
      // The initial crossing seeks too — the finger is already somewhere.
    }
    onScrub(miniStripTimeAtX(e.clientX, drag.rectLeft, drag.rectWidth, durationS));
  };

  const handlePointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    dragRef.current = null;
    if (!drag || e.pointerId !== drag.pointerId) return;
    if (drag.scrubbing) {
      // Scrub already delivered via move events — just swallow the synthetic
      // click so releasing over a segment button doesn't also select it.
      suppressClickRef.current = true;
      window.setTimeout(() => {
        suppressClickRef.current = false;
      }, 0);
      return;
    }
    // Tap: one gesture selects the clip under the finger AND seeks there.
    const seconds = miniStripTimeAtX(
      e.clientX,
      drag.rectLeft,
      drag.rectWidth,
      durationS,
    );
    onSelectClip(segmentAtTime(segments, seconds).id, seconds);
    onScrub(seconds);
    // Guard the double-fire: the browser follows pointerup with a synthetic
    // click on the segment button under the pointer.
    suppressClickRef.current = true;
    e.preventDefault();
    window.setTimeout(() => {
      suppressClickRef.current = false;
    }, 0);
  };

  const handlePointerCancel = () => {
    dragRef.current = null;
  };

  const handleSegmentClick = (segment: MiniStripSegment) => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return; // already handled by the pointer tap path
    }
    onSelectClip(segment.id, segment.startS);
  };

  return (
    <div
      data-testid="pocket-ministrip"
      className="relative h-11 overflow-hidden rounded-lg [touch-action:none]"
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerCancel}
    >
      {segments.map((segment, index) => {
        const selected = selectedClipId === segment.id;
        const last = index === segments.length - 1;
        return (
          <button
            key={segment.id}
            type="button"
            aria-label={`Clip ${index + 1}, ${segment.startS.toFixed(1)}–${segment.endS.toFixed(1)} seconds`}
            onClick={() => handleSegmentClick(segment)}
            style={{
              left: pct(segment.startS),
              width: pct(segment.endS - segment.startS),
            }}
            className={[
              "absolute inset-y-0",
              index % 2 === 0 ? "bg-zinc-300" : "bg-zinc-200",
              last ? "" : "border-r-2 border-white",
              selected ? "outline outline-2 -outline-offset-2 outline-lime-600" : "",
              // Inset focus ring — the container clips outset outlines.
              "focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-lime-500",
            ]
              .filter(Boolean)
              .join(" ")}
          >
            {segment.hasMarks && (
              <span
                aria-hidden="true"
                className="absolute bottom-1 left-1/2 h-1 w-1 -translate-x-1/2 rounded-full bg-lime-600"
              />
            )}
          </button>
        );
      })}
      {marks.map((mark) => (
        <button
          key={mark.id}
          type="button"
          aria-label={`${mark.label}, ${mark.startS.toFixed(1)}–${mark.endS.toFixed(1)} seconds`}
          aria-pressed={selectedMarkId === mark.id}
          onPointerDown={(event) => event.stopPropagation()}
          onClick={(event) => {
            event.stopPropagation();
            onSelectMark?.(mark.id, mark.startS);
          }}
          style={{
            left: pct(mark.startS),
            width: pct(Math.max(1 / 30, mark.endS - mark.startS)),
          }}
          className={[
            "absolute inset-y-0 z-10 min-w-11 bg-transparent",
            "focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-lime-600",
          ].filter(Boolean).join(" ")}
        >
          <span
            aria-hidden="true"
            className={`absolute inset-x-0 bottom-1 h-2 min-w-2 rounded-full bg-lime-600/85 ${
              selectedMarkId === mark.id
                ? "ring-2 ring-white ring-offset-1 ring-offset-lime-700"
                : ""
            }`}
          />
        </button>
      ))}
      <div
        aria-hidden="true"
        data-testid="pocket-ministrip-playhead"
        style={{ left: pct(playbackTimeS) }}
        className="pointer-events-none absolute inset-y-0 w-0.5 bg-lime-600"
      />
    </div>
  );
}
