"use client";

/**
 * MiniStrip — Pocket's direct-manipulation thumbnail timeline.
 *
 * The playhead is fixed near the viewport's left edge. A 22px leading inset
 * keeps the first clip's 44px trim target fully reachable, while trailing
 * padding lets the final frame reach the playhead. scrollLeft /
 * pixelsPerSecond remains the canonical output time. Body dragging only
 * scrolls/scrubs; source-window slip stays behind the explicit inspector mode.
 */

import {
  useCallback,
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
import Filmstrip, { allocateFilmstripDensityBudget } from "./Filmstrip";
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

export type MiniStripLaneItemKind =
  | "text"
  | "visual"
  | "motion"
  | "carousel"
  | "sfx"
  | "music"
  | "overlay"
  | "camera";

export interface MiniStripLaneItem {
  id: string;
  kind: MiniStripLaneItemKind;
  startS: number;
  endS: number;
  label: string;
  resizable?: boolean;
  resizeDisabledReason?: string | null;
}

export interface MiniStripLaneTimingPatch {
  startS: number;
  endS: number;
}

export interface MiniStripLane {
  id: string;
  label: string;
  items: MiniStripLaneItem[];
}

export interface MiniStripProps {
  segments: MiniStripSegment[];
  durationS: number;
  currentTimeS: number;
  playbackClock?: EditorPlaybackClock | null;
  selectedClipId?: string | null;
  marks?: Array<{ id: string; startS: number; endS: number; label: string }>;
  selectedMarkId?: string | null;
  lanes?: MiniStripLane[];
  selectedLaneItem?: { kind: MiniStripLaneItemKind; id: string } | null;
  onScrubStart?: () => void;
  onScrub: (seconds: number) => void;
  onSelectClip: (id: string, seconds: number) => void;
  onSelectMark?: (id: string, seconds: number) => void;
  onSelectLaneItem?: (item: MiniStripLaneItem, seconds: number) => void;
  onLaneResizeStart?: (item: MiniStripLaneItem) => void;
  onPreviewLaneTiming?: (
    item: MiniStripLaneItem,
    patch: MiniStripLaneTimingPatch,
    handle: "left" | "right",
  ) => void;
  onTrimStart?: () => void;
  onPreviewTrim?: (id: string, patch: MiniStripTrimPatch) => void;
  onDisabledTap?: (reason: string) => void;
}

export const MINI_STRIP_TAP_SLOP_PX = 8;
export const POCKET_TIMELINE_MIN_ZOOM = 0.1;
export const POCKET_TIMELINE_MAX_ZOOM = 4;
export const POCKET_TIMELINE_BASE_PX_PER_SECOND = 48;
export const POCKET_TIMELINE_PLAYHEAD_INSET_PX = 22;
export const POCKET_TIMELINE_VIDEO_HEIGHT_PX = 80;
export const POCKET_TIMELINE_LANE_HEIGHT_PX = 48;
export const POCKET_LANE_AUTO_PAN_PX_PER_FRAME = 4;
export const POCKET_LANE_AUTO_PAN_EDGE_EPSILON_PX = 1;
// Keep one compact lane visible under the filmstrip. Additional desktop lanes
// remain vertically scrollable so they do not collapse the phone preview when
// an inline tool panel is also open.
export const POCKET_TIMELINE_MAX_VIEWPORT_HEIGHT_PX = 128;

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

export function resizeMiniStripLaneRange({
  item,
  handle,
  deltaS,
  durationS,
  minimumDurationS = 0.1,
}: {
  item: Pick<MiniStripLaneItem, "startS" | "endS">;
  handle: "left" | "right";
  deltaS: number;
  durationS: number;
  minimumDurationS?: number;
}): MiniStripLaneTimingPatch {
  const roundStep = (seconds: number) => Math.round(seconds * 10) / 10;
  if (handle === "left") {
    const maximumStartS =
      Math.floor((item.endS - minimumDurationS) * 10) / 10;
    return {
      startS: Math.max(
        0,
        Math.min(maximumStartS, roundStep(item.startS + deltaS)),
      ),
      endS: item.endS,
    };
  }
  const minimumEndS =
    Math.ceil((item.startS + minimumDurationS) * 10) / 10;
  return {
    startS: item.startS,
    endS: Math.min(
      durationS,
      Math.max(minimumEndS, roundStep(item.endS + deltaS)),
    ),
  };
}

export function pocketLaneAutoPanDirection({
  clientX,
  viewportLeft,
  viewportRight,
}: {
  clientX: number;
  viewportLeft: number;
  viewportRight: number;
}): -1 | 0 | 1 {
  if (clientX <= viewportLeft + POCKET_LANE_AUTO_PAN_EDGE_EPSILON_PX) return -1;
  if (clientX >= viewportRight - POCKET_LANE_AUTO_PAN_EDGE_EPSILON_PX) return 1;
  return 0;
}

export function pocketTimelineTimeAtTap({
  scrollLeft,
  clientX,
  rectLeft,
  pixelsPerSecond,
  durationS,
}: {
  scrollLeft: number;
  clientX: number;
  rectLeft: number;
  pixelsPerSecond: number;
  durationS: number;
}): number {
  if (pixelsPerSecond <= 0 || durationS <= 0) return 0;
  const seconds =
    (scrollLeft +
      clientX -
      rectLeft -
      POCKET_TIMELINE_PLAYHEAD_INSET_PX) /
    pixelsPerSecond;
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
  startScrollTop: number;
  scrubbing: boolean;
  verticalScrolling: boolean;
  startedLaneItem: MiniStripLaneItem | null;
}

interface TrimDragState {
  pointerId: number;
  segment: MiniStripSegment;
  handle: "left" | "right";
  startX: number;
  recorded: boolean;
}

interface LaneResizeDragState {
  pointerId: number;
  item: MiniStripLaneItem;
  handle: "left" | "right";
  startX: number;
  startScrollLeft: number;
  latestClientX: number;
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
  lanes = [],
  selectedLaneItem,
  onScrubStart,
  onScrub,
  onSelectClip,
  onSelectMark,
  onSelectLaneItem,
  onLaneResizeStart,
  onPreviewLaneTiming,
  onTrimStart,
  onPreviewTrim,
  onDisabledTap,
}: MiniStripProps): JSX.Element | null {
  const playbackTimeS = useEditorPlaybackTime(playbackClock, currentTimeS);
  const viewportRef = useRef<HTMLDivElement>(null);
  const timelineDragRef = useRef<TimelineDragState | null>(null);
  const trimDragRef = useRef<TrimDragState | null>(null);
  const laneResizeDragRef = useRef<LaneResizeDragState | null>(null);
  const laneResizeAutoPanFrameRef = useRef<number | null>(null);
  const pointersRef = useRef(new Map<number, { x: number; y: number }>());
  const pinchRef = useRef<PinchState | null>(null);
  const suppressClickRef = useRef(false);
  const syncingScrollRef = useRef(false);
  const programmaticScrollTargetRef = useRef<number | null>(null);
  const syncScrollEndTimerRef = useRef<number | null>(null);
  const manualScrollRef = useRef(false);
  const zoomFrameRef = useRef<number | null>(null);
  const scrubFrameRef = useRef<number | null>(null);
  const pendingScrubRef = useRef<number | null>(null);
  const onScrubRef = useRef(onScrub);
  const nativeScrollActiveRef = useRef(false);
  const nativeScrollEndTimerRef = useRef<number | null>(null);
  const lastObservedScrollLeftRef = useRef(0);
  const pendingZoomRef = useRef<{ zoom: number; anchorTimeS: number } | null>(
    null,
  );
  const latestSegmentsRef = useRef(segments);
  latestSegmentsRef.current = segments;
  const [zoom, setZoom] = useState(1);
  const [filmstripSegments, setFilmstripSegments] = useState(segments);

  const markProgrammaticScroll = useCallback(() => {
    syncingScrollRef.current = true;
    if (syncScrollEndTimerRef.current != null) {
      window.clearTimeout(syncScrollEndTimerRef.current);
    }
    syncScrollEndTimerRef.current = window.setTimeout(() => {
      syncingScrollRef.current = false;
      programmaticScrollTargetRef.current = null;
      syncScrollEndTimerRef.current = null;
    }, 80);
  }, []);

  useEffect(() => {
    onScrubRef.current = onScrub;
  }, [onScrub]);

  const pixelsPerSecond = POCKET_TIMELINE_BASE_PX_PER_SECOND * zoom;
  const trackWidth = Math.max(1, durationS * pixelsPerSecond);
  const timelineContentHeight =
    POCKET_TIMELINE_VIDEO_HEIGHT_PX +
    lanes.length * POCKET_TIMELINE_LANE_HEIGHT_PX;
  const timelineViewportHeight = Math.min(
    timelineContentHeight,
    POCKET_TIMELINE_MAX_VIEWPORT_HEIGHT_PX,
  );
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
  const filmstripSegmentById = useMemo(
    () => new Map(filmstripSegments.map((segment) => [segment.id, segment])),
    [filmstripSegments],
  );
  const filmstripWidths = useMemo(
    () =>
      segments.map((segment) => {
        const stable = filmstripSegmentById.get(segment.id) ?? segment;
        return Math.max(1, (stable.endS - stable.startS) * pixelsPerSecond);
      }),
    [filmstripSegmentById, pixelsPerSecond, segments],
  );
  const seekBudgets = useMemo(
    () => allocateFilmstripDensityBudget(filmstripWidths, zoom),
    [filmstripWidths, zoom],
  );

  useEffect(() => {
    if (trimDragRef.current) return;
    setFilmstripSegments(segments);
  }, [segments]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (
      !viewport ||
      timelineDragRef.current ||
      pinchRef.current ||
      laneResizeDragRef.current
    ) {
      return;
    }
    const nextScrollLeft =
      Math.min(durationS, Math.max(0, playbackTimeS)) * pixelsPerSecond;
    if (Math.abs(viewport.scrollLeft - nextScrollLeft) < 0.5) return;
    markProgrammaticScroll();
    programmaticScrollTargetRef.current = nextScrollLeft;
    viewport.scrollLeft = nextScrollLeft;
    // The 80ms quiet window stays active across decoded playback frames. A
    // real pointerdown clears it synchronously before user scrolling begins.
  }, [durationS, markProgrammaticScroll, pixelsPerSecond, playbackTimeS]);

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
      if (syncScrollEndTimerRef.current != null) {
        window.clearTimeout(syncScrollEndTimerRef.current);
      }
      if (laneResizeAutoPanFrameRef.current != null) {
        cancelAnimationFrame(laneResizeAutoPanFrameRef.current);
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
    markProgrammaticScroll();
    const nextScrollLeft =
      Math.min(durationS, Math.max(0, pending.anchorTimeS)) * pixelsPerSecond;
    programmaticScrollTargetRef.current = nextScrollLeft;
    viewport.scrollLeft = nextScrollLeft;
  }, [durationS, markProgrammaticScroll, pixelsPerSecond, zoom]);

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
        markProgrammaticScroll();
        const nextScrollLeft =
          Math.min(durationS, Math.max(0, pending.anchorTimeS)) *
          pixelsPerSecond;
        programmaticScrollTargetRef.current = nextScrollLeft;
        viewport.scrollLeft = nextScrollLeft;
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
    if (syncScrollEndTimerRef.current != null) {
      window.clearTimeout(syncScrollEndTimerRef.current);
      syncScrollEndTimerRef.current = null;
    }
    syncingScrollRef.current = false;
    programmaticScrollTargetRef.current = null;
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
    const laneTarget =
      event.target instanceof Element
        ? event.target.closest<HTMLElement>("[data-pocket-lane-id]")
        : null;
    const startedLaneItem = laneTarget
      ? (lanes
          .flatMap((lane) => lane.items)
          .find(
            (item) =>
              item.id === laneTarget.dataset.pocketLaneId &&
              item.kind === laneTarget.dataset.pocketLaneKind,
          ) ?? null)
      : null;
    timelineDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startScrollLeft: event.currentTarget.scrollLeft,
      startScrollTop: event.currentTarget.scrollTop,
      scrubbing: false,
      verticalScrolling: false,
      startedLaneItem,
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
    const deltaX = event.clientX - drag.startX;
    const deltaY = event.clientY - drag.startY;
    const travel = Math.hypot(deltaX, deltaY);
    if (!drag.scrubbing && !drag.verticalScrolling) {
      if (travel <= MINI_STRIP_TAP_SLOP_PX) return;
      if (lanes.length > 0 && Math.abs(deltaY) > Math.abs(deltaX)) {
        drag.verticalScrolling = true;
      } else {
        drag.scrubbing = true;
        onScrubStart?.();
      }
    }
    const viewport = event.currentTarget;
    if (drag.verticalScrolling) {
      viewport.scrollTop = Math.max(0, drag.startScrollTop - deltaY);
      return;
    }
    manualScrollRef.current = true;
    viewport.scrollLeft = Math.min(
      trackWidth,
      Math.max(0, drag.startScrollLeft - deltaX),
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
    if (drag.verticalScrolling) {
      cancelScheduledScrub();
      manualScrollRef.current = false;
      suppressClickRef.current = true;
      window.setTimeout(() => {
        suppressClickRef.current = false;
      }, 0);
      return;
    }
    if (drag.scrubbing) {
      scrubToScrollPosition(event.currentTarget.scrollLeft, true);
      suppressClickRef.current = true;
      window.setTimeout(() => {
        suppressClickRef.current = false;
        manualScrollRef.current = false;
      }, 0);
      return;
    }
    if (drag.startedLaneItem) {
      // Pointer capture retargets pointerup/click to the viewport, so the
      // parent completes a true lane tap. Horizontal scrub and vertical lane
      // scrolling still win once the gesture travels beyond tap slop.
      onSelectLaneItem?.(drag.startedLaneItem, drag.startedLaneItem.startS);
      suppressClickRef.current = true;
      event.preventDefault();
      window.setTimeout(() => {
        suppressClickRef.current = false;
      }, 0);
      return;
    }
    const rect = event.currentTarget.getBoundingClientRect();
    const seconds = pocketTimelineTimeAtTap({
      scrollLeft: event.currentTarget.scrollLeft,
      clientX: event.clientX,
      rectLeft: rect.left,
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

  const previewLaneResize = (
    item: MiniStripLaneItem,
    handle: "left" | "right",
    deltaS: number,
    recordHistory: () => void,
  ) => {
    if (!onPreviewLaneTiming || item.resizable === false) return;
    if (item.resizeDisabledReason) {
      onDisabledTap?.(item.resizeDisabledReason);
      return;
    }
    const next = resizeMiniStripLaneRange({
      item,
      handle,
      deltaS,
      durationS,
    });
    if (
      Math.abs(next.startS - item.startS) < 1e-6 &&
      Math.abs(next.endS - item.endS) < 1e-6
    ) {
      return;
    }
    recordHistory();
    onPreviewLaneTiming(item, next, handle);
  };

  const handleLaneResizePointerDown = (
    event: ReactPointerEvent<HTMLButtonElement>,
    item: MiniStripLaneItem,
    handle: "left" | "right",
  ) => {
    event.stopPropagation();
    if (item.resizeDisabledReason) {
      onDisabledTap?.(item.resizeDisabledReason);
      return;
    }
    event.currentTarget.setPointerCapture?.(event.pointerId);
    const viewport = viewportRef.current;
    manualScrollRef.current = true;
    laneResizeDragRef.current = {
      pointerId: event.pointerId,
      item,
      handle,
      startX: event.clientX,
      startScrollLeft: viewport?.scrollLeft ?? 0,
      latestClientX: event.clientX,
      recorded: false,
    };
  };

  const stopLaneResizeAutoPan = () => {
    if (laneResizeAutoPanFrameRef.current == null) return;
    cancelAnimationFrame(laneResizeAutoPanFrameRef.current);
    laneResizeAutoPanFrameRef.current = null;
  };

  const previewActiveLaneResize = (drag: LaneResizeDragState) => {
    const viewport = viewportRef.current;
    const scrollDeltaPx = viewport
      ? viewport.scrollLeft - drag.startScrollLeft
      : 0;
    previewLaneResize(
      drag.item,
      drag.handle,
      (drag.latestClientX - drag.startX + scrollDeltaPx) / pixelsPerSecond,
      () => {
        if (drag.recorded) return;
        drag.recorded = true;
        onLaneResizeStart?.(drag.item);
      },
    );
  };

  const startLaneResizeAutoPan = () => {
    if (laneResizeAutoPanFrameRef.current != null) return;
    const tick = () => {
      laneResizeAutoPanFrameRef.current = null;
      const drag = laneResizeDragRef.current;
      const viewport = viewportRef.current;
      if (!drag || !viewport) return;
      const rect = viewport.getBoundingClientRect();
      const direction = pocketLaneAutoPanDirection({
        clientX: drag.latestClientX,
        viewportLeft: rect.left,
        viewportRight: rect.right,
      });
      if (direction === 0) return;
      const overshootPx =
        direction < 0
          ? Math.max(0, rect.left - drag.latestClientX)
          : Math.max(0, drag.latestClientX - rect.right);
      const stepPx =
        POCKET_LANE_AUTO_PAN_PX_PER_FRAME + Math.min(12, overshootPx * 0.2);
      const maxScrollLeft = Math.max(
        0,
        viewport.scrollWidth - viewport.clientWidth,
      );
      const nextScrollLeft = Math.max(
        0,
        Math.min(maxScrollLeft, viewport.scrollLeft + direction * stepPx),
      );
      if (Math.abs(nextScrollLeft - viewport.scrollLeft) < 0.25) return;
      markProgrammaticScroll();
      programmaticScrollTargetRef.current = nextScrollLeft;
      viewport.scrollLeft = nextScrollLeft;
      previewActiveLaneResize(drag);
      laneResizeAutoPanFrameRef.current = requestAnimationFrame(tick);
    };
    laneResizeAutoPanFrameRef.current = requestAnimationFrame(tick);
  };

  const handleLaneResizePointerMove = (
    event: ReactPointerEvent<HTMLButtonElement>,
  ) => {
    event.stopPropagation();
    const drag = laneResizeDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    drag.latestClientX = event.clientX;
    previewActiveLaneResize(drag);
    const viewport = viewportRef.current;
    if (!viewport) return;
    const rect = viewport.getBoundingClientRect();
    if (
      pocketLaneAutoPanDirection({
        clientX: drag.latestClientX,
        viewportLeft: rect.left,
        viewportRight: rect.right,
      }) === 0
    ) {
      stopLaneResizeAutoPan();
      return;
    }
    startLaneResizeAutoPan();
  };

  const finishLaneResizePointer = (
    event: ReactPointerEvent<HTMLButtonElement>,
  ) => {
    event.stopPropagation();
    stopLaneResizeAutoPan();
    laneResizeDragRef.current = null;
    manualScrollRef.current = false;
  };

  const handleLaneResizeKey = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    item: MiniStripLaneItem,
    handle: "left" | "right",
  ) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const direction = event.key === "ArrowRight" ? 1 : -1;
    previewLaneResize(item, handle, direction * 0.1, () =>
      onLaneResizeStart?.(item),
    );
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
                  ? Math.max(
                      1,
                      measuredWidth - POCKET_TIMELINE_PLAYHEAD_INSET_PX,
                    ) /
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

      <div
        className="relative overflow-hidden bg-muted/30"
        style={{ height: timelineViewportHeight }}
      >
        <div
          ref={viewportRef}
          data-testid="pocket-timeline-viewport"
          className="scrollbar-none h-full overflow-auto overscroll-contain select-none [touch-action:none] [will-change:scroll-position]"
          onScroll={(event) => {
            const nextScrollLeft = event.currentTarget.scrollLeft;
            const horizontalChanged =
              Math.abs(nextScrollLeft - lastObservedScrollLeftRef.current) >=
              0.25;
            lastObservedScrollLeftRef.current = nextScrollLeft;
            if (!horizontalChanged) return;
            const programmaticTarget = programmaticScrollTargetRef.current;
            if (
              programmaticTarget != null &&
              Math.abs(event.currentTarget.scrollLeft - programmaticTarget) < 1
            ) {
              return;
            }
            programmaticScrollTargetRef.current = null;
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
            className="relative [contain:layout_paint]"
            style={{
              width: `calc(${trackWidth}px + 100vw)`,
              height: timelineContentHeight,
            }}
          >
            {segments.map((segment, index) => {
              const selected = selectedClipId === segment.id;
              const widthPx = segmentWidths[index];
              const duration = segment.endS - segment.startS;
              const filmstripSegment =
                filmstripSegmentById.get(segment.id) ?? segment;
              const filmstripDuration =
                filmstripSegment.endS - filmstripSegment.startS;
              return (
                <div
                  key={segment.id}
                  data-testid={`pocket-timeline-clip-${segment.id}`}
                  className="absolute top-2 h-16"
                  style={{
                    left: `calc(${POCKET_TIMELINE_PLAYHEAD_INSET_PX}px + ${segment.startS * pixelsPerSecond}px)`,
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
                      src={filmstripSegment.sourceUrl ?? null}
                      clipId={segment.id}
                      sourceId={filmstripSegment.sourceId ?? segment.id}
                      sourceStartS={filmstripSegment.sourceStartS ?? 0}
                      durationS={filmstripDuration}
                      sourceDurationS={
                        filmstripSegment.sourceDurationS ?? null
                      }
                      widthPx={filmstripWidths[index] ?? widthPx}
                      heightPx={64}
                      maxSeekCount={seekBudgets[index] ?? 0}
                      minSeekCount={seekBudgets[index] ?? 0}
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
                          setFilmstripSegments(latestSegmentsRef.current);
                        }}
                        onPointerCancel={(event) => {
                          event.stopPropagation();
                          trimDragRef.current = null;
                          setFilmstripSegments(latestSegmentsRef.current);
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
                  left: `calc(${POCKET_TIMELINE_PLAYHEAD_INSET_PX}px + ${mark.startS * pixelsPerSecond}px)`,
                  width: Math.max(
                    44,
                    (mark.endS - mark.startS) * pixelsPerSecond,
                  ),
                }}
                className="absolute top-[35px] z-20 h-11 min-w-11 rounded-none bg-transparent p-0 hover:bg-transparent focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 focus-visible:ring-0"
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

            {lanes.map((lane, laneIndex) => {
              const top =
                POCKET_TIMELINE_VIDEO_HEIGHT_PX +
                laneIndex * POCKET_TIMELINE_LANE_HEIGHT_PX;
              return (
                <div
                  key={lane.id}
                  role="group"
                  aria-label={`${lane.label} lane`}
                  data-testid={`pocket-timeline-lane-${lane.id}`}
                  className="absolute inset-x-0 border-t border-border bg-background/80"
                  style={{
                    top,
                    height: POCKET_TIMELINE_LANE_HEIGHT_PX,
                  }}
                >
                  <span className="sr-only">{lane.label}</span>
                  {lane.items.map((item) => {
                    const selected =
                      selectedLaneItem?.kind === item.kind &&
                      selectedLaneItem.id === item.id;
                    return (
                      <div
                        key={`${item.kind}:${item.id}`}
                        data-pocket-lane-kind={item.kind}
                        data-pocket-lane-id={item.id}
                        className="absolute top-0.5 z-20 h-11 min-w-11"
                        style={{
                          left: `calc(${POCKET_TIMELINE_PLAYHEAD_INSET_PX}px + ${item.startS * pixelsPerSecond}px)`,
                          width: Math.max(
                            44,
                            (item.endS - item.startS) * pixelsPerSecond,
                          ),
                        }}
                      >
                        <Button
                          type="button"
                          variant="ghost"
                          aria-label={`${lane.label}, ${item.label}, ${item.startS.toFixed(1)}–${item.endS.toFixed(1)} seconds`}
                          aria-pressed={selected}
                          onClick={(event) => {
                            event.stopPropagation();
                            if (suppressClickRef.current) return;
                            onSelectLaneItem?.(item, item.startS);
                          }}
                          className={cn(
                            "absolute inset-0 h-11 w-full min-w-11 justify-start overflow-hidden rounded-md border px-2 text-[10px] font-semibold shadow-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 focus-visible:ring-0",
                            item.kind === "text"
                              ? "border-violet-300 bg-violet-100 text-violet-950 hover:bg-violet-100"
                              : item.kind === "sfx" || item.kind === "music"
                                ? "border-sky-300 bg-sky-100 text-sky-950 hover:bg-sky-100"
                                : item.kind === "overlay"
                                  ? "border-amber-300 bg-amber-100 text-amber-950 hover:bg-amber-100"
                                  : "border-zinc-300 bg-zinc-100 text-zinc-950 hover:bg-zinc-100",
                            selected &&
                              "border-foreground ring-2 ring-lime-500 ring-offset-1",
                          )}
                        >
                          <span className="truncate">{item.label}</span>
                        </Button>
                        {selected &&
                          onPreviewLaneTiming &&
                          item.resizable !== false &&
                          (["left", "right"] as const).map((handle) => (
                            <Button
                              key={handle}
                              type="button"
                              variant="ghost"
                              data-pocket-lane-resize-handle={handle}
                              aria-label={`Resize ${item.label} ${
                                handle === "left" ? "start" : "end"
                              }, ${formatTimecode(item.endS - item.startS)}`}
                              aria-disabled={
                                item.resizeDisabledReason ? true : undefined
                              }
                              onPointerDown={(event) =>
                                handleLaneResizePointerDown(event, item, handle)
                              }
                              onPointerMove={handleLaneResizePointerMove}
                              onPointerUp={finishLaneResizePointer}
                              onPointerCancel={finishLaneResizePointer}
                              onKeyDown={(event) =>
                                handleLaneResizeKey(event, item, handle)
                              }
                              className={cn(
                                "absolute inset-y-0 z-30 h-11 w-11 rounded-none bg-transparent p-0 hover:bg-lime-100/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 focus-visible:ring-0",
                                handle === "left"
                                  ? "-left-[22px]"
                                  : "-right-[22px]",
                                item.resizeDisabledReason
                                  ? "opacity-50"
                                  : "cursor-ew-resize",
                              )}
                            >
                              <span
                                aria-hidden="true"
                                className={cn(
                                  "h-8 w-[3px] rounded-full bg-lime-600 shadow-sm",
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
                </div>
              );
            })}
          </div>
        </div>

        <div
          aria-hidden="true"
          data-testid="pocket-ministrip-playhead"
          className="pointer-events-none absolute inset-y-0 z-40 w-0.5 -translate-x-1/2 bg-foreground"
          style={{ left: POCKET_TIMELINE_PLAYHEAD_INSET_PX }}
        >
          <span className="absolute -left-[4px] top-0 h-2.5 w-2.5 rounded-b-sm bg-foreground" />
        </div>
      </div>
    </section>
  );
}
