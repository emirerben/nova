"use client";

/**
 * MiniStrip — Pocket's direct-manipulation thumbnail timeline.
 *
 * The playhead is fixed at the viewport centre. Leading/trailing padding lets
 * the first and last frame reach it, so scrollLeft / pixelsPerSecond is the
 * canonical output time. Body dragging only scrolls/scrubs; source-window slip
 * stays behind the explicit inspector mode. Selected clip edges are separate
 * 44px buttons and reuse the desktop trim math.
 */

import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { Minus, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";
import { formatTimecode } from "@/lib/timeline/time-format";
import { applyClipSourceWindowDrag } from "./editor-bar-drag";
import Filmstrip, { allocateFilmstripSeekBudget } from "./Filmstrip";
import {
  useEditorPlaybackTime,
  type EditorPlaybackClock,
} from "./editor-playback-clock";

export interface MiniStripSegment {
  id: string;
  startS: number;
  endS: number;
  hasMarks?: boolean;
  sourceUrl?: string | null;
  sourceId?: string | number;
  sourceStartS?: number;
  sourceDurationS?: number | null;
  minDurationS?: number;
  label?: string;
  trimDisabledReason?: string | null;
}

export interface MiniStripTrimPatch {
  inS: number;
  durationS: number;
  durationBeats: null;
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
  onTrimStart?: () => void;
  onPreviewTrim?: (id: string, patch: MiniStripTrimPatch) => void;
  onDisabledTap?: (reason: string) => void;
}

export const MINI_STRIP_TAP_SLOP_PX = 8;
export const POCKET_TIMELINE_MIN_ZOOM = 0.1;
export const POCKET_TIMELINE_MAX_ZOOM = 4;
export const POCKET_TIMELINE_BASE_PX_PER_SECOND = 48;

/** Legacy utility retained for callers/tests that map a bounded ruler to time. */
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

export function clampPocketTimelineZoom(value: number): number {
  return Math.min(
    POCKET_TIMELINE_MAX_ZOOM,
    Math.max(POCKET_TIMELINE_MIN_ZOOM, value),
  );
}

export function pocketTimelineTimeAtTap({
  scrollLeft,
  clientX,
  rectLeft,
  viewportWidth,
  pixelsPerSecond,
  durationS,
}: {
  scrollLeft: number;
  clientX: number;
  rectLeft: number;
  viewportWidth: number;
  pixelsPerSecond: number;
  durationS: number;
}): number {
  if (pixelsPerSecond <= 0 || durationS <= 0) return 0;
  const seconds =
    (scrollLeft + clientX - rectLeft - viewportWidth / 2) / pixelsPerSecond;
  return Math.min(durationS, Math.max(0, seconds));
}

function segmentAtTime(
  segments: MiniStripSegment[],
  seconds: number,
): MiniStripSegment {
  // Transitions intentionally overlap canonical entries. Later segments paint
  // above earlier ones, so hit testing must follow the same stacking order.
  for (let index = segments.length - 1; index >= 0; index -= 1) {
    const segment = segments[index];
    if (seconds >= segment.startS && seconds < segment.endS) return segment;
  }
  const last = segments[segments.length - 1];
  if (seconds >= last.startS) return last;
  return segments[0];
}

interface TimelineDragState {
  pointerId: number;
  startX: number;
  startY: number;
  startScrollLeft: number;
  scrubbing: boolean;
}

interface TrimDragState {
  pointerId: number;
  segment: MiniStripSegment;
  handle: "left" | "right";
  startX: number;
  recorded: boolean;
}

interface PinchState {
  startDistance: number;
  startZoom: number;
  anchorTimeS: number;
}

function pointerDistance(points: Array<{ x: number; y: number }>) {
  if (points.length < 2) return 0;
  return Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
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
  onTrimStart,
  onPreviewTrim,
  onDisabledTap,
}: MiniStripProps): JSX.Element | null {
  const playbackTimeS = useEditorPlaybackTime(playbackClock, currentTimeS);
  const viewportRef = useRef<HTMLDivElement>(null);
  const timelineDragRef = useRef<TimelineDragState | null>(null);
  const trimDragRef = useRef<TrimDragState | null>(null);
  const pointersRef = useRef(new Map<number, { x: number; y: number }>());
  const pinchRef = useRef<PinchState | null>(null);
  const suppressClickRef = useRef(false);
  const syncingScrollRef = useRef(false);
  const manualScrollRef = useRef(false);
  const zoomFrameRef = useRef<number | null>(null);
  const scrubFrameRef = useRef<number | null>(null);
  const pendingScrubRef = useRef<number | null>(null);
  const onScrubRef = useRef(onScrub);
  const nativeScrollActiveRef = useRef(false);
  const nativeScrollEndTimerRef = useRef<number | null>(null);
  const pendingZoomRef = useRef<{ zoom: number; anchorTimeS: number } | null>(
    null,
  );
  const [zoom, setZoom] = useState(1);

  useEffect(() => {
    onScrubRef.current = onScrub;
  }, [onScrub]);

  const pixelsPerSecond = POCKET_TIMELINE_BASE_PX_PER_SECOND * zoom;
  const trackWidth = Math.max(1, durationS * pixelsPerSecond);
  const selectedSegment = useMemo(
    () => segments.find((segment) => segment.id === selectedClipId) ?? null,
    [segments, selectedClipId],
  );
  const segmentWidths = useMemo(
    () =>
      segments.map((segment) =>
        Math.max(1, (segment.endS - segment.startS) * pixelsPerSecond),
      ),
    [pixelsPerSecond, segments],
  );
  const seekBudgets = useMemo(
    () => allocateFilmstripSeekBudget(segmentWidths),
    [segmentWidths],
  );

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || timelineDragRef.current || pinchRef.current) return;
    const nextScrollLeft =
      Math.min(durationS, Math.max(0, playbackTimeS)) * pixelsPerSecond;
    if (Math.abs(viewport.scrollLeft - nextScrollLeft) < 0.5) return;
    syncingScrollRef.current = true;
    viewport.scrollLeft = nextScrollLeft;
    const frame = requestAnimationFrame(() => {
      syncingScrollRef.current = false;
    });
    return () => cancelAnimationFrame(frame);
  }, [durationS, pixelsPerSecond, playbackTimeS]);

  useEffect(
    () => () => {
      if (zoomFrameRef.current != null) {
        cancelAnimationFrame(zoomFrameRef.current);
      }
      if (scrubFrameRef.current != null) {
        cancelAnimationFrame(scrubFrameRef.current);
      }
      if (nativeScrollEndTimerRef.current != null) {
        window.clearTimeout(nativeScrollEndTimerRef.current);
      }
    },
    [],
  );

  useLayoutEffect(() => {
    const pending = pendingZoomRef.current;
    const viewport = viewportRef.current;
    if (!pending || !viewport || Math.abs(pending.zoom - zoom) >= 1e-6) {
      return;
    }
    pendingZoomRef.current = null;
    syncingScrollRef.current = true;
    viewport.scrollLeft =
      Math.min(durationS, Math.max(0, pending.anchorTimeS)) * pixelsPerSecond;
    const frame = requestAnimationFrame(() => {
      syncingScrollRef.current = false;
    });
    return () => cancelAnimationFrame(frame);
  }, [durationS, pixelsPerSecond, zoom]);

  if (durationS <= 0 || segments.length === 0) return null;

  const changeZoom = (nextZoom: number, anchorTimeS = playbackTimeS) => {
    const clamped = clampPocketTimelineZoom(nextZoom);
    pendingZoomRef.current = { zoom: clamped, anchorTimeS };
    if (zoomFrameRef.current != null) return;
    zoomFrameRef.current = requestAnimationFrame(() => {
      zoomFrameRef.current = null;
      const pending = pendingZoomRef.current;
      if (!pending) return;
      setZoom((current) =>
        Math.abs(current - pending.zoom) < 1e-6 ? current : pending.zoom,
      );
      if (Math.abs(zoom - pending.zoom) < 1e-6) {
        const viewport = viewportRef.current;
        pendingZoomRef.current = null;
        if (!viewport) return;
        syncingScrollRef.current = true;
        viewport.scrollLeft =
          Math.min(durationS, Math.max(0, pending.anchorTimeS)) *
          pixelsPerSecond;
        requestAnimationFrame(() => {
          syncingScrollRef.current = false;
        });
      }
    });
  };

  const scrubToScrollPosition = (scrollLeft: number, flush = false) => {
    pendingScrubRef.current = Math.min(
      durationS,
      Math.max(0, scrollLeft / pixelsPerSecond),
    );
    if (flush) {
      if (scrubFrameRef.current != null) {
        cancelAnimationFrame(scrubFrameRef.current);
        scrubFrameRef.current = null;
      }
      const pending = pendingScrubRef.current;
      pendingScrubRef.current = null;
      if (pending != null) onScrubRef.current(pending);
      return;
    }
    if (scrubFrameRef.current != null) return;
    scrubFrameRef.current = requestAnimationFrame(() => {
      scrubFrameRef.current = null;
      const pending = pendingScrubRef.current;
      pendingScrubRef.current = null;
      if (pending != null) onScrubRef.current(pending);
    });
  };

  const cancelScheduledScrub = () => {
    if (scrubFrameRef.current != null) {
      cancelAnimationFrame(scrubFrameRef.current);
      scrubFrameRef.current = null;
    }
    pendingScrubRef.current = null;
  };

  const handleTimelinePointerDown = (
    event: ReactPointerEvent<HTMLDivElement>,
  ) => {
    pointersRef.current.set(event.pointerId, {
      x: event.clientX,
      y: event.clientY,
    });
    event.currentTarget.setPointerCapture?.(event.pointerId);
    if (pointersRef.current.size === 2) {
      const distance = pointerDistance([...pointersRef.current.values()]);
      pinchRef.current = {
        startDistance: Math.max(1, distance),
        startZoom: zoom,
        anchorTimeS: playbackTimeS,
      };
      timelineDragRef.current = null;
      suppressClickRef.current = true;
      return;
    }
    suppressClickRef.current = false;
    timelineDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startScrollLeft: event.currentTarget.scrollLeft,
      scrubbing: false,
    };
  };

  const handleTimelinePointerMove = (
    event: ReactPointerEvent<HTMLDivElement>,
  ) => {
    if (!pointersRef.current.has(event.pointerId)) return;
    pointersRef.current.set(event.pointerId, {
      x: event.clientX,
      y: event.clientY,
    });
    if (pinchRef.current && pointersRef.current.size >= 2) {
      const distance = pointerDistance([...pointersRef.current.values()]);
      changeZoom(
        pinchRef.current.startZoom *
          (distance / pinchRef.current.startDistance),
        pinchRef.current.anchorTimeS,
      );
      return;
    }
    const drag = timelineDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const travel = Math.hypot(
      event.clientX - drag.startX,
      event.clientY - drag.startY,
    );
    if (!drag.scrubbing) {
      if (travel <= MINI_STRIP_TAP_SLOP_PX) return;
      drag.scrubbing = true;
      onScrubStart?.();
    }
    const viewport = event.currentTarget;
    manualScrollRef.current = true;
    viewport.scrollLeft = Math.min(
      trackWidth,
      Math.max(0, drag.startScrollLeft - (event.clientX - drag.startX)),
    );
    scrubToScrollPosition(viewport.scrollLeft);
  };

  const finishTimelinePointer = (
    event: ReactPointerEvent<HTMLDivElement>,
    cancelled = false,
  ) => {
    const wasPinching = pinchRef.current !== null;
    pointersRef.current.delete(event.pointerId);
    if (pointersRef.current.size < 2) pinchRef.current = null;
    const drag = timelineDragRef.current;
    timelineDragRef.current = null;
    if (wasPinching) {
      cancelScheduledScrub();
      manualScrollRef.current = false;
      suppressClickRef.current = true;
      window.setTimeout(() => {
        suppressClickRef.current = false;
      }, 0);
      return;
    }
    if (cancelled) {
      cancelScheduledScrub();
      manualScrollRef.current = false;
      suppressClickRef.current = false;
      return;
    }
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (drag.scrubbing) {
      scrubToScrollPosition(event.currentTarget.scrollLeft, true);
      suppressClickRef.current = true;
      window.setTimeout(() => {
        suppressClickRef.current = false;
        manualScrollRef.current = false;
      }, 0);
      return;
    }
    const rect = event.currentTarget.getBoundingClientRect();
    const measuredWidth = rect.width > 0 ? rect.width : 1;
    const seconds = pocketTimelineTimeAtTap({
      scrollLeft: event.currentTarget.scrollLeft,
      clientX: event.clientX,
      rectLeft: rect.left,
      viewportWidth: measuredWidth,
      pixelsPerSecond,
      durationS,
    });
    const segment = segmentAtTime(segments, seconds);
    onSelectClip(segment.id, seconds);
    onScrub(seconds);
    suppressClickRef.current = true;
    event.preventDefault();
    window.setTimeout(() => {
      suppressClickRef.current = false;
    }, 0);
  };

  const previewTrim = (
    segment: MiniStripSegment,
    handle: "left" | "right",
    deltaS: number,
    recordHistory: () => void,
  ) => {
    if (!onPreviewTrim) return;
    if (segment.trimDisabledReason) {
      onDisabledTap?.(segment.trimDisabledReason);
      return;
    }
    const currentInS = segment.sourceStartS ?? 0;
    const currentDurationS = Math.max(0, segment.endS - segment.startS);
    const next = applyClipSourceWindowDrag({
      slot: { inS: currentInS, durationS: currentDurationS },
      handle,
      deltaS,
      sourceDurationS: segment.sourceDurationS ?? null,
      minDurationS: segment.minDurationS ?? 0.1,
    });
    const nextDurationS = next.durationS ?? currentDurationS;
    if (
      Math.abs(next.inS - currentInS) < 1e-6 &&
      Math.abs(nextDurationS - currentDurationS) < 1e-6
    ) {
      return;
    }
    recordHistory();
    onPreviewTrim(segment.id, {
      inS: next.inS,
      durationS: nextDurationS,
      durationBeats: null,
    });
  };

  const handleTrimPointerDown = (
    event: ReactPointerEvent<HTMLButtonElement>,
    segment: MiniStripSegment,
    handle: "left" | "right",
  ) => {
    event.stopPropagation();
    if (segment.trimDisabledReason) {
      onDisabledTap?.(segment.trimDisabledReason);
      return;
    }
    event.currentTarget.setPointerCapture?.(event.pointerId);
    trimDragRef.current = {
      pointerId: event.pointerId,
      segment,
      handle,
      startX: event.clientX,
      recorded: false,
    };
  };

  const handleTrimPointerMove = (
    event: ReactPointerEvent<HTMLButtonElement>,
  ) => {
    event.stopPropagation();
    const drag = trimDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    previewTrim(
      drag.segment,
      drag.handle,
      (event.clientX - drag.startX) / pixelsPerSecond,
      () => {
        if (drag.recorded) return;
        drag.recorded = true;
        onTrimStart?.();
      },
    );
  };

  const handleTrimKey = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    segment: MiniStripSegment,
    handle: "left" | "right",
  ) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const direction = event.key === "ArrowRight" ? 1 : -1;
    previewTrim(segment, handle, direction * 0.1, () => onTrimStart?.());
  };

  return (
    <section
      data-testid="pocket-ministrip"
      aria-label="Video timeline"
      className="bg-background"
    >
      <div className="flex min-h-11 items-center gap-2 border-y border-border px-3">
        <div className="min-w-0 flex-1" aria-live="polite">
          {selectedSegment ? (
            <p className="truncate text-xs tabular-nums text-foreground">
              <span className="sr-only">Selected clip </span>
              <span className="text-muted-foreground">
                In {formatTimecode(selectedSegment.sourceStartS ?? 0)} · Out{" "}
                {formatTimecode(
                  (selectedSegment.sourceStartS ?? 0) +
                    selectedSegment.endS -
                    selectedSegment.startS,
                )}{" "}
                · {formatTimecode(selectedSegment.endS - selectedSegment.startS)}
              </span>
            </p>
          ) : (
            <p className="text-xs text-muted-foreground">
              Drag the timeline to scrub
            </p>
          )}
        </div>
        <div
          role="toolbar"
          aria-label="Timeline zoom"
          className="flex shrink-0 items-center gap-1"
        >
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label="Zoom timeline out"
            onClick={() => changeZoom(zoom / 1.5)}
            className="size-11"
          >
            <Minus className="h-4 w-4" aria-hidden="true" />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            aria-label="Fit timeline"
            onClick={() => {
              const measuredWidth =
                viewportRef.current?.getBoundingClientRect().width ?? 0;
              const fit =
                durationS > 0 && measuredWidth > 0
                  ? measuredWidth /
                    Math.max(
                      1,
                      durationS * POCKET_TIMELINE_BASE_PX_PER_SECOND,
                    )
                  : 1;
              changeZoom(fit);
            }}
            className="h-11 min-w-11 px-2 text-xs"
          >
            Fit
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label="Zoom timeline in"
            onClick={() => changeZoom(zoom * 1.5)}
            className="size-11"
          >
            <Plus className="h-4 w-4" aria-hidden="true" />
          </Button>
        </div>
      </div>

      <div className="relative h-20 overflow-hidden bg-muted/30">
        <div
          ref={viewportRef}
          data-testid="pocket-timeline-viewport"
          className="scrollbar-none h-full overflow-x-auto overflow-y-hidden overscroll-x-contain select-none [touch-action:none] [will-change:scroll-position]"
          onScroll={(event) => {
            if (syncingScrollRef.current || manualScrollRef.current) return;
            if (!nativeScrollActiveRef.current) {
              nativeScrollActiveRef.current = true;
              onScrubStart?.();
            }
            if (nativeScrollEndTimerRef.current != null) {
              window.clearTimeout(nativeScrollEndTimerRef.current);
            }
            nativeScrollEndTimerRef.current = window.setTimeout(() => {
              nativeScrollActiveRef.current = false;
              nativeScrollEndTimerRef.current = null;
            }, 120);
            scrubToScrollPosition(event.currentTarget.scrollLeft);
          }}
          onPointerDown={handleTimelinePointerDown}
          onPointerMove={handleTimelinePointerMove}
          onPointerUp={(event) => finishTimelinePointer(event)}
          onPointerCancel={(event) => finishTimelinePointer(event, true)}
        >
          <div
            className="relative h-full [contain:layout_paint]"
            style={{ width: `calc(${trackWidth}px + 100vw)` }}
          >
            {segments.map((segment, index) => {
              const selected = selectedClipId === segment.id;
              const widthPx = segmentWidths[index];
              const duration = segment.endS - segment.startS;
              return (
                <div
                  key={segment.id}
                  data-testid={`pocket-timeline-clip-${segment.id}`}
                  className="absolute top-2 h-16"
                  style={{
                    left: `calc(50vw + ${segment.startS * pixelsPerSecond}px)`,
                    width: widthPx,
                  }}
                >
                  <div
                    className={cn(
                      "absolute inset-0 overflow-hidden rounded-md border bg-muted",
                      selected
                        ? "border-2 border-lime-600 ring-1 ring-foreground/70"
                        : "border-border",
                    )}
                  >
                    <Filmstrip
                      src={segment.sourceUrl ?? null}
                      clipId={segment.id}
                      sourceId={segment.sourceId ?? segment.id}
                      sourceStartS={segment.sourceStartS ?? 0}
                      durationS={duration}
                      sourceDurationS={segment.sourceDurationS ?? null}
                      widthPx={widthPx}
                      maxSeekCount={seekBudgets[index] ?? 0}
                      label={segment.label ?? formatTimecode(duration)}
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      aria-label={`Clip ${index + 1}, ${segment.startS.toFixed(1)}–${segment.endS.toFixed(1)} seconds`}
                      aria-pressed={selected}
                      onClick={() => {
                        if (suppressClickRef.current) return;
                        onSelectClip(segment.id, segment.startS);
                      }}
                      className="absolute inset-0 h-full w-full rounded-none bg-transparent p-0 hover:bg-foreground/5 focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-lime-500 focus-visible:ring-0"
                    >
                      <span className="sr-only">Select clip {index + 1}</span>
                    </Button>
                    {segment.hasMarks && (
                      <span
                        aria-hidden="true"
                        className="absolute bottom-1 left-1/2 z-10 h-1.5 w-1.5 -translate-x-1/2 rounded-full bg-lime-600 ring-1 ring-white"
                      />
                    )}
                    {selected && (
                      <span className="pointer-events-none absolute bottom-1 right-1 z-10 rounded-sm bg-background/90 px-1 text-[10px] font-medium tabular-nums text-foreground">
                        {formatTimecode(duration)}
                      </span>
                    )}
                  </div>

                  {selected &&
                    onPreviewTrim &&
                    (["left", "right"] as const).map((handle) => (
                      <Button
                        key={handle}
                        type="button"
                        variant="ghost"
                        data-pocket-trim-handle={handle}
                        aria-label={`${
                          handle === "left"
                            ? "Trim clip start"
                            : "Trim clip end"
                        }, ${formatTimecode(duration)}`}
                        aria-disabled={
                          segment.trimDisabledReason ? true : undefined
                        }
                        onPointerDown={(event) =>
                          handleTrimPointerDown(event, segment, handle)
                        }
                        onPointerMove={handleTrimPointerMove}
                        onPointerUp={(event) => {
                          event.stopPropagation();
                          trimDragRef.current = null;
                        }}
                        onPointerCancel={(event) => {
                          event.stopPropagation();
                          trimDragRef.current = null;
                        }}
                        onKeyDown={(event) =>
                          handleTrimKey(event, segment, handle)
                        }
                        className={cn(
                          "absolute inset-y-0 z-30 h-16 w-11 rounded-none bg-transparent p-0 hover:bg-lime-100/35 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 focus-visible:ring-0",
                          handle === "left"
                            ? "-left-[22px]"
                            : "-right-[22px]",
                          segment.trimDisabledReason
                            ? "opacity-50"
                            : "cursor-ew-resize",
                        )}
                      >
                        <span
                          aria-hidden="true"
                          className={cn(
                            "h-10 w-[3px] rounded-full bg-lime-600 shadow-sm",
                            handle === "left"
                              ? "mr-auto ml-[20px]"
                              : "ml-auto mr-[20px]",
                          )}
                        />
                      </Button>
                    ))}
                </div>
              );
            })}

            {marks.map((mark) => (
              <Button
                key={mark.id}
                type="button"
                variant="ghost"
                aria-label={`${mark.label}, ${mark.startS.toFixed(1)}–${mark.endS.toFixed(1)} seconds`}
                aria-pressed={selectedMarkId === mark.id}
                onPointerDown={(event) => event.stopPropagation()}
                onClick={(event) => {
                  event.stopPropagation();
                  onSelectMark?.(mark.id, mark.startS);
                }}
                style={{
                  left: `calc(50vw + ${mark.startS * pixelsPerSecond}px)`,
                  width: Math.max(
                    44,
                    (mark.endS - mark.startS) * pixelsPerSecond,
                  ),
                }}
                className="absolute bottom-1 z-20 h-11 min-w-11 rounded-none bg-transparent p-0 hover:bg-transparent focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 focus-visible:ring-0"
              >
                <span
                  aria-hidden="true"
                  className={cn(
                    "absolute inset-x-0 bottom-1 h-2 min-w-2 rounded-full bg-lime-600/85",
                    selectedMarkId === mark.id && "ring-2 ring-background",
                  )}
                />
              </Button>
            ))}
          </div>
        </div>

        <div
          aria-hidden="true"
          data-testid="pocket-ministrip-playhead"
          className="pointer-events-none absolute inset-y-0 left-1/2 z-40 w-0.5 -translate-x-1/2 bg-foreground"
        >
          <span className="absolute -left-[4px] top-0 h-2.5 w-2.5 rounded-b-sm bg-foreground" />
        </div>
      </div>
    </section>
  );
}
