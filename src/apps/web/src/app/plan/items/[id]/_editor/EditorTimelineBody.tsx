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
import { Button } from "@/components/ui/button";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import type { CameraEffect } from "@/lib/plan-api";
import type { DraftSlot } from "@/app/generative/timeline-math";
import {
  projectBaseRange,
  unprojectOutputTime,
  unprojectOutputRange,
  type VirtualTimeline,
} from "./virtual-timeline";
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
import type { MotionPresetInstance } from "@nova/motion-runtime";
import type {
  EditorSelection,
  EditorSelectionKind,
} from "./useEditorSelection";
import Filmstrip, { allocateFilmstripSeekBudget } from "./Filmstrip";
import { anchoredTimelineScrollLeft } from "./editor-timeline-scroll";
import {
  useEditorPlaybackTime,
  type EditorPlaybackClock,
} from "./editor-playback-clock";
import {
  AI_SEQUENCE_BADGE_TOOLTIP,
  deriveLaneRows,
  deriveTextLaneRows,
  isAiSequenceBar,
  isCaptionBar,
  TEXT_LANE_ROW_GAP_PX,
  TEXT_LANE_BASE_HEIGHT_PX,
} from "./editor-bars";
import {
  type BarDragHandle,
  BAR_EDGE_HIT_PX,
  CLIP_MIN_DURATION_S,
  CLICK_DRAG_THRESHOLD_PX,
  applyClipSourceWindowDrag,
  applySfxBarDrag,
  applyTextBarDrag,
  effectiveBarEdgeHitPx,
  minimumClipDurationForSlot,
  renderedSequentialSlotLayout,
  resolveBarDragHandle,
  secondsDeltaFromTimelineX,
  sequentialSlotLayout,
  timelineXFromClient,
} from "./editor-bar-drag";

/** Sticky left gutter (mute toggle + lane label). */
const GUTTER_PX = 64;
const SFX_SUB_LANE_BASE_HEIGHT_PX = 32;
const MUSIC_BED_HEIGHT_PX = 32;
/**
 * Collapsed Captions lane: one condensed strip of ticks. A 45s talking-head
 * edit carries 30-40 cues, so the lane only earns full rows while the user is
 * actually working on captions (the Captions tool open) — the rest of the time
 * it exists to show caption DENSITY against the clip cuts, which is how you
 * spot a stretch where the transcript dropped a sentence.
 */
const CAPTIONS_LANE_COLLAPSED_HEIGHT_PX = 20;
/**
 * Expanded Captions lane: ONE row, always.
 *
 * Caption cues are a transcript stream — strictly sequential, never
 * overlapping (verified against production data: a 74-cue variant has 0
 * overlapping pairs). `deriveTextLaneRows` exists for AUTHORED text, which can
 * overlap, so it assigns one row per bar unconditionally; running caption cues
 * through it produces a 74 x 26px = 2070px lane inside a 260px timeline region.
 * Cues share a single row because they cannot collide.
 */
const CAPTIONS_LANE_EXPANDED_HEIGHT_PX = 28;

const TEXT_BEHIND_SUBJECT_UI_ENABLED =
  process.env.NEXT_PUBLIC_TEXT_BEHIND_SUBJECT_ENABLED === "true";

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
  /** ✓-accepted AI suggestion, unsaved — dashed lime provenance styling. */
  suggested?: boolean;
}

export interface EditorVisualBlockBar {
  id: string;
  kind: "montage" | "text_card";
  start_s: number;
  end_s: number;
}

export interface EditorMotionBar {
  id: string;
  label: string;
  start_s: number;
  end_s: number;
  sourceScene: MotionPresetInstance;
  readOnly?: boolean;
}

export type CarouselBlockPosition = "intro" | "middle" | "outro";

/** Staged carousel-moment block (Lane C, carousel-blocks train). No
 *  `start_s` — the chip's window is derived from `position` + the CURRENT
 *  clip layout (mirrors the server's `_insert_carousel_moment_step` splice,
 *  same as `buildVirtualTimeline`'s `VirtualCarouselSplice` in
 *  virtual-timeline.ts), so it stays correct as clips are trimmed/split. */
export interface EditorCarouselBlockBar {
  id: string;
  effectLabel: string;
  durationS: number;
  position: CarouselBlockPosition;
}

export interface EditorTimelineBodyProps {
  durationS: number;
  /** Canonical assembled timeline used by playback, ruler, scrubbing and all
   * video-lane geometry. Never rebuild insertion timing inside this component. */
  timelineProjection: VirtualTimeline;
  /** Real rendered player duration, used to calibrate transition overlap. */
  renderedOutputDurationS?: number | null;
  currentTimeS: number;
  playbackClock?: EditorPlaybackClock | null;
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
  /** Captions tool open ⇒ the Captions lane expands to selectable rows. */
  captionsExpanded?: boolean;
  /** Local subtitles on/off. Off dims the lane rather than hiding it —
   *  a lane that vanishes reads as breakage, not as a setting. */
  captionsEnabled?: boolean;
  /** Click on the collapsed strip: open the Captions tool at that cue. */
  onOpenCaptionCue?: (id: string) => void;
  readOnly?: boolean;
  textReadOnly?: boolean;
  textDisabledReason?: string | null;
  onRecordTimelineEdit?: () => void;
  onPreviewTextTiming?: (
    id: string,
    patch: Pick<TextElementBar, "start_s" | "end_s">,
    handle: "left" | "right" | "body",
    origin: TextElementBar,
  ) => void;

  visualBlocks: EditorVisualBlockBar[];
  showVisualBlocks?: boolean;
  visualBlocksReadOnly?: boolean;
  visualBlocksDisabledReason?: string | null;
  onPreviewVisualTiming?: (
    id: string,
    patch: Pick<EditorVisualBlockBar, "start_s" | "end_s">,
  ) => void;
  motionBlocks?: EditorMotionBar[];
  showMotionBlocks?: boolean;
  motionBlocksReadOnly?: boolean;
  motionBlocksDisabledReason?: string | null;
  onPreviewMotionTiming?: (
    id: string,
    patch: Pick<EditorMotionBar, "start_s" | "end_s">,
    origin: EditorMotionBar,
  ) => void;

  cameraEffects?: CameraEffect[];
  onPreviewCameraTiming?: (
    id: string,
    patch: Pick<CameraEffect, "start_s" | "end_s">,
  ) => void;

  slots: DraftSlot[];
  clipReadOnly?: boolean;
  clipAddReadOnly?: boolean;
  clipDisabledReason?: string | null;
  clipAddDisabledReason?: string | null;
  clipSourceDurations?: Record<string, number | null>;
  onPreviewClipTiming?: (
    key: string,
    patch: Pick<DraftSlot, "inS" | "durationS" | "durationBeats">,
  ) => void;
  onPreviewSeek?: (seconds: number) => void;
  grid: number[];
  clipPreviewMode?: "rendered" | "virtual";
  clipsLoading: boolean;
  filmstripClips: Pick<
    TimelineClip,
    "clip_index" | "signed_url" | "duration_s"
  >[];
  allowRepeatedSources?: boolean;
  /** Append an uploaded source that the rendered cut did not select. */
  onAddClip?: (clipIndex: number) => void;

  /** Staged carousel-moment block, or null/undefined when none is staged. */
  carouselBlock?: EditorCarouselBlockBar | null;
  /** A persisted block remains selectable for explanation, but cannot be
   *  dragged when this variant's Carousel capability is unavailable. */
  carouselReadOnly?: boolean;
  carouselDisabledReason?: string | null;
  /** Chip click — opens the panel as inspector (mirrors the "Add a block"
   *  entry point; the caller decides how to surface the panel). */
  onSelectCarousel?: () => void;
  /** Fired by either a drag onto one of the three position drop targets OR a
   *  direct click on one (both stage + history.record() on the caller side). */
  onSetCarouselPosition?: (position: CarouselBlockPosition) => void;
  /** Live outer-edge stretch; caller scales internal choreography without
   * recording another undo snapshot for every pointermove. */
  onPreviewCarouselDuration?: (durationS: number) => number | void;

  sfx: EditorSfxBar[];
  sfxReadOnly?: boolean;
  sfxDisabledReason?: string | null;
  onPreviewSfxTiming?: (
    id: string,
    patch: Pick<EditorSfxBar, "at_s" | "end_s">,
  ) => void;
  hasMusic: boolean;
  musicLabel?: string;
  soundLaneTitle?: string;
  soundBedLabel?: string;
  soundBedTitle?: string;
  videoMuted: boolean;
  onToggleVideoMute: () => void;
  soundMuted: boolean;
  onToggleSoundMute: () => void;

  overlays: EditorOverlayBar[];
  overlaysReadOnly?: boolean;
  overlaysDisabledReason?: string | null;
  onPreviewOverlayTiming?: (
    id: string,
    patch: Pick<EditorOverlayBar, "start_s" | "end_s">,
  ) => void;
  onOpenSounds?: () => void;

  onScrub: (seconds: number) => void;
  onScrubStart: () => void;
  flashIds?: Set<string>;
}

type ActiveDrag =
  | {
      kind: "carousel";
      id: string;
      handle: "left" | "right";
      startTimelineX: number;
      pxPerSecond: number;
      origin: { durationS: number };
      active: boolean;
    }
  | {
      kind: "text";
      id: string;
      handle: "left" | "right" | "body";
      startTimelineX: number;
      pxPerSecond: number;
      origin: TextElementBar;
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
      minDurationS: number;
      active: boolean;
    }
  | {
      kind: "sfx";
      id: string;
      handle: BarDragHandle;
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
    }
  | {
      kind: "visual";
      id: string;
      handle: "left" | "right" | "body";
      startTimelineX: number;
      pxPerSecond: number;
      origin: Pick<EditorVisualBlockBar, "start_s" | "end_s">;
      active: boolean;
    }
  | {
      kind: "motion";
      id: string;
      handle: "left" | "right" | "body";
      startTimelineX: number;
      pxPerSecond: number;
      origin: EditorMotionBar;
      active: boolean;
    }
  | {
      kind: "camera";
      id: string;
      handle: "left" | "right" | "body";
      startTimelineX: number;
      pxPerSecond: number;
      origin: Pick<CameraEffect, "start_s" | "end_s">;
      active: boolean;
    };

export default function EditorTimelineBody(props: EditorTimelineBodyProps) {
  const {
    durationS,
    timelineProjection,
    renderedOutputDurationS,
    currentTimeS,
    playbackClock,
    zoom,
    fitRequestKey,
    scaleResetKey,
    onReportFit,
    selection,
    onSelect,
    onClear,
    textBars: baseTextBars,
    captionsExpanded = false,
    captionsEnabled = true,
    onOpenCaptionCue,
    readOnly = false,
    textReadOnly = readOnly,
    textDisabledReason,
    onRecordTimelineEdit,
    onPreviewTextTiming,
    visualBlocks: baseVisualBlocks,
    showVisualBlocks = true,
    visualBlocksReadOnly = readOnly,
    visualBlocksDisabledReason,
    onPreviewVisualTiming,
    motionBlocks: baseMotionBlocks = [],
    showMotionBlocks = false,
    motionBlocksReadOnly = readOnly,
    motionBlocksDisabledReason,
    onPreviewMotionTiming,
    cameraEffects: baseCameraEffects = [],
    onPreviewCameraTiming,
    slots,
    clipReadOnly = false,
    clipAddReadOnly = clipReadOnly,
    clipDisabledReason,
    clipAddDisabledReason,
    clipSourceDurations,
    onPreviewClipTiming,
    onPreviewSeek,
    grid,
    clipPreviewMode = "rendered",
    clipsLoading,
    filmstripClips,
    allowRepeatedSources = false,
    onAddClip,
    carouselBlock = null,
    carouselReadOnly = false,
    carouselDisabledReason,
    onSelectCarousel,
    onSetCarouselPosition,
    onPreviewCarouselDuration,
    sfx: baseSfx,
    sfxReadOnly = readOnly,
    sfxDisabledReason,
    onPreviewSfxTiming,
    hasMusic,
    musicLabel,
    soundLaneTitle,
    soundBedLabel,
    soundBedTitle,
    videoMuted,
    onToggleVideoMute,
    soundMuted,
    onToggleSoundMute,
    overlays: baseOverlays,
    overlaysReadOnly = readOnly,
    overlaysDisabledReason,
    onPreviewOverlayTiming,
    onOpenSounds,
    onScrub,
    onScrubStart,
    flashIds,
  } = props;

  const projectRange = <T extends { start_s: number; end_s: number }>(item: T): T => {
    const projected = projectBaseRange(timelineProjection, {
      startS: item.start_s,
      endS: item.end_s,
    });
    return { ...item, start_s: projected.startS, end_s: projected.endS };
  };
  const textBars = baseTextBars.map(projectRange);
  const visualBlocks = baseVisualBlocks.map(projectRange);
  const motionBlocks = baseMotionBlocks.map(projectRange);
  const cameraEffects = baseCameraEffects.map(projectRange);
  const overlays = baseOverlays.map(projectRange);
  const sfx = baseSfx.map((item) => {
    const projected = projectBaseRange(timelineProjection, {
      startS: item.at_s,
      endS: item.end_s ?? item.at_s,
    });
    return {
      ...item,
      at_s: projected.startS,
      end_s: item.end_s == null ? null : projected.endS,
    };
  });

  const toBaseRange = (range: { start_s: number; end_s: number }) => {
    const base = unprojectOutputRange(timelineProjection, {
      startS: range.start_s,
      endS: range.end_s,
    });
    return { start_s: base.startS, end_s: base.endS };
  };

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

  const baseSlotLayout = sequentialSlotLayout(slots, grid);
  const renderedLayoutOptions = {
    outputDurationS: renderedOutputDurationS,
    fallbackOverlapS: 0,
  };
  const renderedSlotLayout =
    clipPreviewMode === "rendered"
      ? renderedSequentialSlotLayout(slots, grid, renderedLayoutOptions)
      : baseSlotLayout;
  const effectiveDurationS =
    timelineProjection.totalDurationS > 0
      ? timelineProjection.totalDurationS
      : renderedSlotLayout.totalDurationS > 0
        ? renderedSlotLayout.totalDurationS
        : durationS;

  const trackViewportW = Math.max(0, viewportW);
  const liveFitPps = fitPxPerSecond(trackViewportW, effectiveDurationS);
  const { fitPxPerSecond: fitPps, pxPerSecond: pps } =
    resolveEditorTimelineScale({
      viewportWidth: trackViewportW,
      durationS: effectiveDurationS,
      zoom,
      frozenFitPxPerSecond: frozenFitPps,
    });
  const trackW = Math.max(trackViewportW, scaledTrackWidth(effectiveDurationS, pps));
  const videoEndPx = secondsToPx(effectiveDurationS, pps);
  const showEndMarker = videoEndPx > 0 && videoEndPx < trackW - 1;

  useEffect(() => {
    if (fitPps > 0) onReportFit?.(fitPps);
  }, [fitPps, onReportFit]);

  useLayoutEffect(() => {
    if (trackViewportW <= 0 || effectiveDurationS <= 0) return;
    const resetRequested = lastScaleResetKeyRef.current !== scaleResetKey;
    const fitRequested = lastFitRequestKeyRef.current !== fitRequestKey;

    if (resetRequested) lastScaleResetKeyRef.current = scaleResetKey;
    if (fitRequested) lastFitRequestKeyRef.current = fitRequestKey;

    if (frozenFitPps == null || resetRequested || fitRequested) {
      setFrozenFitPps(liveFitPps);
    }
  }, [
    effectiveDurationS,
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
      durationS: effectiveDurationS,
      currentTimeS,
    });
    previousScaleRef.current = { pps, trackW };
  }, [currentTimeS, effectiveDurationS, pps, trackW, viewportW]);

  const projectedClipBySlot = new Map(
    timelineProjection.entries
      .filter((entry) => entry.kind === "clip")
      .map((entry) => [entry.slotIndex, entry] as const),
  );
  const windows = slots.map((slot, index) => {
    const projected = projectedClipBySlot.get(index);
    const base = renderedSlotLayout.windows[index] ?? baseSlotLayout.windows[index];
    if (!projected || slot.removed) {
      return { startS: null, durationS: 0, offsetBeats: base?.offsetBeats ?? null };
    }
    return {
      startS: projected.startS,
      durationS: projected.durationS,
      offsetBeats: base?.offsetBeats ?? null,
    };
  });
  const projectedCarousel = timelineProjection.entries.find((entry) => entry.kind === "carousel");
  const carouselWindow = carouselBlock && projectedCarousel
    ? { startS: projectedCarousel.startS, durationS: projectedCarousel.durationS }
    : null;
  const filmstripLayout =
    clipPreviewMode === "rendered"
      ? renderedSequentialSlotLayout(
          filmstripSlots,
          grid,
          renderedLayoutOptions,
        )
      : sequentialSlotLayout(filmstripSlots, grid);
  const filmstripSourceByIndex = new Map(
    filmstripClips.map((clip) => [clip.clip_index, clip]),
  );
  const activeClipIndices = new Set(
    slots.filter((slot) => !slot.removed).map((slot) => slot.clipIndex),
  );
  const unusedFilmstripClips = allowRepeatedSources
    ? filmstripClips
    : filmstripClips.filter((clip) => !activeClipIndices.has(clip.clip_index));
  const videoLaneHeight = unusedFilmstripClips.length > 0 ? 76 : 48;
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
  const ticks = rulerTicks(effectiveDurationS, pps);
  // Captions get their own lane. Before this split they shared the Text lane,
  // where 30-40 cues crushed the creator's own text into unreadable slivers.
  const captionBars = textBars.filter(isCaptionBar);
  const plainTextBars = textBars.filter((bar) => !isCaptionBar(bar));
  const hasCaptionLane = captionBars.length > 0;
  const captionsLaneHeight = !hasCaptionLane
    ? 0
    : captionsExpanded
      ? CAPTIONS_LANE_EXPANDED_HEIGHT_PX
      : CAPTIONS_LANE_COLLAPSED_HEIGHT_PX;
  const textLane = deriveTextLaneRows(plainTextBars);
  const visualLane = deriveLaneRows(visualBlocks, {
    baseHeightPx: TEXT_LANE_BASE_HEIGHT_PX,
  });
  const motionLane = deriveLaneRows(motionBlocks, {
    baseHeightPx: SFX_SUB_LANE_BASE_HEIGHT_PX,
  });
  const cameraLane = deriveLaneRows(cameraEffects, {
    baseHeightPx: SFX_SUB_LANE_BASE_HEIGHT_PX,
  });
  const sfxLane = deriveLaneRows(sfx, {
    baseHeightPx: SFX_SUB_LANE_BASE_HEIGHT_PX,
  });
  const soundLaneHeight = sfxLane.totalHeightPx + MUSIC_BED_HEIGHT_PX;
  const overlayLane = deriveLaneRows(overlays, {
    baseHeightPx: TEXT_LANE_BASE_HEIGHT_PX,
  });
  const laneRows = [
    { label: "Text", heightPx: textLane.totalHeightPx },
    ...(hasCaptionLane ? [{ label: "Captions", heightPx: captionsLaneHeight }] : []),
    ...(showVisualBlocks
      ? [{ label: "Visuals", heightPx: visualLane.totalHeightPx }]
      : []),
    ...(showMotionBlocks
      ? [{ label: "Blocks", heightPx: motionLane.totalHeightPx }]
      : []),
    ...(cameraEffects.length > 0
      ? [{ label: "Camera", heightPx: cameraLane.totalHeightPx }]
      : []),
    { label: "Video", heightPx: videoLaneHeight },
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
    const sec = Math.max(0, Math.min(effectiveDurationS, pxToSeconds(localX, pps)));
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

  // Carousel-block reposition: the Video lane's three "drop targets" are the
  // thirds of the visible track (left = intro, middle = middle, right =
  // outro) rather than three separate widgets — the chip is draggable
  // anywhere over the lane, and the drop x-position resolves which third it
  // landed in. `onDragOver` must call preventDefault() or the browser never
  // fires `drop` at all (HTML5 DnD spec default is "reject").
  function handleCarouselDragOver(e: React.DragEvent<HTMLDivElement>) {
    if (!carouselBlock || carouselReadOnly) return;
    e.preventDefault();
  }
  function handleCarouselDrop(e: React.DragEvent<HTMLDivElement>) {
    if (!carouselBlock || carouselReadOnly || !onSetCarouselPosition) return;
    e.preventDefault();
    const x = pointerTimelineX(e.clientX);
    const totalPx = Math.max(1, secondsToPx(effectiveDurationS, pps));
    const frac = Math.max(0, Math.min(1, x / totalPx));
    const position: CarouselBlockPosition =
      frac < 1 / 3 ? "intro" : frac < 2 / 3 ? "middle" : "outro";
    onSetCarouselPosition(position);
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
        videoDurationS: effectiveDurationS,
      });
      onPreviewTextTiming?.(active.id, toBaseRange(next), active.handle, active.origin);
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
        minDurationS: active.minDurationS,
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
    } else if (active.kind === "carousel") {
      const direction = active.handle === "left" ? -1 : 1;
      const nextDuration = Math.max(
        2,
        Math.min(15, Math.round((active.origin.durationS + direction * deltaS) * 10) / 10),
      );
      const appliedDuration = onPreviewCarouselDuration?.(nextDuration);
      setDragLabel({
        x: clientX,
        y: window.innerHeight - 118,
        text: `${(appliedDuration ?? nextDuration).toFixed(1)}s`,
      });
    } else if (active.kind === "sfx") {
      const next = applySfxBarDrag({
        bar: active.origin,
        handle: active.handle,
        deltaS,
        videoDurationS: effectiveDurationS,
      });
      const baseRange =
        next.end_s == null
          ? null
          : unprojectOutputRange(timelineProjection, {
              startS: next.at_s,
              endS: next.end_s,
            });
      onPreviewSfxTiming?.(active.id, {
        at_s: baseRange?.startS ?? unprojectOutputTime(timelineProjection, next.at_s),
        end_s: baseRange?.endS ?? null,
      });
      setDragLabel({
        x: clientX,
        y: window.innerHeight - 118,
        text: `${Math.max(0, (next.end_s ?? next.at_s) - next.at_s).toFixed(1)}s`,
      });
    } else {
      const duration = active.origin.end_s - active.origin.start_s;
      const minDuration = active.kind === "motion" ? 1 / 30 : 0.3;
      const snapTiming = (value: number) =>
        active.kind === "motion" ? Math.round(value * 30) / 30 : Math.round(value * 10) / 10;
      let next = active.origin;
      if (active.handle === "body") {
        const maxStart = Math.max(0, effectiveDurationS - duration);
        const start_s = Math.max(0, Math.min(maxStart, active.origin.start_s + deltaS));
        next = {
          start_s: snapTiming(start_s),
          end_s: snapTiming(start_s + duration),
        };
      } else if (active.handle === "left") {
        const start_s = Math.max(0, Math.min(active.origin.end_s - minDuration, active.origin.start_s + deltaS));
        next = {
          start_s: snapTiming(start_s),
          end_s: active.origin.end_s,
        };
      } else {
        const end_s = Math.min(effectiveDurationS, Math.max(active.origin.start_s + minDuration, active.origin.end_s + deltaS));
        next = {
          start_s: active.origin.start_s,
          end_s: snapTiming(end_s),
        };
      }
      if (active.kind === "visual") {
        onPreviewVisualTiming?.(active.id, toBaseRange(next));
      } else if (active.kind === "motion") {
        onPreviewMotionTiming?.(active.id, toBaseRange(next), active.origin);
      } else if (active.kind === "camera") {
        onPreviewCameraTiming?.(active.id, toBaseRange(next));
      } else {
        onPreviewOverlayTiming?.(active.id, toBaseRange(next));
      }
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
    if (readOnly || textReadOnly) return;
    if (bar.role === "lyric_line") return;
    if (bar.id.startsWith("subtitled-caption-")) return;
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
      origin: bar,
      active: false,
    };
  }

  function startClipDrag(e: React.PointerEvent<HTMLElement>, slot: DraftSlot) {
    if (readOnly || clipReadOnly || slot.removed) return;
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
      slot.durationS ?? windows[slotIndex]?.durationS ?? CLIP_MIN_DURATION_S;
    const minDurationS = minimumClipDurationForSlot({
      grid,
      offsetBeats: windows[slotIndex]?.offsetBeats,
    });
    dragRef.current = {
      kind: "clip",
      id: slot.key,
      handle,
      startTimelineX: pointerTimelineX(e.clientX),
      pxPerSecond: pps,
      origin: { inS: slot.inS, durationS: effectiveDurationS },
      sourceDurationS: clipSourceDurations?.[slot.key] ?? null,
      minDurationS,
      active: false,
    };
  }

  function startSfxDrag(e: React.PointerEvent<HTMLElement>, bar: EditorSfxBar) {
    if (readOnly || sfxReadOnly) return;
    const rect = e.currentTarget.getBoundingClientRect();
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragRef.current = {
      kind: "sfx",
      id: bar.id,
      handle: resolveBarDragHandle({
        localX: e.clientX - rect.left,
        width: rect.width,
      }),
      startTimelineX: pointerTimelineX(e.clientX),
      pxPerSecond: pps,
      origin: { at_s: bar.at_s, end_s: bar.end_s },
      active: false,
    };
  }

  function startCarouselResize(
    e: React.PointerEvent<HTMLElement>,
    handle: "left" | "right",
  ) {
    if (!carouselBlock || carouselReadOnly) return;
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragRef.current = {
      kind: "carousel",
      id: carouselBlock.id,
      handle,
      startTimelineX: pointerTimelineX(e.clientX),
      pxPerSecond: pps,
      origin: { durationS: carouselBlock.durationS },
      active: false,
    };
  }

  function startOverlayDrag(e: React.PointerEvent<HTMLElement>, bar: EditorOverlayBar) {
    if (readOnly || overlaysReadOnly) return;
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

  function startVisualDrag(
    e: React.PointerEvent<HTMLElement>,
    block: EditorVisualBlockBar,
  ) {
    if (readOnly || visualBlocksReadOnly) return;
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    const rect = e.currentTarget.getBoundingClientRect();
    dragRef.current = {
      kind: "visual",
      id: block.id,
      handle: resolveBarDragHandle({
        localX: e.clientX - rect.left,
        width: rect.width,
      }),
      startTimelineX: pointerTimelineX(e.clientX),
      pxPerSecond: pps,
      origin: block,
      active: false,
    };
  }

  function startMotionDrag(
    e: React.PointerEvent<HTMLElement>,
    block: EditorMotionBar,
  ) {
    if (readOnly || motionBlocksReadOnly || block.readOnly) return;
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    const rect = e.currentTarget.getBoundingClientRect();
    dragRef.current = {
      kind: "motion",
      id: block.id,
      handle: resolveBarDragHandle({
        localX: e.clientX - rect.left,
        width: rect.width,
      }),
      startTimelineX: pointerTimelineX(e.clientX),
      pxPerSecond: pps,
      origin: block,
      active: false,
    };
  }

  function startCameraDrag(e: React.PointerEvent<HTMLElement>, effect: CameraEffect) {
    if (readOnly) return;
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    const rect = e.currentTarget.getBoundingClientRect();
    dragRef.current = {
      kind: "camera",
      id: effect.id,
      handle: resolveBarDragHandle({
        localX: e.clientX - rect.left,
        width: rect.width,
      }),
      startTimelineX: pointerTimelineX(e.clientX),
      pxPerSecond: pps,
      origin: { start_s: effect.start_s, end_s: effect.end_s },
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
    } else if (drag?.kind === "text") {
      onPreviewTextTiming?.(
        drag.id,
        { start_s: drag.origin.start_s, end_s: drag.origin.end_s },
        "body",
        drag.origin,
      );
    } else if (drag?.kind === "motion") {
      onPreviewMotionTiming?.(drag.id, toBaseRange(drag.origin), drag.origin);
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
              {hasCaptionLane && (
                <GutterRow
                  label={captionsEnabled ? "Captions" : "Captions · off"}
                  heightPx={captionsLaneHeight}
                />
              )}
              {showVisualBlocks && (
                <GutterRow label="Visuals" heightPx={visualLane.totalHeightPx} />
              )}
              {showMotionBlocks && (
                <GutterRow label="Blocks" heightPx={motionLane.totalHeightPx} />
              )}
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
                  title: soundLaneTitle ?? "Music + effects",
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
              <Playline currentTimeS={currentTimeS} playbackClock={playbackClock} pps={pps} withHead />
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
                <Playline currentTimeS={currentTimeS} playbackClock={playbackClock} pps={pps} />
                {plainTextBars.length === 0 ? (
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
                        const flashing = flashIds?.has(b.id) ?? false;
                        const captionLocked = b.id.startsWith("subtitled-caption-");
                        const locked = b.role === "lyric_line" || captionLocked;
                        const aiSequence = isAiSequenceBar(b);
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
                            onPointerDown={
                              locked || textReadOnly ? undefined : (e) => startTextDrag(e, b)
                            }
                            onPointerMove={(e) => updateDrag(e.clientX)}
                            onPointerUp={(e) => finishDrag(e, "text", b.id)}
                            onPointerCancel={cancelDrag}
                            suppressClickRef={suppressClickRef}
                            showTrimHandles={!locked && !textReadOnly}
                            title={textReadOnly ? (textDisabledReason ?? undefined) : undefined}
                            flashing={flashing}
                            className="bg-[#0c0c0e] text-white"
                          >
                            <span className="pointer-events-none flex items-center gap-1 truncate px-2 text-[10px]">
                              <span className="font-semibold">
                                {captionLocked ? "C" : locked ? "L" : "T"}
                              </span>
                              <span className="truncate">
                                {b.text || "Text"}
                              </span>
                              {locked && (
                                <span
                                  aria-label={
                                    captionLocked ? "Caption timing locked" : "Lyric timing locked"
                                  }
                                  title={
                                    captionLocked
                                      ? // Names the Captions rail tool. This used
                                        // to say "the Captions tab", which only
                                        // ever existed on the item page — in the
                                        // editor it pointed at nothing.
                                        "Caption timing is edited in the Captions panel"
                                      : "Lyric timing is locked to the vocal"
                                  }
                                  className="shrink-0 rounded border border-white/30 px-1 text-[9px] opacity-90"
                                >
                                  {"\u{1F512}"}
                                </span>
                              )}
                              {TEXT_BEHIND_SUBJECT_UI_ENABLED && b.behind_subject && (
                                <span
                                  aria-label="Behind subject"
                                  title="Behind subject"
                                  className="shrink-0 opacity-80"
                                >
                                  ⧉
                                </span>
                              )}
                              {aiSequence && (
                                <span
                                  aria-label="AI sequence"
                                  title={AI_SEQUENCE_BADGE_TOOLTIP}
                                  className="shrink-0 opacity-90"
                                >
                                  {"✦"}
                                </span>
                              )}
                            </span>
                          </BarButton>
                        );
                      },
                    )}
                  </>
                )}
              </LaneTrack>

              {/* ── Captions lane ──
                  Collapsed: a density strip you can click to jump into the
                  Captions tool. Expanded (tool open): full selectable,
                  trimmable rows. Dimmed, never hidden, when subtitles are off. */}
              {hasCaptionLane && (
                <LaneTrack
                  trackW={trackW}
                  heightPx={captionsLaneHeight}
                  testId="editor-captions-lane"
                >
                  <Playline currentTimeS={currentTimeS} playbackClock={playbackClock} pps={pps} />
                  <div
                    className={captionsEnabled ? undefined : "opacity-40"}
                    style={{ height: captionsLaneHeight }}
                  >
                    {captionsExpanded
                      ? captionBars.map((b, i) => {
                          const left = secondsToPx(b.start_s, pps);
                          const width = Math.max(
                            6,
                            secondsToPx(b.end_s - b.start_s, pps),
                          );
                          return (
                            <BarButton
                              key={b.id}
                              left={left}
                              width={width}
                              // Single row: cues never overlap, so they cannot
                              // collide on one line (see the constant's note).
                              top={0}
                              height={CAPTIONS_LANE_EXPANDED_HEIGHT_PX}
                              selected={isSel("text", b.id)}
                              ringCls={ringCls}
                              ariaLabel={`Caption ${i + 1}, ${b.text.slice(0, 24)}, ${formatTimecode(b.start_s)}–${formatTimecode(b.end_s)}`}
                              onSelect={() => onSelect("text", b.id)}
                              dataKind="text"
                              dataId={b.id}
                              dataRowIndex={0}
                              onPointerDown={(e) => startTextDrag(e, b)}
                              onPointerMove={(e) => updateDrag(e.clientX)}
                              onPointerUp={(e) => finishDrag(e, "text", b.id)}
                              onPointerCancel={cancelDrag}
                              suppressClickRef={suppressClickRef}
                              showTrimHandles
                              flashing={flashIds?.has(b.id) ?? false}
                              className="bg-[#0c0c0e] text-white"
                            >
                              <span className="pointer-events-none flex items-center gap-1 truncate px-2 text-[10px]">
                                <span className="font-semibold">C</span>
                                <span className="truncate">{b.text || "Caption"}</span>
                              </span>
                            </BarButton>
                          );
                        })
                      : captionBars.map((b) => {
                          const left = secondsToPx(b.start_s, pps);
                          const width = Math.max(
                            3,
                            secondsToPx(b.end_s - b.start_s, pps),
                          );
                          const playing =
                            currentTimeS >= b.start_s && currentTimeS < b.end_s;
                          return (
                            <Button
                              key={b.id}
                              type="button"
                              variant="ghost"
                              aria-label={`Caption at ${formatTimecode(b.start_s)}, ${b.text.slice(0, 40)}`}
                              onClick={() => onOpenCaptionCue?.(b.id)}
                              style={{ left, width, top: 4, height: 12 }}
                              className={`absolute h-auto w-auto rounded-sm p-0 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-lime-500 ${
                                playing ? "bg-lime-600 hover:bg-lime-600" : "bg-[#0c0c0e]/70 hover:bg-[#0c0c0e]"
                              }`}
                            />
                          );
                        })}
                  </div>
                </LaneTrack>
              )}

              {/* ── Visual replacement blocks, below authored text ── */}
              {showVisualBlocks && <LaneTrack
                trackW={trackW}
                heightPx={visualLane.totalHeightPx}
                testId="editor-visuals-lane"
              >
                <Playline currentTimeS={currentTimeS} playbackClock={playbackClock} pps={pps} />
                {visualBlocks.length === 0 ? (
                  <GhostRow text="Montages and text cards appear here" />
                ) : (
                  visualLane.rows.map(
                    ({ item: block, rowIndex, topPx, heightPx }) => {
                      const left = secondsToPx(block.start_s, pps);
                      const width = Math.max(
                        8,
                        secondsToPx(block.end_s - block.start_s, pps),
                      );
                      const selected = isSel("visual", block.id);
                      return (
                        <BarButton
                          key={block.id}
                          left={left}
                          width={width}
                          top={topPx}
                          height={heightPx}
                          selected={selected}
                          ringCls={ringCls}
                          ariaLabel={`${block.kind === "montage" ? "Montage" : "Text card"}, ${formatTimecode(block.start_s)}–${formatTimecode(block.end_s)}`}
                          onSelect={() => onSelect("visual", block.id)}
                          dataKind="visual"
                          dataId={block.id}
                          dataRowIndex={rowIndex}
                          onPointerDown={
                            visualBlocksReadOnly
                              ? undefined
                              : (event) => startVisualDrag(event, block)
                          }
                          onPointerMove={(event) =>
                            updateDrag(event.clientX)
                          }
                          onPointerUp={(event) =>
                            finishDrag(event, "visual", block.id)
                          }
                          onPointerCancel={cancelDrag}
                          suppressClickRef={suppressClickRef}
                          showTrimHandles={!visualBlocksReadOnly}
                          title={
                            visualBlocksReadOnly
                              ? (visualBlocksDisabledReason ?? undefined)
                              : undefined
                          }
                          className="border border-lime-200 bg-lime-50 text-lime-800"
                        >
                          <span className="pointer-events-none truncate px-2 text-[10px] font-semibold">
                            {block.kind === "montage" ? "Montage" : "Text card"}
                          </span>
                        </BarButton>
                      );
                    },
                  )
                )}
              </LaneTrack>}

              {showMotionBlocks && (
                <LaneTrack
                  trackW={trackW}
                  heightPx={motionLane.totalHeightPx}
                  testId="editor-motion-lane"
                >
                  <Playline currentTimeS={currentTimeS} playbackClock={playbackClock} pps={pps} />
                  {motionBlocks.length === 0 ? (
                    <GhostRow text="Creator Blocks appear here" />
                  ) : (
                    motionLane.rows.map(({ item: block, rowIndex, topPx, heightPx }) => {
                      const left = secondsToPx(block.start_s, pps);
                      const width = Math.max(8, secondsToPx(block.end_s - block.start_s, pps));
                      return (
                        <BarButton
                          key={block.id}
                          left={left}
                          width={width}
                          top={topPx}
                          height={heightPx}
                          selected={isSel("motion", block.id)}
                          ringCls={ringCls}
                          ariaLabel={`${block.label}, ${formatTimecode(block.start_s)}–${formatTimecode(block.end_s)}`}
                          onSelect={() => onSelect("motion", block.id)}
                          dataKind="motion"
                          dataId={block.id}
                          dataRowIndex={rowIndex}
                          onPointerDown={
                            block.readOnly || motionBlocksReadOnly
                              ? undefined
                              : (event) => startMotionDrag(event, block)
                          }
                          onPointerMove={(event) => updateDrag(event.clientX)}
                          onPointerUp={(event) => finishDrag(event, "motion", block.id)}
                          onPointerCancel={cancelDrag}
                          suppressClickRef={suppressClickRef}
                          showTrimHandles={!block.readOnly && !motionBlocksReadOnly}
                          title={
                            motionBlocksReadOnly
                              ? (motionBlocksDisabledReason ?? undefined)
                              : undefined
                          }
                          className="border border-lime-300 bg-lime-50 text-[#3f3f46]"
                        >
                          <span className="pointer-events-none truncate px-2 text-[10px] font-semibold">{block.label}</span>
                        </BarButton>
                      );
                    })
                  )}
                </LaneTrack>
              )}

              {cameraEffects.length > 0 && (
                <LaneTrack
                  trackW={trackW}
                  heightPx={cameraLane.totalHeightPx}
                  testId="editor-camera-lane"
                >
                  <Playline currentTimeS={currentTimeS} playbackClock={playbackClock} pps={pps} />
                  {cameraLane.rows.map(
                    ({ item: effect, rowIndex, topPx, heightPx }) => {
                      const left = secondsToPx(effect.start_s, pps);
                      const width = Math.max(
                        8,
                        secondsToPx(effect.end_s - effect.start_s, pps),
                      );
                      const selected = isSel("camera", effect.id);
                      return (
                        <BarButton
                          key={effect.id}
                          left={left}
                          width={width}
                          top={topPx}
                          height={heightPx}
                          selected={selected}
                          ringCls={ringCls}
                          ariaLabel={`Camera focus, ${formatTimecode(effect.start_s)}–${formatTimecode(effect.end_s)}`}
                          onSelect={() => onSelect("camera", effect.id)}
                          dataKind="camera"
                          dataId={effect.id}
                          dataRowIndex={rowIndex}
                          onPointerDown={(event) => startCameraDrag(event, effect)}
                          onPointerMove={(event) => updateDrag(event.clientX)}
                          onPointerUp={(event) => finishDrag(event, "camera", effect.id)}
                          onPointerCancel={cancelDrag}
                          suppressClickRef={suppressClickRef}
                          showTrimHandles
                          className="border border-sky-200 bg-sky-50 text-sky-800"
                        >
                          <span className="pointer-events-none truncate px-2 text-[10px] font-semibold">
                            Focus
                          </span>
                        </BarButton>
                      );
                    },
                  )}
                </LaneTrack>
              )}

              {/* ── Video lane (Clips + filmstrip) ── */}
              <LaneTrack
                trackW={trackW}
                heightPx={videoLaneHeight}
                onDragOver={handleCarouselDragOver}
                onDrop={handleCarouselDrop}
              >
                <Playline currentTimeS={currentTimeS} playbackClock={playbackClock} pps={pps} />
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
                    const flashing = flashIds?.has(slot.key) ?? false;
                    const strip = filmstripByKey.get(slot.key);
                    const stripSlot = strip?.slot ?? slot;
                    const stripWin = strip?.win ?? win;
                    const source = filmstripSourceByIndex.get(
                      stripSlot.clipIndex,
                    );
                    return (
                      <Button
                        key={slot.key}
                        type="button"
                        variant="ghost"
                        aria-label={`Clip ${i + 1}, timeline ${formatTimecode(win.startS)}–${formatTimecode(win.startS + win.durationS)}, source ${slot.inS.toFixed(1)}–${(slot.inS + win.durationS).toFixed(1)}`}
                        aria-pressed={selected}
                        data-editor-bar-kind="clip"
                        data-editor-bar-id={slot.key}
                        title={clipReadOnly ? (clipDisabledReason ?? "Clip timing is locked") : undefined}
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
                          "group absolute h-auto min-w-11 justify-start overflow-hidden rounded border bg-zinc-200 p-0 transition-colors hover:bg-zinc-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500",
                          unusedFilmstripClips.length > 0 ? "top-0.5 h-11" : "inset-y-0.5",
                          clipReadOnly
                            ? "cursor-default active:cursor-default"
                            : "cursor-grab active:cursor-grabbing",
                          selected
                            ? `border-transparent ${ringCls}`
                            : "border-white/50 hover:border-white",
                          flashing
                            ? "outline outline-2 outline-offset-[1px] outline-lime-500 motion-safe:animate-pulse"
                            : "",
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
                        <TimelineEdgeHitZones width={width} />
                        <TimelineTrimHandle side="left" selected={selected} />
                        <TimelineTrimHandle side="right" selected={selected} />
                      </Button>
                    );
                  })
                )}
                {unusedFilmstripClips.length > 0 && (
                  <div
                    className="absolute inset-x-1 bottom-1 flex h-6 items-center gap-1 overflow-x-auto"
                    aria-label="Unused uploaded clips"
                  >
                    <span className="sticky left-0 z-10 shrink-0 bg-white/90 px-1 text-[9px] font-semibold uppercase tracking-wider text-zinc-500">
                      {allowRepeatedSources ? "Sources" : "Unused"}
                    </span>
                    {unusedFilmstripClips.map((source) => (
                      <Button
                        key={source.clip_index}
                        type="button"
                        variant="outline"
                        aria-label={`Add source clip ${source.clip_index + 1} to timeline`}
                        disabled={(clipAddReadOnly ?? clipReadOnly) || !onAddClip}
                        title={
                          (clipAddReadOnly ?? clipReadOnly)
                            ? (clipAddDisabledReason ?? clipDisabledReason ?? "Clip timing is locked")
                            : `Add Clip ${source.clip_index + 1} to the end`
                        }
                        onClick={(event) => {
                          event.stopPropagation();
                          onAddClip?.(source.clip_index);
                        }}
                        className="h-6 shrink-0 rounded border-dashed border-zinc-300 bg-white px-2 text-[9px] font-semibold text-[#3f3f46] hover:border-lime-500 hover:bg-white hover:text-lime-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-45"
                      >
                        + Clip {source.clip_index + 1}
                      </Button>
                    ))}
                  </div>
                )}
                {carouselBlock && carouselWindow && (
                  <div
                    role="button"
                    tabIndex={0}
                    aria-label={`Carousel block, ${carouselBlock.effectLabel}, ${carouselWindow.durationS.toFixed(1)}s, ${carouselBlock.position}`}
                    aria-pressed={isSel("carousel", carouselBlock.id)}
                    data-editor-bar-kind="carousel"
                    data-editor-bar-id={carouselBlock.id}
                    draggable={!carouselReadOnly}
                    onDragStart={(e) => {
                      if (carouselReadOnly) {
                        e.preventDefault();
                        return;
                      }
                      e.dataTransfer.effectAllowed = "move";
                      e.dataTransfer.setData("text/plain", carouselBlock.id);
                    }}
                    onPointerMove={(event) => updateDrag(event.clientX)}
                    onPointerUp={(event) => finishDrag(event, "carousel", carouselBlock.id)}
                    onPointerCancel={cancelDrag}
                    onClick={(e) => {
                      e.stopPropagation();
                      onSelectCarousel?.();
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onSelectCarousel?.();
                      }
                    }}
                    className={`absolute flex min-w-11 items-center overflow-hidden rounded border border-zinc-600 bg-[#0c0c0e] px-2 text-[10px] font-semibold text-white shadow-sm transition-colors hover:border-zinc-300 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
                      unusedFilmstripClips.length > 0 ? "top-0.5 h-11" : "inset-y-0.5"
                    } ${
                      carouselReadOnly
                        ? "cursor-default opacity-70"
                        : "cursor-grab active:cursor-grabbing"
                    } ${
                      isSel("carousel", carouselBlock.id) ? ringCls : ""
                    }`}
                    style={{
                      left: secondsToPx(carouselWindow.startS, pps),
                      width: Math.max(8, secondsToPx(carouselWindow.durationS, pps)),
                    }}
                    title={
                      carouselReadOnly
                        ? (carouselDisabledReason ?? "Carousel timing is unavailable for this edit")
                        : "Drag onto the video lane (left/middle/right third) to move it"
                    }
                  >
                    <span className="truncate drop-shadow">
                      Carousel · {carouselBlock.effectLabel}
                    </span>
                    {!carouselReadOnly && (
                      <>
                        <span
                          aria-hidden
                          data-carousel-resize-handle="left"
                          onPointerDown={(e) => startCarouselResize(e, "left")}
                          className="absolute inset-y-0 left-0 z-[9] w-3 cursor-ew-resize"
                        />
                        <span
                          aria-hidden
                          data-carousel-resize-handle="right"
                          onPointerDown={(e) => startCarouselResize(e, "right")}
                          className="absolute inset-y-0 right-0 z-[9] w-3 cursor-ew-resize"
                        />
                        <TimelineTrimHandle side="left" selected={isSel("carousel", carouselBlock.id)} />
                        <TimelineTrimHandle side="right" selected={isSel("carousel", carouselBlock.id)} />
                      </>
                    )}
                  </div>
                )}
              </LaneTrack>

              {/* ── Sound lane (SFX sub-row above the music bed) ── */}
              <LaneTrack trackW={trackW} heightPx={soundLaneHeight}>
                <Playline currentTimeS={currentTimeS} playbackClock={playbackClock} pps={pps} />
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
                    <Button
                      type="button"
                      variant="outline"
                      disabled={sfxReadOnly}
                      title={sfxReadOnly ? (sfxDisabledReason ?? undefined) : undefined}
                      onClick={(e) => {
                        e.stopPropagation();
                        onOpenSounds?.();
                      }}
                      className="absolute left-1 bottom-0.5 top-0.5 h-auto rounded border-dashed border-zinc-300 bg-transparent px-2 text-[10px] text-zinc-500 hover:border-zinc-400 hover:bg-transparent hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                    >
                      + Add sounds
                    </Button>
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
                          onPointerDown={sfxReadOnly ? undefined : (e) => startSfxDrag(e, s)}
                          onPointerMove={(e) => updateDrag(e.clientX)}
                          onPointerUp={(e) => finishDrag(e, "sfx", s.id)}
                          onPointerCancel={cancelDrag}
                          suppressClickRef={suppressClickRef}
                          showTrimHandles={!sfxReadOnly}
                          title={sfxReadOnly ? (sfxDisabledReason ?? undefined) : undefined}
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
                      width={secondsToPx(effectiveDurationS, pps)}
                      selected={isSel("music", "bed")}
                      ringCls={ringCls}
                      ariaLabel={`Music bed ${musicLabel ?? ""}`}
                      onSelect={() => onSelect("music", "bed")}
                      dataKind="music"
                      dataId="bed"
                      title={soundBedTitle ?? "The song auto-fits your cut"}
                      draggable={false}
                      className="inset-y-0.5 border border-zinc-300/70 bg-zinc-200/70 text-[#52525b]"
                    >
                      <span className="pointer-events-none flex items-center gap-1 truncate px-2 text-[10px]">
                        <span aria-hidden>♫</span>
                        <span className="truncate">
                          {soundBedLabel ?? musicLabel ?? "Music"}
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
                <Playline currentTimeS={currentTimeS} playbackClock={playbackClock} pps={pps} />
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
                        const flashing = flashIds?.has(o.id) ?? false;
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
                            onPointerDown={
                              overlaysReadOnly ? undefined : (e) => startOverlayDrag(e, o)
                            }
                            onPointerMove={(e) => updateDrag(e.clientX)}
                            onPointerUp={(e) => finishDrag(e, "overlay", o.id)}
                            onPointerCancel={cancelDrag}
                            suppressClickRef={suppressClickRef}
                            showTrimHandles={!overlaysReadOnly}
                            title={
                              overlaysReadOnly ? (overlaysDisabledReason ?? undefined) : undefined
                            }
                            flashing={flashing}
                            className={
                              o.suggested
                                ? "border-[1.5px] border-dashed border-lime-600 bg-white text-[#0c0c0e]"
                                : "border border-zinc-300 bg-white text-[#0c0c0e]"
                            }
                          >
                            <span className="pointer-events-none truncate px-2 text-[10px]">
                              {o.suggested && <span aria-hidden>✦ </span>}
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
  currentTimeS,
  playbackClock,
  pps,
  withHead = false,
}: {
  currentTimeS: number;
  playbackClock?: EditorPlaybackClock | null;
  pps: number;
  withHead?: boolean;
}) {
  const playbackTimeS = useEditorPlaybackTime(playbackClock, currentTimeS);
  return (
    <div
      className="pointer-events-none absolute top-0 bottom-0 z-20 w-px bg-[#0c0c0e]/80"
      style={{ left: secondsToPx(playbackTimeS, pps) }}
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
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={`${muteState.title} ${muteState.muted ? "muted" : "audible"}`}
          aria-pressed={muteState.muted}
          title={
            muteState.muted
              ? `${muteState.title}: muted`
              : `${muteState.title}: audible`
          }
          onClick={muteState.onToggle}
          className={`h-11 w-11 flex-shrink-0 rounded text-[10px] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
            muteState.muted
              ? "text-zinc-300"
              : "text-[#3f3f46] hover:bg-zinc-100"
          }`}
        >
          {muteState.muted ? "🔇" : "🔊"}
        </Button>
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
  onDragOver,
  onDrop,
}: {
  trackW: number;
  heightPx: number;
  testId?: string;
  children: React.ReactNode;
  onDragOver?: React.DragEventHandler<HTMLDivElement>;
  onDrop?: React.DragEventHandler<HTMLDivElement>;
}) {
  return (
    <div
      className="relative overflow-hidden border-b border-zinc-200 bg-zinc-50"
      style={{ width: trackW, height: heightPx }}
      data-testid={testId}
      onDragOver={onDragOver}
      onDrop={onDrop}
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
  title,
  draggable,
  flashing = false,
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
  title?: string;
  draggable?: boolean;
  flashing?: boolean;
  className: string;
  children: React.ReactNode;
}) {
  const positionedInRow = top != null && height != null;
  return (
    <Button
      type="button"
      variant="ghost"
      aria-label={ariaLabel}
      aria-pressed={selected}
      data-editor-bar-kind={dataKind}
      data-editor-bar-id={dataId}
      data-editor-row-index={dataRowIndex}
      data-editor-text-row-index={dataKind === "text" ? dataRowIndex : undefined}
      title={title}
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
        "group absolute h-auto min-w-11 items-center justify-start gap-0 rounded p-0 transition-[filter,outline-color] hover:bg-transparent focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500",
        draggable === false
          ? "cursor-default"
          : "cursor-grab hover:brightness-110 active:cursor-grabbing",
        positionedInRow ? "" : "inset-y-0.5",
        selected ? ringCls : "",
        flashing ? "outline outline-2 outline-offset-[1px] outline-lime-500 motion-safe:animate-pulse" : "",
        className,
      ].join(" ")}
      style={{
        left,
        width,
        ...(positionedInRow ? { top, height } : {}),
      }}
    >
      {children}
      {showTrimHandles && <TimelineEdgeHitZones width={width} />}
      {showTrimHandles && (
        <>
          <TimelineTrimHandle side="left" selected={selected} />
          <TimelineTrimHandle side="right" selected={selected} />
        </>
      )}
    </Button>
  );
}

function TimelineEdgeHitZones({ width }: { width: number }) {
  const edgeWidth = effectiveBarEdgeHitPx(width, BAR_EDGE_HIT_PX);
  return (
    <>
      <span
        aria-hidden
        className="absolute inset-y-0 left-0 z-[9] cursor-ew-resize"
        style={{ width: edgeWidth }}
      />
      <span
        aria-hidden
        className="absolute inset-y-0 right-0 z-[9] cursor-ew-resize"
        style={{ width: edgeWidth }}
      />
    </>
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
      className={`pointer-events-none absolute top-1/2 z-10 flex h-8 w-2 -translate-y-1/2 cursor-ew-resize items-center justify-center rounded-sm bg-white/95 shadow-sm ring-1 ring-black/10 motion-safe:transition-opacity motion-safe:duration-150 ${
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
