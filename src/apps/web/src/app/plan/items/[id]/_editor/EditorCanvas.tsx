"use client";

/**
 * EditorCanvas — the center output-format preview of the editor shell.
 *
 * Renders the text-free base video with overlay text from the LOCAL working
 * bars (bug #6 fix: the editor's working state feeds the overlay, never the
 * server's variant.text_elements directly), plus the selection/manipulation
 * layer per plan §3/§5:
 *  - selection box (lime stroke) + 4 corner handles (white core, 1px ink halo)
 *  - drag = move (x_frac / y_frac), corner-drag = scale (size_px)
 *  - click video/empty = deselect; overlap hit-test topmost + click-cycling
 *  - double-click focuses the inspector textarea (select-all) — no
 *    contenteditable on canvas, ever
 *  - hover: cursor pointer + 1px zinc-400/60 ghost outline
 *  - selecting during playback never pauses
 * Fullscreen button bottom-right ONLY — the "Basic mode" pill is CUT (D7).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type {
  CameraEffect,
  MediaOverlay,
  PlanItemVariant,
  PoolAsset,
  SoundEffectPlacement,
  TextElement,
  VisualBlock,
} from "@/lib/plan-api";
import type {
  CarouselMoment,
  LookAdjustments,
  LookPreset,
  TimelineClip,
} from "@/lib/generative-api";
import CarouselBlockPreview from "./CarouselBlockPreview";
import { mapVirtualTime, transitionPreviewAtTime } from "./virtual-timeline";
import { lookPreviewStyles } from "@/lib/look-presets";
import { cameraScaleAt } from "@/lib/camera-effects";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import {
  resolveTextElementsLayout,
  CANVAS_W,
  CANVAS_H,
  MAX_LINE_W_FRAC,
  MAX_WIDTH_FRAC_MAX,
  MAX_WIDTH_FRAC_MIN,
  resolveTextElementYFrac,
} from "@/lib/overlay-layout";
import { isCaptionBar } from "./editor-bars";
import {
  animationStateAt,
  DISSOLVE_OUT_PARAMS,
  dissolveOutAlphaAt,
  dissolveOutDisplacementScaleAt,
  dissolveOutProgressAt,
  dissolveOutTransformScaleAt,
  normalizeAnimatedRevealText,
  sequenceOverlayFadeOutAlphaAt,
  staggeredSlicePreviewVisibleAt,
  themeTransitionStateAt,
} from "@/lib/overlay-animation";
import { INTRO_FONTS, MAX_INTRO_S, type OverlayCanvas } from "@/lib/overlay-constants";
import { smoothTypeLineProgresses, textMotionPreviewDurationS } from "@/lib/text-motion-v2";
import { StableVideo, stableVideoSourceIdentity } from "@/components/StableVideo";
import { useSfxPreview } from "@/app/plan/_components/useSfxPreview";
import {
  TextElementOverlayContent,
  smoothTypePreviewLayout,
  textElementContentStyle,
  textElementWrapperStyle,
  useSmoothTypeFontRevision,
} from "../components/TextElementOverlayLayer";

const TEXT_MOTION_V2_UI_ENABLED =
  process.env.NEXT_PUBLIC_TEXT_MOTION_V2_ENABLED === "true";
import { StaggeredSliceText } from "@/components/variant-editor/StaggeredSliceText";
import {
  clampMediaOverlayPosition,
  clampMediaOverlayScale,
  EDITOR_STAGE_Z,
  mediaOverlayStackZIndex,
  visibleMediaOverlaysAtTime,
  type VisibleMediaOverlay,
} from "./editor-media-overlays";
import { cycleHit } from "./useEditorSelection";
import type { VirtualPreviewController } from "./useVirtualPreview";
import {
  collageMotionForTextBar,
  masonryBoardXFrac,
  masonryLayerPositionForBoardX,
  masonryMotionOffsetFrac,
} from "./editor-smart-placement";
import VisualBlocksLayer from "./VisualBlocksLayer";
import MotionCanvasLayer from "./MotionCanvasLayer";
import type { MotionPresetInstance } from "@nova/motion-runtime";
import {
  useEditorPlaybackTime,
  type EditorPlaybackClock,
} from "./editor-playback-clock";

/** Min/max font size (1080×1920 canvas px) reachable via corner-drag scale.
 * Wider than the inspector's INTRO_SIZE envelope on purpose — the canvas can
 * host non-intro roles whose sizes exceed it; the server clamps regardless. */
const SCALE_MIN_PX = 24;
const SCALE_MAX_PX = 250;
const DEFAULT_CANVAS = { w: CANVAS_W, h: CANVAS_H };
const DISSOLVE_PREVIEW_FILTER_ID = "nova-editor-dissolve-preview";
const DEFAULT_CAPTION_SIZE_PX = 78;
const DEFAULT_CAPTION_COLOR = "#FFFFFF";
const DEFAULT_CAPTION_HIGHLIGHT_COLOR = "#A3E635";
const DEFAULT_CAPTION_STROKE_WIDTH = 2;

function PlaybackFrame({
  clock,
  fallbackTimeS,
  children,
}: {
  clock?: EditorPlaybackClock | null;
  fallbackTimeS: number;
  children: (timeS: number) => React.ReactNode;
}) {
  const timeS = useEditorPlaybackTime(clock, fallbackTimeS);
  return children(timeS);
}

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(media.matches);
    update();
    if (typeof media.addEventListener === "function") {
      media.addEventListener("change", update);
      return () => media.removeEventListener("change", update);
    }
    media.addListener(update);
    return () => media.removeListener(update);
  }, []);
  return reduced;
}

function captionPreviewShadow(strokePx: number, shadowEnabled: boolean): string | undefined {
  const shadows: string[] = [];
  if (shadowEnabled) {
    shadows.push(`0 ${Math.max(1, strokePx)}px ${Math.max(2, strokePx * 1.5)}px #000`);
  }
  if (strokePx > 0.1) {
    const spread = Math.max(1, strokePx);
    shadows.push(
      `${spread}px 0 0 #000`,
      `-${spread}px 0 0 #000`,
      `0 ${spread}px 0 #000`,
      `0 -${spread}px 0 #000`,
      `${spread}px ${spread}px 0 #000`,
      `-${spread}px ${spread}px 0 #000`,
      `${spread}px -${spread}px 0 #000`,
      `-${spread}px -${spread}px 0 #000`,
    );
  }
  return shadows.length ? shadows.join(", ") : undefined;
}

/** Pointer movement (px) under which a pointerdown+up counts as a CLICK
 * (triggers overlap cycling) rather than a drag. Finger taps wobble 5–15px
 * between down and up, so touch gets a wider slop — 3px would turn most
 * select-taps into accidental micro-drags that commit a position patch. */
const CLICK_SLOP_PX = 3;
const TOUCH_CLICK_SLOP_PX = 10;

function slopForPointer(e: React.PointerEvent): number {
  return e.pointerType === "touch" ? TOUCH_CLICK_SLOP_PX : CLICK_SLOP_PX;
}

interface DragState {
  target: "text" | "overlay";
  mode: "move" | "scale" | "width";
  id: string;
  startClientX: number;
  startClientY: number;
  /** move: starting fracs */
  startXFrac: number;
  startYFrac: number;
  /** scale: starting size + distance from element center */
  startSizePx: number;
  startScale: number;
  startDist: number;
  centerClientX: number;
  centerClientY: number;
  startWidthFrac: number;
  startHeightFrac: number;
  startMaxWidthFrac: number;
  widthSide: "left" | "right" | null;
  moved: boolean;
  /** Click-vs-drag slop for THIS gesture (pointer-type aware). */
  slopPx: number;
  /** Hits (topmost first) captured at pointerdown — used for click-cycling. */
  hits: string[];
}

type DragOverride =
  | {
      target: "text";
      id: string;
      x_frac?: number;
      y_frac?: number;
      size_px?: number;
      max_width_frac?: number;
      layer_origin_px?: number;
    }
  | {
      target: "overlay";
      id: string;
      x_frac?: number;
      y_frac?: number;
      scale?: number;
    };

export default function EditorCanvas({
  variant,
  elements,
  bars,
  mediaOverlays = [],
  visualBlocks = [],
  motionScenes = [],
  motionRuntimeHash,
  cameraEffects = [],
  visualAssets = [],
  overlayPreviewUrls = {},
  suggestedOverlayIds,
  sfxPlacements = [],
  sfxAudioUrls = {},
  selectedTextId,
  selectedOverlayId,
  flashTextIds,
  flashOverlayIds,
  currentTime: committedCurrentTime,
  playbackClock,
  lookPreset = "none",
  lookAdjustments = null,
  virtualDeckLookPresets = { a: "none", b: "none" },
  virtualDeckLookAdjustments = { a: null, b: null },
  playing = false,
  masonryDurationS,
  zoomPct,
  tool,
  videoRef,
  onSelectText,
  onSelectOverlay,
  captionTapSelect = false,
  onClearSelection,
  onPatchBar,
  onPatchOverlay,
  onFocusContent,
  onTimeUpdate,
  onDuration,
  onPlayingChange,
  onReloadSource,
  virtualPreview,
  carouselMoment,
  carouselClips = [],
  allowManipulation = true,
  stageHeightCss,
  captionsEnabled,
  canvas = DEFAULT_CANVAS,
}: {
  variant: PlanItemVariant;
  /** Working bars projected to API shape (barsToTextElements) — layout input. */
  elements: TextElement[];
  /** The raw working bars, for style fields the layout doesn't carry. */
  bars: TextElementBar[];
  mediaOverlays?: MediaOverlay[];
  visualBlocks?: VisualBlock[];
  motionScenes?: MotionPresetInstance[];
  motionRuntimeHash?: string | null;
  cameraEffects?: CameraEffect[];
  visualAssets?: PoolAsset[];
  overlayPreviewUrls?: Record<string, string>;
  /** Overlay ids that came from ✓-accepted AI suggestions — dashed ✦
   *  provenance outline until Save (never stored on MediaOverlay itself). */
  suggestedOverlayIds?: Set<string>;
  sfxPlacements?: SoundEffectPlacement[];
  sfxAudioUrls?: Record<string, string>;
  selectedTextId: string | null;
  selectedOverlayId?: string | null;
  flashTextIds?: Set<string>;
  flashOverlayIds?: Set<string>;
  currentTime: number;
  /** Output-timeline frame clock. Only this authored preview layer subscribes;
   * the editor shell keeps using committed transport time. */
  playbackClock?: EditorPlaybackClock | null;
  /** Close CSS approximation; the saved FFmpeg render is authoritative. */
  lookPreset?: LookPreset;
  lookAdjustments?: LookAdjustments | null;
  virtualDeckLookPresets?: Record<"a" | "b", LookPreset>;
  virtualDeckLookAdjustments?: Record<"a" | "b", LookAdjustments | null>;
  playing?: boolean;
  /** Current preview/render duration used by the masonry board pan. */
  masonryDurationS: number;
  zoomPct: number;
  tool: "select" | "pan";
  /** Owned by the shell so the future TransportBar can drive the same video. */
  videoRef: React.RefObject<HTMLVideoElement>;
  onSelectText: (id: string) => void;
  onSelectOverlay?: (id: string) => void;
  /** Pocket editor only: the caption preview becomes a tap target that selects
   * the current cue's bar (desktop keeps it pointer-events-none). Tap-select
   * only — captions never gain drag/scale handles. */
  captionTapSelect?: boolean;
  onClearSelection: () => void;
  onPatchBar: (id: string, patch: Partial<Omit<TextElementBar, "id" | "role">>) => void;
  onPatchOverlay?: (id: string, patch: Partial<MediaOverlay>) => void;
  /** Double-click contract: focus the inspector content textarea, select-all. */
  onFocusContent: () => void;
  onTimeUpdate: (t: number) => void;
  onDuration: (d: number) => void;
  /** Lifts play/pause state to the shell so the TransportBar can mirror it. */
  onPlayingChange?: (playing: boolean) => void;
  /** Re-fetch the variant (re-signs an expired preview URL) on the error tile's
   * Retry — the shell re-runs getPlanItem (plan §9 canvas error state). */
  onReloadSource?: () => void;
  virtualPreview?: VirtualPreviewController | null;
  /** Staged/persisted carousel-moment config — drives the placeholder block
   *  preview (CarouselBlockPreview) mounted when the playhead is inside its
   *  spliced window on `virtualPreview.timeline`. */
  carouselMoment?: CarouselMoment | null;
  /** The variant's clips, in timeline order — SAME shape and array the
   *  virtual-preview transport uses (`useVirtualPreview`'s `clips` option),
   *  forwarded straight through to the carousel block renderer. Array order
   *  is card order (index i -> card i). */
  carouselClips?: Pick<TimelineClip, "clip_index" | "signed_url">[];
  /** Light-edit mode keeps the canvas tap-only: no drag, scale, or handles. */
  allowManipulation?: boolean;
  /** Shell-specific chrome height for sizing the 9:16 stage. */
  stageHeightCss?: string;
  /**
   * LOCAL subtitles on/off from the editing session, which wins over the
   * server's `variant.captions_enabled`.
   *
   * Load-bearing for the Captions drawer's Subtitles switch: the server flag
   * only changes on Save, so reading it alone leaves the canvas drawing
   * captions after the user has turned them off — the preview would contradict
   * the control that was just toggled. Undefined = no local override yet, fall
   * back to the server value (every non-caption caller).
   */
  captionsEnabled?: boolean;
  /** Output canvas dimensions used for layout projection. Defaults to portrait. */
  canvas?: OverlayCanvas;
}) {
  // The canvas shell stays on committed time. Only PlaybackFrame and the
  // dedicated authored layers below subscribe to decoded-frame cadence.
  const currentTime = committedCurrentTime;
  const viewportRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const overlayRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const mediaOverlayRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const virtualDeckAContainerRef = useRef<HTMLDivElement>(null);
  const virtualDeckBContainerRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const panRef = useRef<{ x: number; y: number; left: number; top: number } | null>(null);
  const emptyVideoRef = useRef<HTMLVideoElement | null>(null);
  const renderedIdentityRef = useRef<string | null | undefined>(undefined);

  const [stageSize, setStageSize] = useState({ w: 0, h: 0 });
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [hoveredOverlayId, setHoveredOverlayId] = useState<string | null>(null);
  // Canvas video states (plan §9): shimmer while the frame under the playhead
  // isn't decoded yet (scrub buffering); error tile on a load/expiry failure.
  const [buffering, setBuffering] = useState(false);
  const [videoError, setVideoError] = useState(false);
  // Transient per-drag override so a gesture is ONE history entry (the
  // PATCH_BAR dispatch happens on pointerup, not per pointermove).
  const [dragOverride, setDragOverride] = useState<DragOverride | null>(null);
  const reducedMotion = usePrefersReducedMotion();
  const settleAuthoredMotion = !playbackClock && reducedMotion;
  const renderedIdentity = variant.base_video_url
    ? stableVideoSourceIdentity(variant.base_video_url, variant.base_video_path ?? undefined)
    : `${variant.variant_id}:${variant.render_finished_at ?? ""}`;
  // Render-phase ref update closes the passive-effect gap on source swaps:
  // a queued callback from the prior identity is rejected even before the old
  // effect cleanup runs.
  renderedIdentityRef.current = renderedIdentity;

  // Rendered-preview mode has one stable video instead of the virtual dual
  // deck. Publish its decoded media time to the same output clock. Virtual
  // preview owns this when its deck controller is present.
  useEffect(() => {
    if (!playbackClock || virtualPreview || !playing) return;
    const video = videoRef.current;
    if (!video || typeof video.requestVideoFrameCallback !== "function") return;

    let live = true;
    let callbackId = 0;
    let generation = 0;
    const effectIdentity = renderedIdentity;
    const schedule = (expectedGeneration: number) => {
      const sample: VideoFrameRequestCallback = (_now, metadata) => {
        if (
          !live ||
          generation !== expectedGeneration ||
          renderedIdentityRef.current !== effectIdentity
        ) return;
        playbackClock.publish(metadata.mediaTime);
        schedule(expectedGeneration);
      };
      callbackId = video.requestVideoFrameCallback(sample);
    };
    const restart = () => {
      if (!live) return;
      generation += 1;
      if (callbackId) video.cancelVideoFrameCallback(callbackId);
      playbackClock.publish(video.currentTime);
      schedule(generation);
    };
    const recoverVisibility = () => {
      if (document.visibilityState === "visible") restart();
    };

    restart();
    video.addEventListener("seeked", restart);
    video.addEventListener("ratechange", restart);
    video.addEventListener("canplay", restart);
    document.addEventListener("visibilitychange", recoverVisibility);
    return () => {
      live = false;
      if (callbackId) video.cancelVideoFrameCallback(callbackId);
      video.removeEventListener("seeked", restart);
      video.removeEventListener("ratechange", restart);
      video.removeEventListener("canplay", restart);
      document.removeEventListener("visibilitychange", recoverVisibility);
    };
  }, [
    playbackClock,
    playing,
    videoRef,
    virtualPreview,
    renderedIdentity,
  ]);

  // Measure the stage so 1080×1920-scale px project onto the rendered box.
  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (r) setStageSize({ w: r.width, h: r.height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const layouts = useMemo(() => resolveTextElementsLayout(elements, canvas), [elements, canvas]);
  const barById = useMemo(() => new Map(bars.map((b) => [b.id, b])), [bars]);
  const smoothFontRevision = useSmoothTypeFontRevision(layouts);
  const smoothPreviewById = useMemo(() => {
    void smoothFontRevision;
    return new Map(
      layouts
        .filter((layout) => (barById.get(layout.id)?.effect ?? layout.effect) === "smooth-type")
        .map((layout) => [layout.id, smoothTypePreviewLayout(layout)]),
    );
  }, [barById, layouts, smoothFontRevision]);

  const visibleAt = (timeS: number) =>
    layouts.filter((layout) => {
      const tLocal = timeS - layout.start_s;
      const durationS = layout.end_s - layout.start_s;
      if ((barById.get(layout.id)?.effect ?? "static") === "staggered-slice") {
        return staggeredSlicePreviewVisibleAt(tLocal, durationS, playing);
      }
      return tLocal >= 0 && tLocal < durationS;
    });
  const captionPreviewUsesCleanBase = Boolean(variant.base_video_url || virtualPreview);
  const visibleCaptionAt = (timeS: number): {
    text: string;
    bar: TextElementBar;
    wordMode: boolean;
  } | null => {
    // Local session state wins: the drawer's Subtitles switch must clear the
    // preview on the same frame, not on Save.
    const captionsOn = captionsEnabled ?? variant.captions_enabled !== false;
    if (
      !captionPreviewUsesCleanBase ||
      (variant.resolved_archetype !== "subtitled" &&
        variant.resolved_archetype !== "narrated") ||
      !captionsOn ||
      !bars.some(isCaptionBar)
    ) {
      return null;
    }
    const bar = bars.filter(isCaptionBar).find(
      (candidate) => timeS >= candidate.start_s && timeS < candidate.end_s,
    );
    if (!bar) return null;
    const cueIndex = Number(bar.id.match(/^caption-(\d+)$/)?.[1]);
    const originalCue = Number.isFinite(cueIndex) ? variant.caption_cues?.[cueIndex] : null;
    const originalWords = originalCue?.words ?? [];
    const wordMode =
      variant.voiceover_caption_style === "word" &&
      originalWords.length > 0 &&
      originalCue?.text === bar.text;
    if (wordMode) {
      const activeWord = originalWords.find(
        (word) => timeS >= word.start_s && timeS < word.end_s,
      )?.text;
      return activeWord ? { text: activeWord, bar, wordMode: true } : null;
    }
    return { text: bar.text, bar, wordMode: false };
  };
  const captionPreviewStyleFor = (
    visibleCaption: ReturnType<typeof visibleCaptionAt>,
  ) => {
    const bar = visibleCaption?.bar;
    // Per-cue override BEFORE the variant-wide value, matching the inspector's
    // own precedence for the "This caption" section (`cue_* ?? global`).
    // Without the cue_* leg the override fields write state the preview never
    // renders, so changing this line's font/colour/size looks like a dead
    // control (shipped that way in the Lane PR-A caption split).
    const fontName =
      bar?.cue_font_family ?? bar?.font_family ?? variant.voiceover_caption_font;
    const selected = INTRO_FONTS.find((font) => font.name === fontName);
    const sizePx =
      bar?.cue_size_px ?? bar?.size_px ?? variant.caption_size_px ?? DEFAULT_CAPTION_SIZE_PX;
    const strokeWidth =
      bar?.stroke_width ?? variant.caption_stroke_width ?? DEFAULT_CAPTION_STROKE_WIDTH;
    const scaledStroke = stageSize.h > 0 ? (strokeWidth / canvas.h) * stageSize.h : 0;
    const shadowEnabled = bar?.shadow_enabled ?? variant.caption_shadow_enabled ?? true;
    return {
      bottomPct:
        typeof bar?.y_frac === "number"
          ? Math.max(0, Math.min(100, (1 - bar.y_frac) * 100))
          : ((variant.caption_margin_v ?? 384) / canvas.h) * 100,
      color:
        visibleCaption?.wordMode
          ? bar?.highlight_color ??
            variant.caption_highlight_color ??
            DEFAULT_CAPTION_HIGHLIGHT_COLOR
          : bar?.cue_text_color ??
            bar?.color ??
            variant.caption_text_color ??
            DEFAULT_CAPTION_COLOR,
      fontFamily: selected?.cssFamily ?? "'TikTok Sans', 'Inter', system-ui, sans-serif",
      fontSizePx: stageSize.h > 0 ? (sizePx / canvas.h) * stageSize.h : 0,
      textShadow: captionPreviewShadow(scaledStroke, shadowEnabled),
    };
  };

  const src = variant.base_video_url ?? variant.output_url ?? null;
  const hasPreview = Boolean(src || virtualPreview);
  const virtualVideoARef = virtualPreview?.videoAProps.ref;
  const virtualVideoBRef = virtualPreview?.videoBProps.ref;
  const virtualVideoAProps = virtualPreview
    ? { ...virtualPreview.videoAProps, ref: undefined }
    : null;
  const virtualVideoBProps = virtualPreview
    ? { ...virtualPreview.videoBProps, ref: undefined }
    : null;
  const virtualMusicAudioRef = virtualPreview?.musicAudioProps?.ref;
  const virtualMusicAudioProps = virtualPreview?.musicAudioProps
    ? { ...virtualPreview.musicAudioProps, ref: undefined }
    : null;
  // Carousel-block placeholder (Lane C): the playhead is inside a spliced
  // carousel entry on the virtual timeline — both decks are already paused
  // by useVirtualPreview's own gate (showMapping's `entry.kind !== "clip"`
  // branch), this just decides whether to paint the placeholder OVER them.
  const virtualFrameStateAt = (timeS: number) => {
    const carouselMapping = virtualPreview
      ? mapVirtualTime(virtualPreview.timeline, timeS)
      : null;
    const activeCarouselEntry =
      carouselMapping?.entry.kind === "carousel" ? carouselMapping.entry : null;
    const virtualTransition = virtualPreview
      ? transitionPreviewAtTime(virtualPreview.timeline, timeS)
      : null;
    const displayedCarouselEntry =
      activeCarouselEntry ?? virtualTransition?.carouselEntry ?? null;
    const transitionProgress = virtualTransition?.progress ?? 0;
    const transitionOverlayOpacity =
      virtualTransition?.kind === "dip_to_black" || virtualTransition?.kind === "flash"
        ? 1 - Math.abs(transitionProgress * 2 - 1)
        : 0;
    return {
      displayedCarouselEntry,
      transitionOverlayOpacity,
      transitionProgress,
      virtualTransition,
    };
  };
  const virtualDeckStyleAt = (
    deck: "a" | "b",
    virtualTransition: ReturnType<typeof transitionPreviewAtTime>,
    transitionProgress: number,
  ): React.CSSProperties => {
    const isActive = virtualPreview?.activeDeck === deck;
    if (virtualTransition?.carouselRole === "incoming") {
      return { opacity: isActive ? 1 : 0, zIndex: EDITOR_STAGE_Z.video };
    }
    if (virtualTransition?.carouselRole === "outgoing") {
      return { opacity: isActive ? 0 : 1, zIndex: EDITOR_STAGE_Z.video };
    }
    const opacity = virtualTransition
      ? isActive
        ? 1 - transitionProgress
        : transitionProgress
      : isActive
        ? 1
        : 0;
    return { opacity, zIndex: EDITOR_STAGE_Z.video };
  };
  const identity = renderedIdentity;

  useSfxPreview(
    videoRef,
    virtualPreview ? [] : sfxPlacements,
    sfxAudioUrls,
  );
  useSfxPreview(
    virtualVideoARef ?? emptyVideoRef,
    virtualPreview?.activeDeck === "a" ? sfxPlacements : [],
    sfxAudioUrls,
  );
  useSfxPreview(
    virtualVideoBRef ?? emptyVideoRef,
    virtualPreview?.activeDeck === "b" ? sfxPlacements : [],
    sfxAudioUrls,
  );

  // ── Pointer interactions ────────────────────────────────────────────────────

  function hitsAtPoint(clientX: number, clientY: number): string[] {
    // Topmost first: render order is array order (last-in-array = top).
    const out: Array<{ id: string; z: number }> = [];
    overlayRefs.current.forEach((el, id) => {
      if (!el) return;
      const r = el.getBoundingClientRect();
      if (clientX >= r.left && clientX <= r.right && clientY >= r.top && clientY <= r.bottom) {
        out.push({ id, z: layouts.findIndex((layout) => layout.id === id) });
      }
    });
    return out.sort((a, b) => b.z - a.z).map((h) => h.id);
  }

  function beginMove(e: React.PointerEvent, id: string, hits: string[]) {
    if (!allowManipulation) return;
    const bar = barById.get(id);
    if (bar?.role === "lyric_line") return;
    const layout = layouts.find((l) => l.id === id);
    if (!layout) return;
    const localXFrac = bar?.x_frac ?? layout.xFrac;
    const motion = collageMotionForTextBar(variant, masonryDurationS, bar);
    dragRef.current = {
      target: "text",
      mode: "move",
      id,
      startClientX: e.clientX,
      startClientY: e.clientY,
      startXFrac: motion ? masonryBoardXFrac(motion, localXFrac) : localXFrac,
      startYFrac: bar?.y_frac ?? layout.yFrac,
      startSizePx: layout.sizePx,
      startScale: 0,
      startDist: 0,
      centerClientX: 0,
      centerClientY: 0,
      startWidthFrac: 0,
      startHeightFrac: 0,
      startMaxWidthFrac: bar?.max_width_frac ?? layout.maxWidthFrac,
      widthSide: null,
      moved: false,
      slopPx: slopForPointer(e),
      hits,
    };
    (e.target as Element).setPointerCapture?.(e.pointerId);
  }

  function onOverlayPointerDown(e: React.PointerEvent, id: string) {
    if (tool !== "select" || e.button !== 0) return;
    e.stopPropagation();
    const hits = hitsAtPoint(e.clientX, e.clientY);
    if (!allowManipulation) {
      const next = cycleHit(hits, selectedTextId);
      if (next) onSelectText(next);
      return;
    }
    if (selectedTextId && hits.includes(selectedTextId)) {
      // Keep the current selection for dragging; a no-movement CLICK cycles
      // to the element underneath on pointerup (Figma/TikTok convention).
      beginMove(e, selectedTextId, hits);
    } else {
      // Fresh selection: select the topmost hit NOW and pass empty hits so
      // this same gesture's pointerup can't immediately cycle away from it.
      const next = cycleHit(hits, null);
      if (next) onSelectText(next);
      beginMove(e, next ?? id, []);
    }
  }

  function onHandlePointerDown(e: React.PointerEvent, id: string) {
    if (!allowManipulation || tool !== "select" || e.button !== 0) return;
    if (barById.get(id)?.role === "lyric_line") return;
    e.stopPropagation();
    const el = overlayRefs.current.get(id);
    const layout = layouts.find((l) => l.id === id);
    if (!el || !layout) return;
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const dist = Math.hypot(e.clientX - cx, e.clientY - cy) || 1;
    dragRef.current = {
      target: "text",
      mode: "scale",
      id,
      startClientX: e.clientX,
      startClientY: e.clientY,
      startXFrac: 0,
      startYFrac: 0,
      startSizePx: layout.sizePx,
      startScale: 0,
      startDist: dist,
      centerClientX: cx,
      centerClientY: cy,
      startWidthFrac: 0,
      startHeightFrac: 0,
      startMaxWidthFrac: barById.get(id)?.max_width_frac ?? layout.maxWidthFrac,
      widthSide: null,
      moved: false,
      slopPx: slopForPointer(e),
      hits: [],
    };
    (e.target as Element).setPointerCapture?.(e.pointerId);
  }

  function onWidthHandlePointerDown(
    e: React.PointerEvent,
    id: string,
    side: "left" | "right",
  ) {
    if (!allowManipulation || tool !== "select" || e.button !== 0) return;
    if (barById.get(id)?.role === "lyric_line") return;
    e.stopPropagation();
    const layout = layouts.find((l) => l.id === id);
    if (!layout) return;
    const bar = barById.get(id);
    dragRef.current = {
      target: "text",
      mode: "width",
      id,
      startClientX: e.clientX,
      startClientY: e.clientY,
      startXFrac: 0,
      startYFrac: 0,
      startSizePx: layout.sizePx,
      startScale: 0,
      startDist: 0,
      centerClientX: 0,
      centerClientY: 0,
      startWidthFrac: 0,
      startHeightFrac: 0,
      startMaxWidthFrac: bar?.max_width_frac ?? layout.maxWidthFrac,
      widthSide: side,
      moved: false,
      slopPx: slopForPointer(e),
      hits: [],
    };
    (e.target as Element).setPointerCapture?.(e.pointerId);
  }

  function beginMediaOverlayMove(e: React.PointerEvent<HTMLElement>, card: MediaOverlay) {
    if (!allowManipulation || tool !== "select" || e.button !== 0) return;
    e.stopPropagation();
    onSelectOverlay?.(card.id);
    const r = e.currentTarget.getBoundingClientRect();
    dragRef.current = {
      target: "overlay",
      mode: "move",
      id: card.id,
      startClientX: e.clientX,
      startClientY: e.clientY,
      startXFrac: card.x_frac,
      startYFrac: card.y_frac,
      startSizePx: 0,
      startScale: card.scale,
      startDist: 0,
      centerClientX: 0,
      centerClientY: 0,
      startWidthFrac: stageSize.w > 0 ? r.width / stageSize.w : card.scale,
      startHeightFrac: stageSize.h > 0 ? r.height / stageSize.h : card.scale,
      startMaxWidthFrac: MAX_LINE_W_FRAC,
      widthSide: null,
      moved: false,
      slopPx: slopForPointer(e),
      hits: [],
    };
    e.currentTarget.setPointerCapture?.(e.pointerId);
  }

  function beginMediaOverlayScale(e: React.PointerEvent<HTMLElement>, card: MediaOverlay) {
    if (!allowManipulation || tool !== "select" || e.button !== 0) return;
    e.stopPropagation();
    onSelectOverlay?.(card.id);
    const el = mediaOverlayRefs.current.get(card.id);
    if (!el) return;
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const dist = Math.hypot(e.clientX - cx, e.clientY - cy) || 1;
    dragRef.current = {
      target: "overlay",
      mode: "scale",
      id: card.id,
      startClientX: e.clientX,
      startClientY: e.clientY,
      startXFrac: card.x_frac,
      startYFrac: card.y_frac,
      startSizePx: 0,
      startScale: card.scale,
      startDist: dist,
      centerClientX: cx,
      centerClientY: cy,
      startWidthFrac: stageSize.w > 0 ? r.width / stageSize.w : card.scale,
      startHeightFrac: stageSize.h > 0 ? r.height / stageSize.h : card.scale,
      startMaxWidthFrac: MAX_LINE_W_FRAC,
      widthSide: null,
      moved: false,
      slopPx: slopForPointer(e),
      hits: [],
    };
    e.currentTarget.setPointerCapture?.(e.pointerId);
  }

  function onPointerMove(e: React.PointerEvent) {
    if (!allowManipulation) return;
    const drag = dragRef.current;
    if (!drag) return;
    const dx = e.clientX - drag.startClientX;
    const dy = e.clientY - drag.startClientY;
    if (Math.hypot(dx, dy) > drag.slopPx) drag.moved = true;
    if (!drag.moved) return;
    if (drag.mode === "move") {
      if (stageSize.w === 0 || stageSize.h === 0) return;
      if (drag.target === "overlay") {
        const next = clampMediaOverlayPosition({
          xFrac: drag.startXFrac + dx / stageSize.w,
          yFrac: drag.startYFrac + dy / stageSize.h,
          widthFrac: drag.startWidthFrac,
          heightFrac: drag.startHeightFrac,
        });
        setDragOverride({ target: "overlay", id: drag.id, ...next });
      } else {
        const yFrac = Math.min(0.98, Math.max(0.02, drag.startYFrac + dy / stageSize.h));
        const bar = barById.get(drag.id);
        const motion = collageMotionForTextBar(variant, masonryDurationS, bar);
        if (motion) {
          const position = masonryLayerPositionForBoardX(
            motion,
            drag.startXFrac + dx / stageSize.w,
          );
          setDragOverride({
            target: "text",
            id: drag.id,
            x_frac: position.xFrac,
            y_frac: yFrac,
            layer_origin_px: position.layerOriginPx,
          });
        } else {
          const xFrac = Math.min(0.98, Math.max(0.02, drag.startXFrac + dx / stageSize.w));
          setDragOverride({ target: "text", id: drag.id, x_frac: xFrac, y_frac: yFrac });
        }
      }
    } else if (drag.mode === "scale") {
      const dist = Math.hypot(e.clientX - drag.centerClientX, e.clientY - drag.centerClientY);
      const ratio = dist / drag.startDist;
      if (drag.target === "overlay") {
        setDragOverride({
          target: "overlay",
          id: drag.id,
          scale: clampMediaOverlayScale(drag.startScale * ratio),
        });
      } else {
        const size = Math.min(
          SCALE_MAX_PX,
          Math.max(SCALE_MIN_PX, Math.round(drag.startSizePx * ratio)),
        );
        setDragOverride({ target: "text", id: drag.id, size_px: size });
      }
    } else if (drag.target === "text" && drag.mode === "width") {
      if (stageSize.w === 0) return;
      const signedDelta = drag.widthSide === "left" ? -dx : dx;
      const maxWidthFrac = Math.min(
        MAX_WIDTH_FRAC_MAX,
        Math.max(MAX_WIDTH_FRAC_MIN, drag.startMaxWidthFrac + signedDelta / stageSize.w),
      );
      setDragOverride({ target: "text", id: drag.id, max_width_frac: maxWidthFrac });
    }
  }

  function onPointerUp() {
    if (!allowManipulation) return;
    const drag = dragRef.current;
    if (!drag) return;
    dragRef.current = null;
    if (drag.moved) {
      // Commit the gesture as ONE reducer mutation (one undo step later).
      if (
        drag.target === "text" &&
        drag.mode === "move" &&
        dragOverride?.target === "text" &&
        dragOverride.x_frac != null &&
        dragOverride.y_frac != null
      ) {
        const bar = barById.get(drag.id);
        const patch: Partial<Omit<TextElementBar, "id" | "role">> = {
          x_frac: dragOverride.x_frac,
          y_frac: dragOverride.y_frac,
          position: "custom",
        };
        if (dragOverride.layer_origin_px != null) {
          const sourceParams = { ...(bar?.source_params ?? {}) };
          const motion = {
            ...(collageMotionForTextBar(variant, masonryDurationS, bar) ?? {}),
            layer_origin_px: dragOverride.layer_origin_px,
          } as Record<string, unknown>;
          delete motion.pocket_left_px;
          delete motion.pocket_top_px;
          delete motion.pocket_right_px;
          delete motion.pocket_bottom_px;
          sourceParams.masonry_motion = motion;
          patch.source_params = sourceParams;
        }
        onPatchBar(drag.id, patch);
      } else if (
        drag.target === "text" &&
        drag.mode === "scale" &&
        dragOverride?.target === "text" &&
        dragOverride.size_px != null
      ) {
        onPatchBar(drag.id, { size_px: dragOverride.size_px, size_class: undefined });
      } else if (
        drag.target === "text" &&
        drag.mode === "width" &&
        dragOverride?.target === "text" &&
        dragOverride.max_width_frac != null
      ) {
        const bar = barById.get(drag.id);
        onPatchBar(drag.id, {
          max_width_frac: dragOverride.max_width_frac,
          position: "custom",
          y_frac: resolveTextElementYFrac(bar?.position, bar?.y_frac),
        });
      } else if (
        drag.target === "overlay" &&
        drag.mode === "move" &&
        dragOverride?.target === "overlay" &&
        dragOverride.x_frac != null &&
        dragOverride.y_frac != null
      ) {
        onPatchOverlay?.(drag.id, {
          x_frac: dragOverride.x_frac,
          y_frac: dragOverride.y_frac,
          position: "custom",
        });
      } else if (
        drag.target === "overlay" &&
        drag.mode === "scale" &&
        dragOverride?.target === "overlay" &&
        dragOverride.scale != null
      ) {
        onPatchOverlay?.(drag.id, { scale: dragOverride.scale });
      }
    } else if (drag.target === "text" && drag.mode === "move" && drag.hits.length > 0 && selectedTextId) {
      // Stationary click while already selected → cycle to the element
      // underneath at this point.
      const next = cycleHit(drag.hits, selectedTextId);
      if (next && next !== selectedTextId) onSelectText(next);
    }
    setDragOverride(null);
  }

  // Pan tool: drag scrolls the zoomed viewport.
  function onViewportPointerDown(e: React.PointerEvent) {
    if (tool !== "pan") return;
    const vp = viewportRef.current;
    if (!vp) return;
    panRef.current = { x: e.clientX, y: e.clientY, left: vp.scrollLeft, top: vp.scrollTop };
    (e.target as Element).setPointerCapture?.(e.pointerId);
  }
  function onViewportPointerMove(e: React.PointerEvent) {
    const pan = panRef.current;
    const vp = viewportRef.current;
    if (!pan || !vp) return;
    vp.scrollLeft = pan.left - (e.clientX - pan.x);
    vp.scrollTop = pan.top - (e.clientY - pan.y);
  }
  function onViewportPointerUp() {
    panRef.current = null;
  }

  // ── Video wiring ────────────────────────────────────────────────────────────

  function toggleFullscreen() {
    const el = stageRef.current;
    if (!el) return;
    if (document.fullscreenElement) void document.exitFullscreen();
    else void el.requestFullscreen();
  }

  const zoom = zoomPct / 100;
  const outputFormatLabel = canvas.w > canvas.h ? "16:9 landscape" : "9:16 portrait";
  // Unsaved orientation changes still display the previously rendered video.
  // In landscape, cover-crop that source so the canvas previews the same
  // centered 16:9 composition the server will produce on Save. Portrait keeps
  // its historical contain behavior.
  const videoFitClass = canvas.w > canvas.h ? "object-cover" : "object-contain";
  const activeLookStyles = lookPreviewStyles(lookPreset, lookAdjustments);
  const cameraTransformAt = (timeS: number): React.CSSProperties => ({
    transform: `scale(${cameraScaleAt(cameraEffects, timeS)})`,
    transformOrigin: "50% 50%",
    zIndex: EDITOR_STAGE_Z.video,
    ...activeLookStyles.video,
  });
  const virtualVideoStyleAt = (deck: "a" | "b", timeS: number): React.CSSProperties => {
    const styles = lookPreviewStyles(
      virtualDeckLookPresets[deck],
      virtualDeckLookAdjustments[deck],
    );
    return {
      transform: `scale(${cameraScaleAt(cameraEffects, timeS)})`,
      transformOrigin: "50% 50%",
      zIndex: EDITOR_STAGE_Z.video,
      ...styles.video,
    };
  };
  const lookPreviewLayers = (
    preset: LookPreset,
    adjustments?: LookAdjustments | null,
  ) => {
    const styles = lookPreviewStyles(preset, adjustments);
    if (preset === "none") return null;
    return (
      <>
        {preset === "stadium_diffusion" && (
          <div
            aria-hidden="true"
            className="pointer-events-none absolute -inset-[2.5%]"
            style={{
              zIndex: EDITOR_STAGE_Z.video + 1,
              transform: "scale(1.035)",
              backdropFilter: "blur(2.5px) saturate(0.96)",
              WebkitBackdropFilter: "blur(2.5px) saturate(0.96)",
              maskImage:
                "radial-gradient(ellipse at center, transparent 0 40%, rgba(0,0,0,.3) 58%, #000 92%)",
              WebkitMaskImage:
                "radial-gradient(ellipse at center, transparent 0 40%, rgba(0,0,0,.3) 58%, #000 92%)",
            }}
          />
        )}
        {styles.tint && (
          <div
            aria-hidden="true"
            data-look-preview-layer="tint"
            data-look-preview-preset={preset}
            className="pointer-events-none absolute inset-0"
            style={{ zIndex: EDITOR_STAGE_Z.video + 1, ...styles.tint }}
          />
        )}
        {styles.grain && (
          <div
            aria-hidden="true"
            data-look-preview-layer="grain"
            data-look-preview-preset={preset}
            className="pointer-events-none absolute inset-0"
            style={{ zIndex: EDITOR_STAGE_Z.video + 1, ...styles.grain }}
          />
        )}
      </>
    );
  };
  const cssPixelsPerCanvasPixel = stageSize.h > 0 ? stageSize.h / canvas.h : 0;
  const dissolvePreviewScaleAt = (
    timeS: number,
    frameVisible: ReturnType<typeof visibleAt>,
    frameMediaOverlays: ReturnType<typeof visibleMediaOverlaysAtTime>,
  ) => {
    const progress = settleAuthoredMotion
      ? 0
      : Math.max(
          0,
          ...frameVisible.map((layout) => {
            const effect = barById.get(layout.id)?.effect ?? "static";
            return effect === "dissolve-out"
              ? dissolveOutProgressAt(timeS - layout.start_s, layout.end_s - layout.start_s)
              : 0;
          }),
          ...frameMediaOverlays.map(({ card }) =>
            card.exit_token === "dissolve-out"
              ? dissolveOutProgressAt(timeS - card.start_s, card.end_s - card.start_s)
              : 0,
          ),
        );
    return dissolveOutDisplacementScaleAt(progress, cssPixelsPerCanvasPixel, true);
  };
  const committedVirtualFrame = virtualFrameStateAt(currentTime);

  // Keep stable media DOM out of React's decoded-frame render path. Only the
  // compositor styles that actually change with time are written here; the
  // authored overlay, transition overlay, and carousel each have their own
  // narrow PlaybackFrame subscriber below.
  useEffect(() => {
    if (!playbackClock) return;
    const updateMediaStyles = () => {
      const timeS = playbackClock.getSnapshot();
      const transition = virtualPreview
        ? transitionPreviewAtTime(virtualPreview.timeline, timeS)
        : null;
      const progress = transition?.progress ?? 0;
      const deckStyle = (deck: "a" | "b") => {
        const isActive = virtualPreview?.activeDeck === deck;
        if (transition?.carouselRole === "incoming") {
          return { opacity: isActive ? 1 : 0, zIndex: EDITOR_STAGE_Z.video };
        }
        if (transition?.carouselRole === "outgoing") {
          return { opacity: isActive ? 0 : 1, zIndex: EDITOR_STAGE_Z.video };
        }
        return {
          opacity: transition ? (isActive ? 1 - progress : progress) : isActive ? 1 : 0,
          zIndex: EDITOR_STAGE_Z.video,
        };
      };
      const deckAStyle = deckStyle("a");
      const deckBStyle = deckStyle("b");
      Object.assign(virtualDeckAContainerRef.current?.style ?? {}, deckAStyle);
      Object.assign(virtualDeckBContainerRef.current?.style ?? {}, deckBStyle);
      if (virtualVideoARef?.current) {
        virtualVideoARef.current.style.transform = `scale(${cameraScaleAt(cameraEffects, timeS)})`;
      }
      if (virtualVideoBRef?.current) {
        virtualVideoBRef.current.style.transform = `scale(${cameraScaleAt(cameraEffects, timeS)})`;
      }
      if (!virtualPreview && videoRef.current) {
        videoRef.current.style.transform = `scale(${cameraScaleAt(cameraEffects, timeS)})`;
      }
    };
    updateMediaStyles();
    return playbackClock.subscribe(updateMediaStyles);
  }, [cameraEffects, playbackClock, videoRef, virtualPreview, virtualVideoARef, virtualVideoBRef]);

  return (
    <div
      ref={viewportRef}
      data-region="canvas"
      data-output-orientation={canvas.w > canvas.h ? "landscape" : "portrait"}
      role="region"
      aria-label={`Video canvas, ${outputFormatLabel}`}
      data-look-preview={lookPreset}
      className={`relative h-full w-full min-h-0 min-w-0 overflow-auto bg-[#ffffff] ${
        tool === "pan" && zoom > 1 ? "cursor-grab active:cursor-grabbing" : ""
      }`}
      onPointerDown={onViewportPointerDown}
      onPointerMove={onViewportPointerMove}
      onPointerUp={onViewportPointerUp}
    >
      <div
        className="flex min-h-full items-center justify-center p-6"
        style={zoom > 1 ? { minWidth: `${zoom * 100}%`, minHeight: `${zoom * 100}%` } : undefined}
      >
        {/* height-driven output stage; zoom scales it up */}
        <div
          className="relative"
          style={{
            height: stageHeightCss
              ? `calc(${stageHeightCss} * ${zoom})`
              : `calc((100vh - 56px - 260px - 48px) * ${zoom})`,
            aspectRatio: `${canvas.w} / ${canvas.h}`,
            maxWidth: "100%",
          }}
        >
          <div
            ref={stageRef}
            className="relative h-full w-full overflow-hidden rounded-xl border border-zinc-200 bg-zinc-100 shadow-[0_8px_28px_rgba(12,12,14,0.10)]"
            onPointerDown={(e) => {
              // Click on the video surface / empty stage = clear selection
              // (plan §5 — the video surface is never a clip-selector).
              if (tool === "select" && e.target === e.currentTarget) onClearSelection();
            }}
          >
            <PlaybackFrame clock={playbackClock} fallbackTimeS={currentTime}>
              {(frameTime) => {
                const frameVisible = visibleAt(frameTime);
                const frameMediaOverlays = visibleMediaOverlaysAtTime(
                  mediaOverlays,
                  frameTime,
                  overlayPreviewUrls,
                );
                const dissolvePreviewScale = dissolvePreviewScaleAt(
                  frameTime,
                  frameVisible,
                  frameMediaOverlays,
                );
                return (
            <svg
              aria-hidden
              className="pointer-events-none absolute h-0 w-0"
              focusable="false"
            >
              <defs>
                <filter
                  id={DISSOLVE_PREVIEW_FILTER_ID}
                  x="-200%"
                  y="-200%"
                  width="500%"
                  height="500%"
                  colorInterpolationFilters="sRGB"
                >
                  <feTurbulence
                    type="fractalNoise"
                    baseFrequency={DISSOLVE_OUT_PARAMS.baseFrequency}
                    numOctaves="1"
                    result="bigNoise"
                  />
                  <feComponentTransfer in="bigNoise" result="bigNoiseAdjusted">
                    <feFuncR
                      type="linear"
                      slope={DISSOLVE_OUT_PARAMS.coherence}
                      intercept={-((DISSOLVE_OUT_PARAMS.coherence - 1) / 2)}
                    />
                    <feFuncG
                      type="linear"
                      slope={DISSOLVE_OUT_PARAMS.coherence}
                      intercept={-((DISSOLVE_OUT_PARAMS.coherence - 1) / 2)}
                    />
                  </feComponentTransfer>
                  <feTurbulence
                    type="fractalNoise"
                    baseFrequency={DISSOLVE_OUT_PARAMS.fineFrequency}
                    numOctaves="1"
                    result="fineNoise"
                  />
                  <feMerge result="mergedNoise">
                    <feMergeNode in="bigNoiseAdjusted" />
                    <feMergeNode in="fineNoise" />
                  </feMerge>
                  <feDisplacementMap
                    in="SourceGraphic"
                    in2="mergedNoise"
                    scale={dissolvePreviewScale}
                    xChannelSelector="R"
                    yChannelSelector="G"
                  />
                </filter>
              </defs>
            </svg>
                );
              }}
            </PlaybackFrame>
            {virtualPreview ? (
              <>
                <div
                  ref={virtualDeckAContainerRef}
                  className="pointer-events-none absolute inset-0 overflow-hidden"
                  style={virtualDeckStyleAt(
                    "a",
                    committedVirtualFrame.virtualTransition,
                    committedVirtualFrame.transitionProgress,
                  )}
                  data-look-preview-deck="a"
                  data-look-preset={virtualDeckLookPresets.a}
                >
                  <video
                    {...virtualVideoAProps}
                    ref={virtualVideoARef}
                    className={`pointer-events-none absolute inset-0 h-full w-full ${videoFitClass}`}
                    style={virtualVideoStyleAt("a", currentTime)}
                  />
                  {lookPreviewLayers(
                    virtualDeckLookPresets.a,
                    virtualDeckLookAdjustments.a,
                  )}
                </div>
                <div
                  ref={virtualDeckBContainerRef}
                  className="pointer-events-none absolute inset-0 overflow-hidden"
                  style={virtualDeckStyleAt(
                    "b",
                    committedVirtualFrame.virtualTransition,
                    committedVirtualFrame.transitionProgress,
                  )}
                  data-look-preview-deck="b"
                  data-look-preset={virtualDeckLookPresets.b}
                >
                  <video
                    {...virtualVideoBProps}
                    ref={virtualVideoBRef}
                    className={`pointer-events-none absolute inset-0 h-full w-full ${videoFitClass}`}
                    style={virtualVideoStyleAt("b", currentTime)}
                  />
                  {lookPreviewLayers(
                    virtualDeckLookPresets.b,
                    virtualDeckLookAdjustments.b,
                  )}
                </div>
                <PlaybackFrame clock={playbackClock} fallbackTimeS={currentTime}>
                  {(frameTime) => {
                    const { transitionOverlayOpacity, virtualTransition } =
                      virtualFrameStateAt(frameTime);
                    return transitionOverlayOpacity > 0 ? (
                      <div
                        aria-hidden="true"
                        className="pointer-events-none absolute inset-0"
                        style={{
                          backgroundColor:
                            virtualTransition?.kind === "flash" ? "#ffffff" : "#000000",
                          opacity: transitionOverlayOpacity,
                          zIndex: EDITOR_STAGE_Z.video + 2,
                        }}
                      />
                    ) : null;
                  }}
                </PlaybackFrame>
                {virtualMusicAudioProps && (
                  <audio
                    {...virtualMusicAudioProps}
                    ref={virtualMusicAudioRef}
                    className="hidden"
                  />
                )}
                <PlaybackFrame clock={playbackClock} fallbackTimeS={currentTime}>
                  {(frameTime) => {
                    const { displayedCarouselEntry, transitionProgress, virtualTransition } =
                      virtualFrameStateAt(frameTime);
                    return displayedCarouselEntry && carouselMoment ? (
                    <div
                    className="absolute inset-0 overflow-hidden"
                    style={{
                      zIndex: EDITOR_STAGE_Z.video + 3,
                      opacity:
                        virtualTransition?.carouselRole === "incoming"
                          ? transitionProgress
                          : virtualTransition?.carouselRole === "outgoing"
                            ? 1 - transitionProgress
                            : 1,
                    }}
                  >
                    {/* CarouselBlockPreviewImpl renders at its native
                        1080x1920 canvas space (see that component's
                        docblock) and expects the mount point to apply the
                        stage's fit-to-viewport scale — same
                        cssPixelsPerCanvasPixel ratio used elsewhere in this
                        file to convert canvas-native px to on-screen CSS px
                        (e.g. caption font-size below). Without this wrapper
                        the 1080x1920 box renders at native size inside the
                        much smaller on-screen stage, showing only its
                        top-left sliver. */}
                    <div
                      data-testid="carousel-block-scale-wrapper"
                      style={{
                        width: canvas.w,
                        height: canvas.h,
                        transform: `scale(${cssPixelsPerCanvasPixel})`,
                        transformOrigin: "0 0",
                      }}
                    >
                      <CarouselBlockPreview
                        config={carouselMoment}
                        clips={carouselClips}
                        currentTimeS={frameTime}
                        blockStartS={displayedCarouselEntry.startS}
                        durationS={displayedCarouselEntry.durationS}
                        isPlaying={playing}
                      />
                    </div>
                    </div>
                    ) : null;
                  }}
                </PlaybackFrame>
              </>
            ) : src ? (
              <StableVideo
                ref={videoRef}
                src={src}
                identity={identity ?? undefined}
                playsInline
                preload="auto"
                className={`pointer-events-none absolute inset-0 h-full w-full ${videoFitClass}`}
                style={cameraTransformAt(currentTime)}
                onTimeUpdate={(e) => {
                  const video = e.target as HTMLVideoElement;
                  if (
                    playbackClock &&
                    typeof video.requestVideoFrameCallback !== "function"
                  ) {
                    playbackClock.publish(video.currentTime);
                  }
                  onTimeUpdate(video.currentTime);
                }}
                onLoadedMetadata={(e) => {
                  const d = (e.target as HTMLVideoElement).duration;
                  if (isFinite(d) && d > 0) onDuration(d);
                }}
                onPlay={() => onPlayingChange?.(true)}
                onPause={(event) => {
                  if (playbackClock) {
                    playbackClock.publish(event.currentTarget.currentTime);
                    onTimeUpdate(event.currentTarget.currentTime);
                  }
                  onPlayingChange?.(false);
                }}
                // Frame under the playhead not yet decoded → shimmer (never move
                // the playhead against a silently frozen frame).
                onWaiting={() => setBuffering(true)}
                onSeeking={() => setBuffering(true)}
                onSeeked={() => setBuffering(false)}
                onCanPlay={() => {
                  setBuffering(false);
                  setVideoError(false);
                }}
                onPlaying={() => setBuffering(false)}
                onLoadedData={() => {
                  setBuffering(false);
                  setVideoError(false);
                }}
                // StableVideo already falls forward to the freshest signed URL;
                // a surfaced error means the fall-forward didn't recover.
                onError={() => setVideoError(true)}
              />
            ) : (
              <div
                className="absolute inset-0 flex h-full items-center justify-center rounded-xl border border-dashed border-zinc-300 text-sm text-[#71717a]"
                style={{ zIndex: EDITOR_STAGE_Z.video }}
              >
                No preview for this variant yet
              </div>
            )}

            {!virtualPreview &&
              hasPreview &&
              lookPreviewLayers(lookPreset, lookAdjustments)}

            <VisualBlocksLayer
              blocks={visualBlocks}
              assets={visualAssets}
              currentTime={currentTime}
              frameDriven={Boolean(playbackClock)}
              playbackClock={playbackClock}
              playing={playing}
            />
            <MotionCanvasLayer
              instances={motionScenes}
              assets={visualAssets}
              currentTime={currentTime}
              playing={playing}
              width={canvas.w}
              height={canvas.h}
              runtimeHash={motionRuntimeHash}
              videoRef={videoRef}
              frameDriven={Boolean(playbackClock)}
              playbackClock={playbackClock}
            />

            {/* Deselect layer over the video (the <video> is pointer-events-none,
                so clicks on footage land here). */}
            <PlaybackFrame clock={playbackClock} fallbackTimeS={currentTime}>
              {(frameTime) => {
                const frameVisible = visibleAt(frameTime);
                const frameMediaOverlays = visibleMediaOverlaysAtTime(
                  mediaOverlays,
                  frameTime,
                  overlayPreviewUrls,
                );
                const visibleCaption = visibleCaptionAt(frameTime);
                const captionPreviewStyle = captionPreviewStyleFor(visibleCaption);
                return <>
            {hasPreview && (
              <div
                className="absolute inset-0"
                style={{ zIndex: EDITOR_STAGE_Z.mediaOverlay }}
                onPointerDown={(e) => {
                  if (tool === "select" && e.target === e.currentTarget) onClearSelection();
                }}
                onPointerMove={onPointerMove}
                onPointerUp={onPointerUp}
              >
                {visibleCaption && (
                  <div
                    data-caption-preview="true"
                    className="pointer-events-none absolute inset-x-[7.5%] flex justify-center text-center"
                    style={{
                      bottom: `${captionPreviewStyle.bottomPct}%`,
                      // The final subtitled compositor burns captions after
                      // authored text but before media overlays.
                      zIndex: EDITOR_STAGE_Z.textOverlay + 10,
                    }}
                  >
                    {captionTapSelect ? (
                      <button
                        type="button"
                        data-caption-tap-target="true"
                        aria-label="Select this caption"
                        onPointerDown={(e) => {
                          // Selection only — never a drag source; stop the
                          // deselect layer underneath from clearing it.
                          e.stopPropagation();
                        }}
                        onClick={(e) => {
                          e.stopPropagation();
                          onSelectText(visibleCaption.bar.id);
                        }}
                        className="pointer-events-auto -m-2 min-h-11 min-w-11 cursor-pointer appearance-none border-0 bg-transparent p-2 text-center focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                      >
                        <span
                          style={{
                            color: captionPreviewStyle.color,
                            fontFamily: captionPreviewStyle.fontFamily,
                            fontSize: `${captionPreviewStyle.fontSizePx}px`,
                            fontWeight: 700,
                            lineHeight: 1.18,
                            maxWidth: "100%",
                            textShadow: captionPreviewStyle.textShadow,
                            whiteSpace: "pre-wrap",
                          }}
                        >
                          {visibleCaption.text}
                        </span>
                      </button>
                    ) : (
                      <span
                        style={{
                          color: captionPreviewStyle.color,
                          fontFamily: captionPreviewStyle.fontFamily,
                          fontSize: `${captionPreviewStyle.fontSizePx}px`,
                          fontWeight: 700,
                          lineHeight: 1.18,
                          maxWidth: "100%",
                          textShadow: captionPreviewStyle.textShadow,
                          whiteSpace: "pre-wrap",
                        }}
                      >
                        {visibleCaption.text}
                      </span>
                    )}
                  </div>
                )}
                {frameMediaOverlays.map((overlay) => (
                  <MediaOverlayCard
                    key={overlay.card.id}
                    overlay={overlay}
                    currentTimeS={frameTime}
                    reducedMotion={settleAuthoredMotion}
                    selected={selectedOverlayId === overlay.card.id}
                    flashing={flashOverlayIds?.has(overlay.card.id) ?? false}
                    suggested={suggestedOverlayIds?.has(overlay.card.id) ?? false}
                    hovered={hoveredOverlayId === overlay.card.id}
                    dragOverride={
                      dragOverride?.target === "overlay" && dragOverride.id === overlay.card.id
                        ? dragOverride
                        : null
                    }
                    allowManipulation={allowManipulation}
                    setRef={(el) => {
                      if (el) mediaOverlayRefs.current.set(overlay.card.id, el);
                      else mediaOverlayRefs.current.delete(overlay.card.id);
                    }}
                    onSelect={onSelectOverlay}
                    onPointerDown={beginMediaOverlayMove}
                    onHandlePointerDown={beginMediaOverlayScale}
                    onPointerMove={onPointerMove}
                    onPointerUp={onPointerUp}
                    onHoverChange={(hovered) =>
                      setHoveredOverlayId((current) =>
                        hovered ? overlay.card.id : current === overlay.card.id ? null : current,
                      )
                    }
                  />
                ))}
                {frameVisible.map((layout) => {
                  const bar = barById.get(layout.id);
                  const override =
                    dragOverride?.target === "text" && dragOverride.id === layout.id
                      ? dragOverride
                      : null;
                  const localXFrac = override?.x_frac ?? layout.xFrac;
                  const baseMotion = collageMotionForTextBar(variant, masonryDurationS, bar);
                  const motion =
                    baseMotion && override?.layer_origin_px != null
                      ? { ...baseMotion, layer_origin_px: override.layer_origin_px }
                      : baseMotion;
                  const xFrac =
                    masonryBoardXFrac(motion, localXFrac) -
                    masonryMotionOffsetFrac(motion, frameTime);
                  const yFrac = override?.y_frac ?? layout.yFrac;
                  const sizePx = override?.size_px ?? layout.sizePx;
                  const maxWidthFrac = override?.max_width_frac ?? layout.maxWidthFrac;
                  const fontPx = stageSize.h > 0 ? (sizePx / canvas.h) * stageSize.h : 0;
                  const strokeCanvasPx = bar?.stroke_width ?? layout.strokeWidth;
                  const strokePx =
                    strokeCanvasPx && stageSize.h > 0
                      ? (strokeCanvasPx / canvas.h) * stageSize.h
                      : 0;
                  const isSelected = selectedTextId === layout.id;
                  const isLyric = bar?.role === "lyric_line";
                  const isHovered = hoveredId === layout.id && !isSelected;
                  const isFlashing = flashTextIds?.has(layout.id) ?? false;
                  const zIndex =
                    isSelected && allowManipulation
                      ? EDITOR_STAGE_Z.selectionHandle
                      : EDITOR_STAGE_Z.textOverlay;
                  const effect = bar?.effect ?? "static";
                  const animationDurationS = textMotionPreviewDurationS(
                    layout.end_s - layout.start_s,
                    bar?.motion,
                    TEXT_MOTION_V2_UI_ENABLED,
                    MAX_INTRO_S,
                  );
                  const animation = animationStateAt(
                    effect,
                    Math.max(0, frameTime - layout.start_s),
                    animationDurationS,
                    layout.text,
                    {
                      revealScheduleS: bar?.source_params?.reveal_schedule_s,
                      absoluteStartS: layout.start_s,
                      motion: bar?.motion,
                      motionV2Enabled: TEXT_MOTION_V2_UI_ENABLED,
                    },
                  );
                  const transition = themeTransitionStateAt(
                    bar?.theme_transition,
                    Math.max(0, frameTime - layout.start_s),
                    Math.min(MAX_INTRO_S, Math.max(0.01, layout.end_s - layout.start_s)),
                    layout.text,
                  );
                  const fadeOutAlpha = sequenceOverlayFadeOutAlphaAt(
                    bar?.role,
                    effect,
                    Math.max(0, frameTime - layout.start_s),
                    Math.max(0.01, layout.end_s - layout.start_s),
                    bar?.fade_out_ms,
                  );
                  const usesFixedRevealLayout =
                    effect === "typewriter" || effect === "stream-in";
                  const baseStyle = textElementWrapperStyle({
                    layout,
                    xFrac,
                    yFrac,
                    maxWidthFrac,
                    zIndex,
                  });
                  const smoothPreview = smoothPreviewById.get(layout.id);
                  const motionFontPx = smoothPreview
                    ? (smoothPreview.sizePx / canvas.h) * stageSize.h
                    : fontPx;
                  return (
                    <div
                      key={layout.id}
                      ref={(el) => {
                        if (el) overlayRefs.current.set(layout.id, el);
                        else overlayRefs.current.delete(layout.id);
                      }}
                      data-text-id={layout.id}
                      data-max-width-frac={maxWidthFrac}
                      className={`absolute select-none touch-none ${
                        tool === "select" ? "cursor-pointer" : ""
                      }`}
                      style={{
                        ...baseStyle,
                        opacity: animation.alpha * transition.alpha * fadeOutAlpha,
                        filter: [
                          !settleAuthoredMotion && animation.dissolveProgress > 0
                            ? `url(#${DISSOLVE_PREVIEW_FILTER_ID})`
                            : null,
                          !settleAuthoredMotion && animation.blurPx > 0.01
                            ? `blur(${(animation.blurPx / canvas.h) * stageSize.h}px)`
                            : null,
                        ].filter(Boolean).join(" ") || undefined,
                        transform: `${baseStyle.transform ?? ""} translate(${
                          (animation.xTranslate / canvas.w) * stageSize.w
                        }px, ${(animation.yTranslate / canvas.h) * stageSize.h}px) scale(${
                          animation.scale *
                          transition.scale *
                          (settleAuthoredMotion
                            ? 1
                            : dissolveOutTransformScaleAt(animation.dissolveProgress))
                        })`,
                        transformOrigin: `calc(50% + ${
                          ((transition.scaleOriginX || animation.scaleOriginX) / canvas.w) *
                          stageSize.w
                        }px) calc(50% + ${
                          ((transition.scaleOriginY || animation.scaleOriginY) / canvas.h) *
                          stageSize.h
                        }px)`,
                      }}
                      onPointerDown={(e) => onOverlayPointerDown(e, layout.id)}
                      onPointerMove={onPointerMove}
                      onPointerUp={onPointerUp}
                      onPointerEnter={() => setHoveredId(layout.id)}
                      onPointerLeave={() => setHoveredId((h) => (h === layout.id ? null : h))}
                      onDoubleClick={(e) => {
                        e.stopPropagation();
                        onSelectText(layout.id);
                        onFocusContent();
                      }}
                    >
                      {effect === "staggered-slice" ? (
                        <StaggeredSliceText
                          text={layout.text}
                          tLocal={frameTime - layout.start_s}
                          durationS={animationDurationS}
                          playing={playing}
                          motion={TEXT_MOTION_V2_UI_ENABLED ? bar?.motion : null}
                          style={textElementContentStyle({
                            layout,
                            fontSize: `${fontPx}px`,
                            strokeWidth: strokePx > 0 ? `${strokePx}px` : null,
                            canvasPixelCssSize: `${stageSize.h / canvas.h}px`,
                          })}
                        />
                      ) : (
                        <TextElementOverlayContent
                          layout={{ ...layout, effect: effect as TextElement["effect"] }}
                          fontSize={`${motionFontPx}px`}
                          strokeWidth={strokePx > 0 ? `${strokePx}px` : null}
                          canvasPixelCssSize={`${stageSize.h / canvas.h}px`}
                          reserveText={
                            usesFixedRevealLayout
                              ? normalizeAnimatedRevealText(layout.text)
                              : null
                          }
                          showCursor={animation.showCursor}
                          cursorStyle={animation.cursorStyle}
                          revealProgress={
                            effect === "handwriting" || effect === "ink-reveal" || effect === "smooth-type"
                              ? settleAuthoredMotion
                                ? 1
                                : animation.revealProgress
                              : undefined
                          }
                          revealOrigin={animation.revealOrigin}
                          revealLines={smoothPreview?.lines}
                          lineRevealProgresses={
                            effect === "smooth-type" &&
                            TEXT_MOTION_V2_UI_ENABLED &&
                            !settleAuthoredMotion
                              ? smoothTypeLineProgresses(
                                  smoothPreview?.lines ?? layout.text.split("\n"),
                                  frameTime - layout.start_s,
                                  bar?.motion,
                                )
                              : undefined
                          }
                        >
                          {animation.visibleText}
                        </TextElementOverlayContent>
                      )}

                      {/* Hover ghost outline (1px zinc-400/60) */}
                      {isHovered && (
                        <div
                          aria-hidden
                          className="pointer-events-none absolute inset-0 rounded-[2px]"
                          style={{ outline: "1px solid rgba(161,161,170,0.6)" }}
                        />
                      )}

                      {isFlashing && (
                        <div
                          aria-hidden
                          className="pointer-events-none absolute -inset-1 rounded-[3px] outline outline-2 outline-offset-4 outline-lime-500 motion-safe:animate-pulse"
                        />
                      )}

                      {/* Selection box: lime stroke; handles white-core + 1px ink halo (D10) */}
                      {isSelected && allowManipulation && !isLyric && (
                        <div
                          aria-hidden={false}
                          role="group"
                          aria-label={`Selected text: ${layout.text.slice(0, 40)}`}
                          className="absolute -inset-1 motion-safe:transition-opacity motion-safe:duration-150"
                          style={{
                            border: "1.5px solid #84cc16",
                            zIndex: EDITOR_STAGE_Z.selectionHandle,
                          }}
                        >
                          {(["nw", "ne", "sw", "se"] as const).map((corner) => (
                            <button
                              key={corner}
                              type="button"
                              tabIndex={-1}
                              aria-label={`Resize text (${corner})`}
                              onPointerDown={(e) => onHandlePointerDown(e, layout.id)}
                              className="absolute flex h-6 w-6 items-center justify-center touch-none"
                              style={{
                                cursor: corner === "nw" || corner === "se" ? "nwse-resize" : "nesw-resize",
                                top: corner.startsWith("n") ? -13 : undefined,
                                bottom: corner.startsWith("s") ? -13 : undefined,
                                left: corner.endsWith("w") ? -13 : undefined,
                                right: corner.endsWith("e") ? -13 : undefined,
                              }}
                            >
                              <span
                                aria-hidden
                                className="h-2 w-2 rounded-[1px] bg-white"
                                style={{ boxShadow: "0 0 0 1px #0c0c0e" }}
                              />
                            </button>
                          ))}
                          {(["left", "right"] as const).map((side) => (
                            <button
                              key={side}
                              type="button"
                              tabIndex={-1}
                              aria-label={`Adjust text width (${side})`}
                              onPointerDown={(e) => onWidthHandlePointerDown(e, layout.id, side)}
                              className="absolute flex h-7 w-7 items-center justify-center touch-none"
                              style={{
                                cursor: "ew-resize",
                                top: "50%",
                                transform: "translateY(-50%)",
                                left: side === "left" ? -15 : undefined,
                                right: side === "right" ? -15 : undefined,
                              }}
                            >
                              <span
                                aria-hidden
                                className="h-3 w-1.5 rounded-[1px] bg-white"
                                style={{ boxShadow: "0 0 0 1px #0c0c0e" }}
                              />
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
                </>;
              }}
            </PlaybackFrame>

            {/* Scrub-buffering shimmer (readyState < HAVE_CURRENT_DATA). */}
            {hasPreview && (virtualPreview?.buffering || buffering) && !videoError && (
              <div
                aria-hidden
                className="pointer-events-none absolute inset-0 bg-gradient-to-r from-transparent via-white/40 to-transparent motion-safe:animate-pulse"
                style={{ zIndex: EDITOR_STAGE_Z.chrome }}
              />
            )}

            {/* Load-failure / expired-URL tile — plain reason + Retry (re-fetch
                re-signs the URL). Distinct from the ineligible-variant banner. */}
            {src && !virtualPreview && videoError && (
              <div
                className="absolute inset-0 flex items-center justify-center p-6"
                style={{ zIndex: EDITOR_STAGE_Z.error }}
              >
                <div className="max-w-[280px] rounded-xl border border-dashed border-zinc-300 bg-white/95 p-5 text-center">
                  <p className="text-[13px] text-[#3f3f46]">
                    This preview couldn&apos;t load — the link may have expired.
                  </p>
                  <button
                    type="button"
                    onClick={() => {
                      setVideoError(false);
                      onReloadSource?.();
                    }}
                    className="mt-3 min-h-11 rounded-full border border-zinc-200 px-4 text-[12px] text-[#3f3f46] hover:border-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                  >
                    Retry
                  </button>
                </div>
              </div>
            )}

            {/* Bottom-right corner: fullscreen ONLY. Play/pause now lives in
                the TransportBar (§6); "Basic mode" pill is CUT (D7). */}
            {hasPreview && (
              <div
                className="absolute bottom-3 right-3 flex items-center gap-2"
                style={{ zIndex: EDITOR_STAGE_Z.chrome }}
              >
                <button
                  type="button"
                  aria-label="Fullscreen"
                  onClick={toggleFullscreen}
                  className="flex h-11 w-11 items-center justify-center rounded-lg border border-zinc-200 bg-white/90 text-sm text-[#3f3f46] hover:bg-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                >
                  ⛶
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function MediaOverlayCard({
  overlay,
  currentTimeS,
  reducedMotion,
  selected,
  flashing = false,
  suggested = false,
  hovered,
  dragOverride,
  allowManipulation,
  setRef,
  onSelect,
  onPointerDown,
  onHandlePointerDown,
  onPointerMove,
  onPointerUp,
  onHoverChange,
}: {
  overlay: VisibleMediaOverlay;
  currentTimeS: number;
  reducedMotion: boolean;
  selected: boolean;
  flashing?: boolean;
  /** ✓-accepted AI suggestion, unsaved — dashed lime outline + ✦ marker. */
  suggested?: boolean;
  hovered: boolean;
  dragOverride: Extract<DragOverride, { target: "overlay" }> | null;
  allowManipulation: boolean;
  setRef: (el: HTMLDivElement | null) => void;
  onSelect?: (id: string) => void;
  onPointerDown: (e: React.PointerEvent<HTMLElement>, card: MediaOverlay) => void;
  onHandlePointerDown: (e: React.PointerEvent<HTMLElement>, card: MediaOverlay) => void;
  onPointerMove: (e: React.PointerEvent) => void;
  onPointerUp: () => void;
  onHoverChange: (hovered: boolean) => void;
}) {
  const { card, displayUrl } = overlay;
  const [previewFailed, setPreviewFailed] = useState(false);
  useEffect(() => {
    setPreviewFailed(false);
  }, [displayUrl]);
  const xFrac = dragOverride?.x_frac ?? card.x_frac;
  const yFrac = dragOverride?.y_frac ?? card.y_frac;
  const scale = dragOverride?.scale ?? card.scale;
  const durationS = Math.max(0.01, card.end_s - card.start_s);
  const progress =
    card.exit_token === "dissolve-out"
      ? dissolveOutProgressAt(currentTimeS - card.start_s, durationS)
      : 0;
  const mediaStyle =
    progress > 0
      ? {
          opacity: dissolveOutAlphaAt(progress),
          filter: !reducedMotion ? `url(#${DISSOLVE_PREVIEW_FILTER_ID})` : undefined,
          transform: !reducedMotion ? `scale(${dissolveOutTransformScaleAt(progress)})` : undefined,
        }
      : undefined;
  return (
    <div
      ref={setRef}
      data-overlay-id={card.id}
      className={`absolute select-none touch-none ${allowManipulation ? "cursor-pointer" : ""}`}
      style={{
        left: `${xFrac * 100}%`,
        top: `${yFrac * 100}%`,
        transform: "translate(-50%, -50%)",
        width: `${scale * 100}%`,
        zIndex: mediaOverlayStackZIndex(card.z, selected),
      }}
      onPointerDown={(e) => {
        if (!allowManipulation) {
          e.stopPropagation();
          onSelect?.(card.id);
          return;
        }
        onPointerDown(e, card);
      }}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerEnter={() => onHoverChange(true)}
      onPointerLeave={() => onHoverChange(false)}
    >
      {card.kind === "image" && displayUrl && !previewFailed ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={displayUrl}
          alt=""
          className="h-auto w-full rounded"
          style={mediaStyle}
          draggable={false}
          onError={() => setPreviewFailed(true)}
        />
      ) : card.kind === "video" && displayUrl ? (
        <EditorVideoOverlayPreview
          src={displayUrl}
          trimStart={card.clip_trim_start_s ?? 0}
          trimEnd={card.clip_trim_end_s ?? null}
          cardStartS={card.start_s}
          currentTimeS={currentTimeS}
          style={mediaStyle}
        />
      ) : (
        <div
          className="flex aspect-[4/3] w-full items-center justify-center rounded border border-dashed border-zinc-300 bg-white/90 px-3 text-center text-[11px] font-medium text-[#3f3f46] shadow-sm"
          style={mediaStyle}
        >
          Preview unavailable
        </div>
      )}
      {hovered && !selected && (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 rounded-[2px]"
          style={{ outline: "1px solid rgba(161,161,170,0.6)" }}
        />
      )}
      {/* Suggestion provenance (until Save): dashed lime outline + ✦ badge.
          Suppressed while selected so it never fights the selection frame. */}
      {suggested && !selected && (
        <div
          aria-hidden
          data-testid={`suggested-overlay-marker-${card.id}`}
          className="pointer-events-none absolute inset-0 rounded-[2px] border-[1.5px] border-dashed border-lime-600"
        >
          <span className="absolute -right-1.5 -top-1.5 flex h-4 w-4 items-center justify-center rounded-full bg-lime-600 text-[9px] text-white">
            ✦
          </span>
        </div>
      )}
      {selected && (
        <div
          aria-hidden={false}
          role="group"
          aria-label={`Selected ${card.kind} overlay`}
          className="absolute -inset-1 rounded motion-safe:transition-opacity motion-safe:duration-150"
          style={{
            border: "1.5px solid #84cc16",
            zIndex: EDITOR_STAGE_Z.selectionHandle,
          }}
        >
          {allowManipulation &&
            (["nw", "ne", "sw", "se"] as const).map((corner) => (
              <button
                key={corner}
                type="button"
                tabIndex={-1}
                aria-label={`Resize overlay (${corner})`}
                onPointerDown={(e) => onHandlePointerDown(e, card)}
                className="absolute flex h-6 w-6 items-center justify-center touch-none"
                style={{
                  cursor: corner === "nw" || corner === "se" ? "nwse-resize" : "nesw-resize",
                  top: corner.startsWith("n") ? -13 : undefined,
                  bottom: corner.startsWith("s") ? -13 : undefined,
                  left: corner.endsWith("w") ? -13 : undefined,
                  right: corner.endsWith("e") ? -13 : undefined,
                }}
              >
                <span
                  aria-hidden
                  className="h-2 w-2 rounded-[1px] bg-white"
                  style={{ boxShadow: "0 0 0 1px #0c0c0e" }}
                />
              </button>
            ))}
        </div>
      )}
      {flashing && (
        <div
          aria-hidden
          className="pointer-events-none absolute -inset-1 rounded outline outline-2 outline-offset-4 outline-lime-500 motion-safe:animate-pulse"
        />
      )}
    </div>
  );
}

function EditorVideoOverlayPreview({
  src,
  trimStart,
  trimEnd,
  cardStartS,
  currentTimeS,
  style,
}: {
  src: string;
  trimStart: number;
  trimEnd: number | null;
  cardStartS: number;
  currentTimeS: number;
  style?: React.CSSProperties;
}) {
  const ref = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const video = ref.current;
    if (!video) return;
    const overlayTime = trimStart + Math.max(0, currentTimeS - cardStartS);
    const cappedTime = trimEnd !== null ? Math.min(overlayTime, trimEnd) : overlayTime;
    if (Number.isFinite(cappedTime) && Math.abs(video.currentTime - cappedTime) > 0.15) {
      video.currentTime = cappedTime;
    }
  }, [cardStartS, currentTimeS, trimEnd, trimStart]);

  return (
    <video
      ref={ref}
      src={src}
      autoPlay
      muted
      loop
      playsInline
      className="h-auto w-full rounded"
      style={style}
    />
  );
}
