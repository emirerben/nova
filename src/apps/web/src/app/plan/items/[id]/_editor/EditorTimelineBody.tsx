"use client";

/**
 * EditorTimelineBody — the editor-shell timeline (plan §6), mounted by
 * UnifiedTimeline when `editorMode` is set. Track order Text → Video (Clips)
 * → Sound (SFX sub-row above the music bed) → Overlays.
 *
 * Everything routes through the px-per-second scale (lib/timeline/timeline-scale):
 * fit = viewport/duration; zoom multiplies it; bars/playhead/scrub math all use
 * secondsToPx / pxToSeconds. Horizontal scroll when zoomed; the left gutter is
 * sticky so mute toggles + labels stay visible.
 *
 * D10 strict-neutral palette — lime appears ONLY as the selection ring. Video
 * shows a Filmstrip texture; Sound is zinc waveform-ish ink; Overlay is white /
 * zinc border. Bars get a subtle value shift on hover; the selection ring +
 * end-trim handles transition 120–180ms (motion-safe).
 */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import type { DraftSlot } from "@/app/generative/timeline-math";
import {
  fitPxPerSecond,
  pxToSeconds,
  resolveEditorTimelineScale,
  rulerTicks,
  scaledTrackWidth,
  secondsToPx,
  tickIntervalForScale,
} from "@/lib/timeline/timeline-scale";
import { formatTimecode } from "@/lib/timeline/time-format";
import type { TimelineClip } from "@/lib/generative-api";
import type {
  EditorSelection,
  EditorSelectionKind,
} from "./useEditorSelection";
import Filmstrip, { allocateFilmstripSeekBudget } from "./Filmstrip";
import { anchoredTimelineScrollLeft } from "./editor-timeline-scroll";
import {
  deriveLaneRows,
  deriveTextLaneRows,
  TEXT_LANE_ROW_GAP_PX,
  TEXT_LANE_BASE_HEIGHT_PX,
} from "./editor-bars";
import {
  type BarDragHandle,
  CLICK_DRAG_THRESHOLD_PX,
  applyClipSourceWindowDrag,
  applySfxMove,
  applyTextBarDrag,
  resolveBarDragHandle,
  secondsDeltaFromTimelineX,
  sequentialSlotLayout,
  timelineXFromClient,
} from "./editor-bar-drag";

/** Sticky left gutter (mute toggle + lane label). */
const GUTTER_PX = 64;
const SFX_SUB_LANE_BASE_HEIGHT_PX = 32;
const MUSIC_BED_HEIGHT_PX = 32;

export interface EditorSfxBar {
  id: string;
  at_s: number;
  end_s?: number | null;
  label?: string | null;
}
export interface EditorOverlayBar {
  id: string;
  start_s: number;
  end_s: number;
  label?: string | null;
}

export interface EditorTimelineBodyProps {
  durationS: number;
  currentTimeS: number;
  /** Zoom factor: 1 = fit-to-width. */
  zoom: number;
  /** Incremented only when the user explicitly presses Fit. */
  fitRequestKey?: number;
  /** Changes when a different rendered variant seeds the editor timeline. */
  scaleResetKey?: string;
  /** Reports the fit scale up so the shell can keep "fit" meaningful. */
  onReportFit?: (fitPxPerSecond: number) => void;

  selection: EditorSelection | null;
  onSelect: (kind: EditorSelectionKind, id: string) => void;
  onClear: () => void;

  textBars: TextElementBar[];
  readOnly?: boolean;
  onRecordTimelineEdit?: () => void;
  onPreviewTextTiming?: (
    id: string,
    patch: Pick<TextElementBar, "start_s" | "end_s">,
  ) => void;

  slots: DraftSlot[];
  clipSourceDurations?: Record<string, number | null>;
  onPreviewClipTiming?: (
    key: string,
    patch: Pick<DraftSlot, "inS" | "durationS" | "durationBeats">,
  ) => void;
  onPreviewSeek?: (seconds: number) => void;
  grid: number[];
  clipsLoading: boolean;
  filmstripClips: Pick<
    TimelineClip,
    "clip_index" | "signed_url" | "duration_s"
  >[];

  sfx: EditorSfxBar[];
  onPreviewSfxTiming?: (
    id: string,
    patch: Pick<EditorSfxBar, "at_s" | "end_s">,
  ) => void;
  hasMusic: boolean;
  musicLabel?: string;
  videoMuted: boolean;
  onToggleVideoMute: () => void;
  soundMuted: boolean;
  onToggleSoundMute: () => void;

  overlays: EditorOverlayBar[];
  onPreviewOverlayTiming?: (
    id: string,
    patch: Pick<EditorOverlayBar, "start_s" | "end_s">,
  ) => void;
  onOpenSounds?: () => void;

  onScrub: (seconds: number) => void;
  onScrubStart: () => void;
}

type ActiveDrag =
  | {
      kind: "text";
      id: string;
      handle: "left" | "right" | "body";
      startTimelineX: number;
      pxPerSecond: number;
      origin: Pick<TextElementBar, "start_s" | "end_s">;
      active: boolean;
    }
  | {
      kind: "clip";
      id: string;
      handle: BarDragHandle;
      startTimelineX: number;
      pxPerSecond: number;
      origin: Pick<DraftSlot, "inS" | "durationS">;
      sourceDurationS: number | null;
      active: boolean;
    }
  | {
      kind: "sfx";
      id: string;
      handle: "body";
      startTimelineX: number;
      pxPerSecond: number;
      origin: Pick<EditorSfxBar, "at_s" | "end_s">;
      active: boolean;
    }
  | {
      kind: "overlay";
      id: string;
      handle: "left" | "right" | "body";
      startTimelineX: number;
      pxPerSecond: number;
      origin: Pick<EditorOverlayBar, "start_s" | "end_s">;
      active: boolean;
    };

export default function EditorTimelineBody(props: EditorTimelineBodyProps) {
  const {
    durationS,
    currentTimeS,
    zoom,
    fitRequestKey,
    scaleResetKey,
    onReportFit,
    selection,
    onSelect,
    onClear,
    textBars,
    readOnly = false,
    onRecordTimelineEdit,
    onPreviewTextTiming,
    slots,
    clipSourceDurations,
    onPreviewClipTiming,
    onPreviewSeek,
    grid,
    clipsLoading,
    filmstripClips,
    sfx,
    onPreviewSfxTiming,
    hasMusic,
    musicLabel,
    videoMuted,
    onToggleVideoMute,
    soundMuted,
    onToggleSoundMute,
    overlays,
    onPreviewOverlayTiming,
    onOpenSounds,
    onScrub,
    onScrubStart,
  } = props;

  const scrollRef = useRef<HTMLDivElement>(null);
  const rulerContentRef = useRef<HTMLDivElement>(null);
  const gutterRowsRef = useRef<HTMLDivElement>(null);
  const previousScaleRef = useRef<{ pps: number; trackW: number } | null>(null);
  const lastFitRequestKeyRef = useRef(fitRequestKey);
  const lastScaleResetKeyRef = useRef(scaleResetKey);
  const dragRef = useRef<ActiveDrag | null>(null);
  const suppressClickRef = useRef(false);
  const [viewportW, setViewportW] = useState(0);
  const [dragLabel, setDragLabel] = useState<{
    x: number;
    y: number;
    text: string;
  } | null>(null);
  const [filmstripSlots, setFilmstripSlots] = useState(slots);
  const [frozenFitPps, setFrozenFitPps] = useState<number | null>(null);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const measure = () => setViewportW(el.clientWidth);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const trackViewportW = Math.max(0, viewportW);
  const liveFitPps = fitPxPerSecond(trackViewportW, durationS);
  const { fitPxPerSecond: fitPps, pxPerSecond: pps } =
    resolveEditorTimelineScale({
      viewportWidth: trackViewportW,
      durationS,
      zoom,
      frozenFitPxPerSecond: frozenFitPps,
    });
  const trackW = Math.max(trackViewportW, scaledTrackWidth(durationS, pps));
  const videoEndPx = secondsToPx(durationS, pps);
  const showEndMarker = videoEndPx > 0 && videoEndPx < trackW - 1;

  useEffect(() => {
    if (fitPps > 0) onReportFit?.(fitPps);
  }, [fitPps, onReportFit]);

  useLayoutEffect(() => {
    if (trackViewportW <= 0 || durationS <= 0) return;
    const resetRequested = lastScaleResetKeyRef.current !== scaleResetKey;
    const fitRequested = lastFitRequestKeyRef.current !== fitRequestKey;

    if (resetRequested) lastScaleResetKeyRef.current = scaleResetKey;
    if (fitRequested) lastFitRequestKeyRef.current = fitRequestKey;

    if (frozenFitPps == null || resetRequested || fitRequested) {
      setFrozenFitPps(liveFitPps);
    }
  }, [
    durationS,
    fitRequestKey,
    frozenFitPps,
    liveFitPps,
    scaleResetKey,
    trackViewportW,
  ]);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    const previous = previousScaleRef.current;
    if (!el || !previous || previous.pps === pps || viewportW <= 0) {
      previousScaleRef.current = { pps, trackW };
      return;
    }

    el.scrollLeft = anchoredTimelineScrollLeft({
      previousScrollLeft: el.scrollLeft,
      viewportWidth: el.clientWidth,
      previousPxPerSecond: previous.pps,
      nextPxPerSecond: pps,
      durationS,
      currentTimeS,
    });
    previousScaleRef.current = { pps, trackW };
  }, [currentTimeS, durationS, pps, trackW, viewportW]);

  const playheadPx = secondsToPx(currentTimeS, pps);
  const slotLayout = sequentialSlotLayout(slots, grid);
  const windows = slotLayout.windows;
  const filmstripLayout = sequentialSlotLayout(filmstripSlots, grid);
  const filmstripSourceByIndex = new Map(
    filmstripClips.map((clip) => [clip.clip_index, clip]),
  );
  const activeFilmstripCount = filmstripLayout.windows.reduce(
    (count, win, i) => {
      const slot = filmstripSlots[i];
      return slot && !slot.removed && win.startS != null && win.durationS > 0
        ? count + 1
        : count;
    },
    0,
  );
  const perClipSeekBudget =
    activeFilmstripCount > 0
      ? Math.max(1, Math.floor(24 / activeFilmstripCount))
      : 0;
  const zoomSeekBudget = Math.max(1, Math.round(zoom * 10));
  const filmstripWidths = filmstripLayout.windows.map((win, i) => {
    const slot = filmstripSlots[i];
    if (!slot || slot.removed || win.startS == null || win.durationS <= 0)
      return 0;
    return Math.max(8, secondsToPx(win.durationS, pps));
  });
  const filmstripSeekBudgets = allocateFilmstripSeekBudget(
    filmstripWidths.map((width) => (width > 0 ? 1 : 0)),
    Math.min(
      24,
      perClipSeekBudget * activeFilmstripCount,
      zoomSeekBudget * activeFilmstripCount,
    ),
  ).map((budget) =>
    budget > 0 ? Math.min(perClipSeekBudget, zoomSeekBudget) : 0,
  );
  const filmstripByKey = new Map(
    filmstripSlots.map((slot, i) => [
      slot.key,
      {
        slot,
        win: filmstripLayout.windows[i],
        widthPx: filmstripWidths[i] ?? 0,
        maxSeekCount: filmstripSeekBudgets[i] ?? 0,
      },
    ]),
  );
  const tickInterval = tickIntervalForScale(pps);
  const ticks = rulerTicks(durationS, pps);
  const textLane = deriveTextLaneRows(textBars);
  const sfxLane = deriveLaneRows(sfx, {
    baseHeightPx: SFX_SUB_LANE_BASE_HEIGHT_PX,
  });
  const soundLaneHeight = sfxLane.totalHeightPx + MUSIC_BED_HEIGHT_PX;
  const overlayLane = deriveLaneRows(overlays, {
    baseHeightPx: TEXT_LANE_BASE_HEIGHT_PX,
  });
  const laneRows = [
    { label: "Text", heightPx: textLane.totalHeightPx },
    { label: "Video", heightPx: 48 },
    { label: "Sound", heightPx: soundLaneHeight },
    { label: "Overlays", heightPx: overlayLane.totalHeightPx },
  ];
  const lanesHeight = laneRows.reduce((total, row) => total + row.heightPx, 0);

  useEffect(() => {
    const drag = dragRef.current;
    if (drag?.kind === "clip" && drag.active) return;
    setFilmstripSlots(slots);
  }, [slots]);

  // ── Scrub (ruler click/drag → seek; pauses playback per the contract) ────────
  const scrubbing = useRef(false);
  function scrubToClientX(clientX: number, trackEl: HTMLElement) {
    const rect = trackEl.getBoundingClientRect();
    const localX = clientX - rect.left;
    const sec = Math.max(0, Math.min(durationS, pxToSeconds(localX, pps)));
    onScrub(sec);
  }
  function onRulerPointerDown(e: React.PointerEvent<HTMLDivElement>) {
    scrubbing.current = true;
    onScrubStart();
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    scrubToClientX(e.clientX, e.currentTarget);
  }
  function onRulerPointerMove(e: React.PointerEvent<HTMLDivElement>) {
    if (!scrubbing.current) return;
    scrubToClientX(e.clientX, e.currentTarget);
  }
  function onRulerPointerUp() {
    scrubbing.current = false;
  }

  function pointerTimelineX(clientX: number): number {
    const el = scrollRef.current;
    if (!el) return 0;
    const rect = el.getBoundingClientRect();
    return timelineXFromClient({
      clientX,
      scrollRectLeft: rect.left,
      scrollLeft: el.scrollLeft,
    });
  }

  function activateDrag(drag: ActiveDrag) {
    if (drag.active) return drag;
    drag.active = true;
    suppressClickRef.current = true;
    onScrubStart();
    onRecordTimelineEdit?.();
    onSelect(drag.kind, drag.id);
    return drag;
  }

  function updateDrag(clientX: number) {
    const drag = dragRef.current;
    if (!drag) return;
    const currentTimelineX = pointerTimelineX(clientX);
    const deltaPx = currentTimelineX - drag.startTimelineX;
    if (!drag.active && Math.abs(deltaPx) < CLICK_DRAG_THRESHOLD_PX) return;
    const active = activateDrag(drag);
    const deltaS = secondsDeltaFromTimelineX({
      currentTimelineX,
      startTimelineX: active.startTimelineX,
      pxPerSecond: active.pxPerSecond,
    });

    if (active.kind === "text") {
      const next = applyTextBarDrag({
        bar: active.origin,
        handle: active.handle,
        deltaS,
        videoDurationS: durationS,
      });
      onPreviewTextTiming?.(active.id, next);
      setDragLabel({
        x: clientX,
        y: window.innerHeight - 118,
        text: `${Math.max(0, next.end_s - next.start_s).toFixed(1)}s`,
      });
    } else if (active.kind === "clip") {
      const next = applyClipSourceWindowDrag({
        slot: active.origin,
        handle: active.handle,
        deltaS,
        sourceDurationS: active.sourceDurationS,
      });
      onPreviewClipTiming?.(active.id, next);
      const idx = slots.findIndex((slot) => slot.key === active.id);
      const win = windows[idx];
      if (win?.startS != null) {
        onPreviewSeek?.(
          active.handle === "right"
            ? win.startS +
                (next.durationS ?? active.origin.durationS ?? win.durationS)
            : win.startS,
        );
      }
      setDragLabel({
        x: clientX,
        y: window.innerHeight - 118,
        text: `${(next.durationS ?? active.origin.durationS ?? 0).toFixed(1)}s`,
      });
    } else if (active.kind === "sfx") {
      const next = applySfxMove({
        atS: active.origin.at_s,
        endS: active.origin.end_s,
        deltaS,
        videoDurationS: durationS,
      });
      onPreviewSfxTiming?.(active.id, next);
      setDragLabel({
        x: clientX,
        y: window.innerHeight - 118,
        text: `${Math.max(0, (next.end_s ?? next.at_s) - next.at_s).toFixed(1)}s`,
      });
    } else {
      const duration = active.origin.end_s - active.origin.start_s;
      const minDuration = 0.3;
      let next = active.origin;
      if (active.handle === "body") {
        const maxStart = Math.max(0, durationS - duration);
        const start_s = Math.max(0, Math.min(maxStart, active.origin.start_s + deltaS));
        next = {
          start_s: Math.round(start_s * 10) / 10,
          end_s: Math.round((start_s + duration) * 10) / 10,
        };
      } else if (active.handle === "left") {
        const start_s = Math.max(0, Math.min(active.origin.end_s - minDuration, active.origin.start_s + deltaS));
        next = {
          start_s: Math.round(start_s * 10) / 10,
          end_s: active.origin.end_s,
        };
      } else {
        const end_s = Math.min(durationS, Math.max(active.origin.start_s + minDuration, active.origin.end_s + deltaS));
        next = {
          start_s: active.origin.start_s,
          end_s: Math.round(end_s * 10) / 10,
        };
      }
      onPreviewOverlayTiming?.(active.id, next);
      setDragLabel({
        x: clientX,
        y: window.innerHeight - 118,
        text: `${Math.max(0, next.end_s - next.start_s).toFixed(1)}s`,
      });
    }
  }

  function startTextDrag(
    e: React.PointerEvent<HTMLElement>,
    bar: TextElementBar,
  ) {
    if (readOnly) return;
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    const rect = e.currentTarget.getBoundingClientRect();
    dragRef.current = {
      kind: "text",
      id: bar.id,
      handle: resolveBarDragHandle({
        localX: e.clientX - rect.left,
        width: rect.width,
      }),
      startTimelineX: pointerTimelineX(e.clientX),
      pxPerSecond: pps,
      origin: { start_s: bar.start_s, end_s: bar.end_s },
      active: false,
    };
  }

  function startClipDrag(e: React.PointerEvent<HTMLElement>, slot: DraftSlot) {
    if (readOnly || slot.removed) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const handle = resolveBarDragHandle({
      localX: e.clientX - rect.left,
      width: rect.width,
    });
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    const slotIndex = slots.findIndex((s) => s.key === slot.key);
    const effectiveDurationS =
      slot.durationS ?? windows[slotIndex]?.durationS ?? 0.6;
    dragRef.current = {
      kind: "clip",
      id: slot.key,
      handle,
      startTimelineX: pointerTimelineX(e.clientX),
      pxPerSecond: pps,
      origin: { inS: slot.inS, durationS: effectiveDurationS },
      sourceDurationS: clipSourceDurations?.[slot.key] ?? null,
      active: false,
    };
  }

  function startSfxDrag(e: React.PointerEvent<HTMLElement>, bar: EditorSfxBar) {
    if (readOnly) return;
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragRef.current = {
      kind: "sfx",
      id: bar.id,
      handle: "body",
      startTimelineX: pointerTimelineX(e.clientX),
      pxPerSecond: pps,
      origin: { at_s: bar.at_s, end_s: bar.end_s },
      active: false,
    };
  }

  function startOverlayDrag(e: React.PointerEvent<HTMLElement>, bar: EditorOverlayBar) {
    if (readOnly) return;
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    const rect = e.currentTarget.getBoundingClientRect();
    dragRef.current = {
      kind: "overlay",
      id: bar.id,
      handle: resolveBarDragHandle({
        localX: e.clientX - rect.left,
        width: rect.width,
      }),
      startTimelineX: pointerTimelineX(e.clientX),
      pxPerSecond: pps,
      origin: { start_s: bar.start_s, end_s: bar.end_s },
      active: false,
    };
  }

  function finishDrag(
    e: React.PointerEvent<HTMLElement>,
    kind: EditorSelectionKind,
    id: string,
  ) {
    const drag = dragRef.current;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    if (!drag || drag.id !== id) {
      onSelect(kind, id);
      return;
    }
    if (!drag.active) {
      onSelect(kind, id);
    } else {
      e.preventDefault();
      e.stopPropagation();
    }
    dragRef.current = null;
    if (drag.kind === "clip") {
      setFilmstripSlots(slots);
    }
    setDragLabel(null);
  }

  function cancelDrag() {
    const drag = dragRef.current;
    dragRef.current = null;
    if (drag?.kind === "clip") {
      setFilmstripSlots(slots);
    }
    setDragLabel(null);
  }

  const syncTimelineChrome = useCallback((el: HTMLDivElement) => {
    if (rulerContentRef.current) {
      rulerContentRef.current.style.transform = `translateX(${-el.scrollLeft}px)`;
    }
    if (gutterRowsRef.current) {
      gutterRowsRef.current.style.transform = `translateY(${-el.scrollTop}px)`;
    }
  }, []);

  function onTimelineScroll(e: React.UIEvent<HTMLDivElement>) {
    syncTimelineChrome(e.currentTarget);
  }

  function onTimelineWheel(e: React.WheelEvent<HTMLDivElement>) {
    const el = e.currentTarget;
    if (el.scrollWidth <= el.clientWidth) return;
    if (el.scrollHeight > el.clientHeight + 1) return;
    if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
    e.preventDefault();
    el.scrollLeft += e.deltaY;
    syncTimelineChrome(el);
  }

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el) syncTimelineChrome(el);
  }, [lanesHeight, syncTimelineChrome, trackW]);

  const isSel = (kind: EditorSelectionKind, id: string) =>
    selection?.kind === kind && selection.id === id;

  const ringCls =
    "outline outline-2 outline-lime-500 outline-offset-[1px] motion-safe:transition-[outline-color,box-shadow] motion-safe:duration-150";

  return (
    <div
      role="listbox"
      aria-label="Editor timeline selections"
      className="h-full w-full max-w-full min-w-0 select-none overflow-hidden"
      data-testid="editor-timeline"
    >
      <div className="flex h-full w-full max-w-full min-w-0 overflow-hidden">
        <div
          className="flex flex-shrink-0 flex-col bg-white"
          style={{ width: GUTTER_PX }}
        >
          <div className="h-6 border-b border-zinc-200 bg-white" />
          <div className="min-h-0 flex-1 overflow-hidden">
            <div ref={gutterRowsRef} style={{ height: lanesHeight }}>
              <GutterRow label="Text" heightPx={textLane.totalHeightPx} />
              <GutterRow
                label="Video"
                heightPx={48}
                muteState={{
                  muted: videoMuted,
                  onToggle: onToggleVideoMute,
                  title: "Original audio",
                }}
              />
              <GutterRow
                label="Sound"
                heightPx={soundLaneHeight}
                muteState={{
                  muted: soundMuted,
                  onToggle: onToggleSoundMute,
                  title: "Music + effects",
                }}
              />
              <GutterRow
                label="Overlays"
                heightPx={overlayLane.totalHeightPx}
              />
            </div>
          </div>
        </div>
        <div className="flex w-0 min-w-0 flex-1 flex-col overflow-hidden">
          <div className="h-6 overflow-hidden border-b border-zinc-200 bg-zinc-50">
            <div
              ref={rulerContentRef}
              className="relative h-6 cursor-ew-resize"
              data-testid="editor-timeline-ruler"
              style={{ width: trackW, minWidth: trackW }}
              onPointerDown={onRulerPointerDown}
              onPointerMove={onRulerPointerMove}
              onPointerUp={onRulerPointerUp}
              onPointerCancel={onRulerPointerUp}
            >
              {ticks.map((t) => (
                <div
                  key={t}
                  className="pointer-events-none absolute top-0 h-full"
                  style={{ left: secondsToPx(t, pps) }}
                >
                  <div className="h-2 w-px bg-zinc-300" />
                  <span className="absolute left-1 top-1.5 whitespace-nowrap text-[9px] leading-none text-zinc-400">
                    {tickInterval < 1 ? t.toFixed(1) : formatTimecode(t)}
                  </span>
                </div>
              ))}
              <Playline px={playheadPx} withHead />
            </div>
          </div>
          <div
            ref={scrollRef}
            className="min-h-0 flex-1 overflow-auto"
            data-testid="editor-timeline-lanes-scroll"
            onScroll={onTimelineScroll}
            onWheel={onTimelineWheel}
          >
            <div
              className="relative"
              style={{ width: trackW, minWidth: trackW, height: lanesHeight }}
            >
              {/* ── Text lane ── */}
              <LaneTrack
                trackW={trackW}
                heightPx={textLane.totalHeightPx}
                testId="editor-text-lane"
              >
                <Playline px={playheadPx} />
                {textBars.length === 0 ? (
                  <GhostRow text="Add text from the Text tool" />
                ) : (
                  <>
                    {Array.from(
                      { length: Math.max(0, textLane.rowCount - 1) },
                      (_, i) => (
                        <div
                          key={`text-row-separator-${i}`}
                          className="pointer-events-none absolute inset-x-0 border-t border-zinc-200/80"
                          style={{
                            top:
                              (i + 1) * textLane.rowHeightPx +
                              i * TEXT_LANE_ROW_GAP_PX +
                              TEXT_LANE_ROW_GAP_PX / 2,
                          }}
                          aria-hidden
                        />
                      ),
                    )}
                    {textLane.rows.map(
                      ({ bar: b, rowIndex, topPx, heightPx }) => {
                        const left = secondsToPx(b.start_s, pps);
                        const width = Math.max(
                          6,
                          secondsToPx(b.end_s - b.start_s, pps),
                        );
                        const selected = isSel("text", b.id);
                        return (
                          <BarButton
                            key={b.id}
                            left={left}
                            width={width}
                            top={topPx}
                            height={heightPx}
                            selected={selected}
                            ringCls={ringCls}
                            ariaLabel={`Text row ${rowIndex + 1}, ${b.text.slice(0, 24)}, ${formatTimecode(b.start_s)}–${formatTimecode(b.end_s)}`}
                            onSelect={() => onSelect("text", b.id)}
                            dataKind="text"
                            dataId={b.id}
                            dataRowIndex={rowIndex}
                            onPointerDown={(e) => startTextDrag(e, b)}
                            onPointerMove={(e) => updateDrag(e.clientX)}
                            onPointerUp={(e) => finishDrag(e, "text", b.id)}
                            onPointerCancel={cancelDrag}
                            suppressClickRef={suppressClickRef}
                            showTrimHandles
                            className="bg-[#0c0c0e] text-white"
                          >
                            <span className="pointer-events-none flex items-center gap-1 truncate px-2 text-[10px]">
                              <span className="font-semibold">T</span>
                              <span className="truncate">
                                {b.text || "Text"}
                              </span>
                            </span>
                          </BarButton>
                        );
                      },
                    )}
                  </>
                )}
              </LaneTrack>

              {/* ── Video lane (Clips + filmstrip) ── */}
              <LaneTrack trackW={trackW} heightPx={TEXT_LANE_BASE_HEIGHT_PX}>
                <Playline px={playheadPx} />
                {clipsLoading ? (
                  <div className="absolute inset-1 rounded bg-zinc-200/60 motion-safe:animate-pulse" />
                ) : (
                  windows.map((win, i) => {
                    const slot = slots[i];
                    if (
                      !slot ||
                      slot.removed ||
                      win.startS == null ||
                      win.durationS <= 0
                    )
                      return null;
                    const left = secondsToPx(win.startS, pps);
                    const width = Math.max(8, secondsToPx(win.durationS, pps));
                    const selected = isSel("clip", slot.key);
                    const strip = filmstripByKey.get(slot.key);
                    const stripSlot = strip?.slot ?? slot;
                    const stripWin = strip?.win ?? win;
                    const source = filmstripSourceByIndex.get(
                      stripSlot.clipIndex,
                    );
                    return (
                      <button
                        key={slot.key}
                        type="button"
                        aria-label={`Clip ${i + 1}, timeline ${formatTimecode(win.startS)}–${formatTimecode(win.startS + win.durationS)}, source ${slot.inS.toFixed(1)}–${(slot.inS + win.durationS).toFixed(1)}`}
                        aria-pressed={selected}
                        data-editor-bar-kind="clip"
                        data-editor-bar-id={slot.key}
                        onPointerDown={(e) => startClipDrag(e, slot)}
                        onPointerMove={(e) => updateDrag(e.clientX)}
                        onPointerUp={(e) => finishDrag(e, "clip", slot.key)}
                        onPointerCancel={cancelDrag}
                        onClick={(e) => {
                          e.stopPropagation();
                          if (suppressClickRef.current) {
                            suppressClickRef.current = false;
                            return;
                          }
                          onSelect("clip", slot.key);
                        }}
                        className={[
                          "group absolute inset-y-0.5 min-w-11 cursor-grab overflow-hidden rounded border bg-zinc-200 transition-colors active:cursor-grabbing focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500",
                          selected
                            ? `border-transparent ${ringCls}`
                            : "border-white/50 hover:border-white",
                        ].join(" ")}
                        style={{ left, width }}
                      >
                        <span className="pointer-events-none absolute inset-0">
                          <Filmstrip
                            src={source?.signed_url ?? null}
                            clipId={stripSlot.key}
                            sourceId={stripSlot.clipIndex}
                            sourceStartS={stripSlot.inS}
                            durationS={stripWin.durationS}
                            sourceDurationS={
                              source?.duration_s ??
                              clipSourceDurations?.[stripSlot.key] ??
                              null
                            }
                            widthPx={strip?.widthPx ?? width}
                            maxSeekCount={strip?.maxSeekCount ?? 0}
                            label=""
                          />
                        </span>
                        {i > 0 && (
                          <span className="absolute inset-y-0 left-0 w-px bg-white/80" />
                        )}
                        <span className="pointer-events-none absolute inset-0 flex items-center px-2 text-[10px] font-semibold text-white drop-shadow">
                          <span className="truncate">
                            Clip {i + 1} · {win.durationS.toFixed(1)}s
                          </span>
                        </span>
                        <TimelineTrimHandle side="left" selected={selected} />
                        <TimelineTrimHandle side="right" selected={selected} />
                      </button>
                    );
                  })
                )}
              </LaneTrack>

              {/* ── Sound lane (SFX sub-row above the music bed) ── */}
              <LaneTrack trackW={trackW} heightPx={soundLaneHeight}>
                <Playline px={playheadPx} />
                {/* SFX rows above the fixed music bed. */}
                <div
                  className="absolute inset-x-0 top-0"
                  style={{ height: sfxLane.totalHeightPx }}
                  data-testid="editor-sfx-lane"
                >
                  {Array.from(
                    { length: Math.max(0, sfxLane.rowCount - 1) },
                    (_, i) => (
                      <div
                        key={`sfx-row-separator-${i}`}
                        className="pointer-events-none absolute inset-x-0 border-t border-zinc-200/80"
                        style={{
                          top:
                            (i + 1) * sfxLane.rowHeightPx +
                            i * TEXT_LANE_ROW_GAP_PX +
                            TEXT_LANE_ROW_GAP_PX / 2,
                        }}
                        aria-hidden
                      />
                    ),
                  )}
                  {sfx.length === 0 && (
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        onOpenSounds?.();
                      }}
                      className="absolute left-1 bottom-0.5 top-0.5 rounded border border-dashed border-zinc-300 px-2 text-[10px] text-zinc-500 hover:border-zinc-400 hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                    >
                      + Add sounds
                    </button>
                  )}
                  {sfxLane.rows.map(
                    ({ item: s, rowIndex, topPx, heightPx }) => {
                      const left = secondsToPx(s.at_s, pps);
                      const end = s.end_s ?? s.at_s + 0.6;
                      const width = Math.max(
                        6,
                        secondsToPx(end - s.at_s, pps),
                      );
                      const selected = isSel("sfx", s.id);
                      return (
                        <BarButton
                          key={s.id}
                          left={left}
                          width={width}
                          top={topPx}
                          height={heightPx}
                          selected={selected}
                          ringCls={ringCls}
                          ariaLabel={`Sound effect row ${rowIndex + 1}, ${s.label ?? ""} at ${formatTimecode(s.at_s)}`}
                          onSelect={() => onSelect("sfx", s.id)}
                          dataKind="sfx"
                          dataId={s.id}
                          dataRowIndex={rowIndex}
                          onPointerDown={(e) => startSfxDrag(e, s)}
                          onPointerMove={(e) => updateDrag(e.clientX)}
                          onPointerUp={(e) => finishDrag(e, "sfx", s.id)}
                          onPointerCancel={cancelDrag}
                          suppressClickRef={suppressClickRef}
                          className="bg-zinc-300 text-[#0c0c0e]"
                        >
                          <span className="pointer-events-none truncate px-1.5 text-[9px]">
                            {s.label ?? "sfx"}
                          </span>
                        </BarButton>
                      );
                    },
                  )}
                </div>
                {/* Music bed (bottom half) — full-width; split disabled on it */}
                <div
                  className="absolute inset-x-0 bottom-0"
                  style={{ height: MUSIC_BED_HEIGHT_PX }}
                >
                  {hasMusic ? (
                    <BarButton
                      left={0}
                      width={secondsToPx(durationS, pps)}
                      selected={isSel("music", "bed")}
                      ringCls={ringCls}
                      ariaLabel={`Music bed ${musicLabel ?? ""}`}
                      onSelect={() => onSelect("music", "bed")}
                      dataKind="music"
                      dataId="bed"
                      className="inset-y-0.5 bg-zinc-200 text-[#0c0c0e]"
                    >
                      <span className="pointer-events-none flex items-center gap-1 truncate px-2 text-[10px]">
                        <span aria-hidden>♫</span>
                        <span className="truncate">
                          {musicLabel ?? "Music"}
                        </span>
                      </span>
                    </BarButton>
                  ) : (
                    sfx.length === 0 && (
                      <div className="absolute inset-x-1 bottom-0.5 top-0.5 flex items-center rounded border border-dashed border-zinc-300 px-2 text-[10px] text-zinc-400">
                        Add sounds from the Sounds tool
                      </div>
                    )
                  )}
                </div>
              </LaneTrack>

              {/* ── Overlays lane ── */}
              <LaneTrack
                trackW={trackW}
                heightPx={overlayLane.totalHeightPx}
                testId="editor-overlays-lane"
              >
                <Playline px={playheadPx} />
                {overlays.length === 0 ? (
                  <GhostRow text="Overlays appear here" />
                ) : (
                  <>
                    {Array.from(
                      { length: Math.max(0, overlayLane.rowCount - 1) },
                      (_, i) => (
                        <div
                          key={`overlay-row-separator-${i}`}
                          className="pointer-events-none absolute inset-x-0 border-t border-zinc-200/80"
                          style={{
                            top:
                              (i + 1) * overlayLane.rowHeightPx +
                              i * TEXT_LANE_ROW_GAP_PX +
                              TEXT_LANE_ROW_GAP_PX / 2,
                          }}
                          aria-hidden
                        />
                      ),
                    )}
                    {overlayLane.rows.map(
                      ({ item: o, rowIndex, topPx, heightPx }) => {
                        const left = secondsToPx(o.start_s, pps);
                        const width = Math.max(
                          8,
                          secondsToPx(o.end_s - o.start_s, pps),
                        );
                        const selected = isSel("overlay", o.id);
                        return (
                          <BarButton
                            key={o.id}
                            left={left}
                            width={width}
                            top={topPx}
                            height={heightPx}
                            selected={selected}
                            ringCls={ringCls}
                            ariaLabel={`Overlay row ${rowIndex + 1}, ${o.label ?? ""}, ${formatTimecode(o.start_s)}–${formatTimecode(o.end_s)}`}
                            onSelect={() => onSelect("overlay", o.id)}
                            dataKind="overlay"
                            dataId={o.id}
                            dataRowIndex={rowIndex}
                            onPointerDown={(e) => startOverlayDrag(e, o)}
                            onPointerMove={(e) => updateDrag(e.clientX)}
                            onPointerUp={(e) => finishDrag(e, "overlay", o.id)}
                            onPointerCancel={cancelDrag}
                            suppressClickRef={suppressClickRef}
                            className="border border-zinc-300 bg-white text-[#0c0c0e]"
                          >
                            <span className="pointer-events-none truncate px-2 text-[10px]">
                              {o.label ?? "Overlay"}
                            </span>
                          </BarButton>
                        );
                      },
                    )}
                  </>
                )}
              </LaneTrack>
              {showEndMarker && <EndOfVideoMarker left={videoEndPx} />}
            </div>
          </div>
        </div>
      </div>
      {dragLabel && (
        <div
          className="pointer-events-none fixed z-[80] -translate-x-1/2 rounded-md bg-[#0c0c0e] px-2 py-1 text-[11px] font-semibold tabular-nums text-white shadow-lg"
          style={{ left: dragLabel.x, top: dragLabel.y }}
        >
          {dragLabel.text}
        </div>
      )}
    </div>
  );
}

// ── Sub-components ──────────────────────────────────────────────────────────

/** One px-positioned playhead segment (line only; head on the ruler copy). */
function Playline({
  px,
  withHead = false,
}: {
  px: number;
  withHead?: boolean;
}) {
  return (
    <div
      className="pointer-events-none absolute top-0 bottom-0 z-20 w-px bg-[#0c0c0e]/80"
      style={{ left: px }}
      aria-hidden
    >
      {withHead && (
        <div className="absolute -top-1 left-1/2 h-2 w-2 -translate-x-1/2 rounded-[2px] bg-[#0c0c0e]" />
      )}
    </div>
  );
}

function EndOfVideoMarker({ left }: { left: number }) {
  return (
    <div
      className="pointer-events-none absolute bottom-0 top-0 z-10 w-px bg-zinc-400/40"
      style={{ left }}
      aria-hidden
    />
  );
}

function GutterRow({
  label,
  heightPx,
  muteState,
}: {
  label: string;
  heightPx: number;
  muteState?: { muted: boolean; onToggle: () => void; title: string };
}) {
  return (
    <div
      className="flex items-center gap-1 border-b border-zinc-200 bg-white pl-1.5 pr-1"
      style={{ height: heightPx }}
    >
      {muteState ? (
        <button
          type="button"
          aria-label={`${muteState.title} ${muteState.muted ? "muted" : "audible"}`}
          aria-pressed={muteState.muted}
          title={
            muteState.muted
              ? `${muteState.title}: muted`
              : `${muteState.title}: audible`
          }
          onClick={muteState.onToggle}
          className={`flex h-11 w-11 flex-shrink-0 items-center justify-center rounded text-[10px] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
            muteState.muted
              ? "text-zinc-300"
              : "text-[#3f3f46] hover:bg-zinc-100"
          }`}
        >
          {muteState.muted ? "🔇" : "🔊"}
        </button>
      ) : (
        <span className="w-11 flex-shrink-0" />
      )}
      <span className="truncate text-[9px] font-semibold uppercase tracking-wider text-zinc-500">
        {label}
      </span>
    </div>
  );
}

function LaneTrack({
  trackW,
  heightPx,
  testId,
  children,
}: {
  trackW: number;
  heightPx: number;
  testId?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className="relative overflow-hidden border-b border-zinc-200 bg-zinc-50"
      style={{ width: trackW, height: heightPx }}
      data-testid={testId}
    >
      {children}
    </div>
  );
}

function GhostRow({ text }: { text: string }) {
  return (
    <div className="absolute inset-1 flex items-center rounded border border-dashed border-zinc-300 px-2 text-[10px] text-zinc-400">
      {text}
    </div>
  );
}

function BarButton({
  left,
  width,
  top,
  height,
  selected,
  ringCls,
  ariaLabel,
  onSelect,
  onPointerDown,
  onPointerMove,
  onPointerUp,
  onPointerCancel,
  suppressClickRef,
  showTrimHandles = false,
  dataKind,
  dataId,
  dataRowIndex,
  className,
  children,
}: {
  left: number;
  width: number;
  top?: number;
  height?: number;
  selected: boolean;
  ringCls: string;
  ariaLabel: string;
  onSelect: () => void;
  onPointerDown?: (e: React.PointerEvent<HTMLButtonElement>) => void;
  onPointerMove?: (e: React.PointerEvent<HTMLButtonElement>) => void;
  onPointerUp?: (e: React.PointerEvent<HTMLButtonElement>) => void;
  onPointerCancel?: (e: React.PointerEvent<HTMLButtonElement>) => void;
  suppressClickRef?: React.MutableRefObject<boolean>;
  showTrimHandles?: boolean;
  dataKind?: string;
  dataId?: string;
  dataRowIndex?: number;
  className: string;
  children: React.ReactNode;
}) {
  const positionedInRow = top != null && height != null;
  return (
    <button
      type="button"
      aria-label={ariaLabel}
      aria-pressed={selected}
      data-editor-bar-kind={dataKind}
      data-editor-bar-id={dataId}
      data-editor-row-index={dataRowIndex}
      data-editor-text-row-index={dataKind === "text" ? dataRowIndex : undefined}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      onClick={(e) => {
        e.stopPropagation();
        if (suppressClickRef?.current) {
          suppressClickRef.current = false;
          return;
        }
        onSelect();
      }}
      className={[
        "group absolute flex min-w-11 cursor-grab items-center rounded transition-[filter,outline-color] hover:brightness-110 active:cursor-grabbing focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500",
        positionedInRow ? "" : "inset-y-0.5",
        selected ? ringCls : "",
        className,
      ].join(" ")}
      style={{
        left,
        width,
        ...(positionedInRow ? { top, height } : {}),
      }}
    >
      {children}
      {showTrimHandles && (
        <>
          <TimelineTrimHandle side="left" selected={selected} />
          <TimelineTrimHandle side="right" selected={selected} />
        </>
      )}
    </button>
  );
}

/** End-trim handle (visual affordance; transitions in with the ring). */
function TimelineTrimHandle({
  side,
  selected,
}: {
  side: "left" | "right";
  selected: boolean;
}) {
  return (
    <span
      aria-hidden
      className={`absolute top-1/2 z-10 flex h-8 w-2 -translate-y-1/2 cursor-ew-resize items-center justify-center rounded-sm bg-white/95 shadow-sm ring-1 ring-black/10 motion-safe:transition-opacity motion-safe:duration-150 ${
        selected ? "opacity-100" : "opacity-0 group-hover:opacity-100"
      } ${side === "left" ? "left-0" : "right-0"}`}
    >
      <span className="flex flex-col gap-0.5" aria-hidden>
        <span className="h-0.5 w-0.5 rounded-full bg-[#0c0c0e]" />
        <span className="h-0.5 w-0.5 rounded-full bg-[#0c0c0e]" />
        <span className="h-0.5 w-0.5 rounded-full bg-[#0c0c0e]" />
      </span>
    </span>
  );
}
