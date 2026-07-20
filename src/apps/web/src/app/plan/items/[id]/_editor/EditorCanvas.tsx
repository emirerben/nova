"use client";

/**
 * EditorCanvas — the center 9:16 preview of the TikTok-parity editor shell.
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
  MediaOverlay,
  PlanItemVariant,
  PoolAsset,
  SoundEffectPlacement,
  TextElement,
  VisualBlock,
} from "@/lib/plan-api";
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
import {
  animationStateAt,
  normalizeAnimatedRevealText,
  sequenceOverlayFadeOutAlphaAt,
} from "@/lib/overlay-animation";
import { INTRO_FONTS, MAX_INTRO_S, type OverlayCanvas } from "@/lib/overlay-constants";
import { StableVideo } from "@/components/StableVideo";
import { useSfxPreview } from "@/app/plan/_components/useSfxPreview";
import {
  TextElementOverlayContent,
  textElementWrapperStyle,
} from "../components/TextElementOverlayLayer";
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

/** Min/max font size (1080×1920 canvas px) reachable via corner-drag scale.
 * Wider than the inspector's INTRO_SIZE envelope on purpose — the canvas can
 * host non-intro roles whose sizes exceed it; the server clamps regardless. */
const SCALE_MIN_PX = 24;
const SCALE_MAX_PX = 250;
const DEFAULT_CANVAS = { w: CANVAS_W, h: CANVAS_H };

/** Pointer movement (px) under which a pointerdown+up counts as a CLICK
 * (triggers overlap cycling) rather than a drag. */
const CLICK_SLOP_PX = 3;

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
  visualAssets = [],
  overlayPreviewUrls = {},
  suggestedOverlayIds,
  sfxPlacements = [],
  sfxAudioUrls = {},
  selectedTextId,
  selectedOverlayId,
  flashTextIds,
  flashOverlayIds,
  currentTime,
  masonryDurationS,
  zoomPct,
  tool,
  videoRef,
  onSelectText,
  onSelectOverlay,
  onClearSelection,
  onPatchBar,
  onPatchOverlay,
  onFocusContent,
  onTimeUpdate,
  onDuration,
  onPlayingChange,
  onReloadSource,
  virtualPreview,
  allowManipulation = true,
  stageHeightCss,
  canvas = DEFAULT_CANVAS,
}: {
  variant: PlanItemVariant;
  /** Working bars projected to API shape (barsToTextElements) — layout input. */
  elements: TextElement[];
  /** The raw working bars, for style fields the layout doesn't carry. */
  bars: TextElementBar[];
  mediaOverlays?: MediaOverlay[];
  visualBlocks?: VisualBlock[];
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
  /** Current preview/render duration used by the masonry board pan. */
  masonryDurationS: number;
  zoomPct: number;
  tool: "select" | "pan";
  /** Owned by the shell so the future TransportBar can drive the same video. */
  videoRef: React.RefObject<HTMLVideoElement>;
  onSelectText: (id: string) => void;
  onSelectOverlay?: (id: string) => void;
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
  /** Light-edit mode keeps the canvas tap-only: no drag, scale, or handles. */
  allowManipulation?: boolean;
  /** Shell-specific chrome height for sizing the 9:16 stage. */
  stageHeightCss?: string;
  /** Output canvas dimensions used for layout projection. Defaults to portrait. */
  canvas?: OverlayCanvas;
}) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const overlayRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const mediaOverlayRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const dragRef = useRef<DragState | null>(null);
  const panRef = useRef<{ x: number; y: number; left: number; top: number } | null>(null);
  const emptyVideoRef = useRef<HTMLVideoElement | null>(null);

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

  // Elements visible at the playhead (the working bars' own timing).
  const visible = useMemo(
    () => layouts.filter((l) => currentTime >= l.start_s && currentTime < l.end_s),
    [layouts, currentTime],
  );
  const visibleMediaOverlays = useMemo(
    () => visibleMediaOverlaysAtTime(mediaOverlays, currentTime, overlayPreviewUrls),
    [currentTime, mediaOverlays, overlayPreviewUrls],
  );
  const captionPreviewUsesCleanBase = Boolean(variant.base_video_url || virtualPreview);
  const visibleCaption = useMemo(() => {
    if (
      !captionPreviewUsesCleanBase ||
      variant.resolved_archetype !== "subtitled" ||
      variant.captions_enabled === false ||
      !variant.caption_cues?.length
    ) {
      return null;
    }
    const cue = variant.caption_cues.find(
      (candidate) => currentTime >= candidate.start_s && currentTime < candidate.end_s,
    );
    if (!cue) return null;
    if (variant.voiceover_caption_style === "word" && cue.words?.length) {
      return (
        cue.words.find(
          (word) => currentTime >= word.start_s && currentTime < word.end_s,
        )?.text ?? null
      );
    }
    return cue.text;
  }, [captionPreviewUsesCleanBase, currentTime, variant]);
  const captionFontFamily = useMemo(() => {
    const selected = INTRO_FONTS.find((font) => font.name === variant.voiceover_caption_font);
    return selected?.cssFamily ?? "'TikTok Sans', 'Inter', system-ui, sans-serif";
  }, [variant.voiceover_caption_font]);

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
  const identity = variant.base_video_url
    ? (variant.base_video_path ?? undefined)
    : `${variant.variant_id}:${variant.render_finished_at ?? ""}`;

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
    visible.forEach((l, i) => {
      const el = overlayRefs.current.get(l.id);
      if (!el) return;
      const r = el.getBoundingClientRect();
      if (clientX >= r.left && clientX <= r.right && clientY >= r.top && clientY <= r.bottom) {
        out.push({ id: l.id, z: i });
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
    if (Math.hypot(dx, dy) > CLICK_SLOP_PX) drag.moved = true;
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
  // Unsaved orientation changes still display the previously rendered video.
  // In landscape, cover-crop that source so the canvas previews the same
  // centered 16:9 composition the server will produce on Save. Portrait keeps
  // its historical contain behavior.
  const videoFitClass = canvas.w > canvas.h ? "object-cover" : "object-contain";

  return (
    <div
      ref={viewportRef}
      data-region="canvas"
      className={`relative h-full w-full min-h-0 min-w-0 overflow-auto bg-[#fafaf8] ${
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
            {virtualPreview ? (
              <>
                <video
                  {...virtualVideoAProps}
                  ref={virtualVideoARef}
                  className={[
                    `pointer-events-none absolute inset-0 h-full w-full ${videoFitClass}`,
                    virtualPreview.activeDeck === "a" ? "opacity-100" : "opacity-0",
                  ].join(" ")}
                  style={{ zIndex: EDITOR_STAGE_Z.video }}
                />
                <video
                  {...virtualVideoBProps}
                  ref={virtualVideoBRef}
                  className={[
                    `pointer-events-none absolute inset-0 h-full w-full ${videoFitClass}`,
                    virtualPreview.activeDeck === "b" ? "opacity-100" : "opacity-0",
                  ].join(" ")}
                  style={{ zIndex: EDITOR_STAGE_Z.video }}
                />
                {virtualMusicAudioProps && (
                  <audio
                    {...virtualMusicAudioProps}
                    ref={virtualMusicAudioRef}
                    className="hidden"
                  />
                )}
              </>
            ) : src ? (
              <StableVideo
                ref={videoRef}
                src={src}
                identity={identity}
                playsInline
                preload="auto"
                className={`pointer-events-none absolute inset-0 h-full w-full ${videoFitClass}`}
                style={{ zIndex: EDITOR_STAGE_Z.video }}
                onTimeUpdate={(e) => onTimeUpdate((e.target as HTMLVideoElement).currentTime)}
                onLoadedMetadata={(e) => {
                  const d = (e.target as HTMLVideoElement).duration;
                  if (isFinite(d) && d > 0) onDuration(d);
                }}
                onPlay={() => onPlayingChange?.(true)}
                onPause={() => onPlayingChange?.(false)}
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

            <VisualBlocksLayer
              blocks={visualBlocks}
              assets={visualAssets}
              currentTime={currentTime}
            />

            {/* Deselect layer over the video (the <video> is pointer-events-none,
                so clicks on footage land here). */}
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
                      bottom: `${
                        ((variant.caption_margin_v ?? 384) / canvas.h) * 100
                      }%`,
                      // The final subtitled compositor burns captions after
                      // authored text but before media overlays.
                      zIndex: EDITOR_STAGE_Z.textOverlay + 10,
                    }}
                  >
                    <span
                      style={{
                        color: "#fff",
                        fontFamily: captionFontFamily,
                        fontSize: `${stageSize.h > 0 ? (78 / canvas.h) * stageSize.h : 0}px`,
                        fontWeight: 700,
                        lineHeight: 1.18,
                        maxWidth: "100%",
                        textShadow:
                          "0 2px 2px #000, 2px 0 2px #000, 0 -2px 2px #000, -2px 0 2px #000",
                        whiteSpace: "pre-wrap",
                      }}
                    >
                      {visibleCaption}
                    </span>
                  </div>
                )}
                {visibleMediaOverlays.map((overlay) => (
                  <MediaOverlayCard
                    key={overlay.card.id}
                    overlay={overlay}
                    currentTimeS={currentTime}
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
                {visible.map((layout) => {
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
                    masonryMotionOffsetFrac(motion, currentTime);
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
                  const animation = animationStateAt(
                    effect,
                    Math.max(0, currentTime - layout.start_s),
                    Math.min(MAX_INTRO_S, Math.max(0.01, layout.end_s - layout.start_s)),
                    layout.text,
                  );
                  const fadeOutAlpha = sequenceOverlayFadeOutAlphaAt(
                    bar?.role,
                    effect,
                    Math.max(0, currentTime - layout.start_s),
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
                        opacity: animation.alpha * fadeOutAlpha,
                        transform: `${baseStyle.transform ?? ""} translateY(${
                          (animation.yTranslate / canvas.h) * stageSize.h
                        }px) scale(${animation.scale})`,
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
                      <TextElementOverlayContent
                        layout={layout}
                        fontSize={`${fontPx}px`}
                        strokeWidth={strokePx > 0 ? `${strokePx}px` : null}
                        canvasPixelCssSize={`${stageSize.h / canvas.h}px`}
                        reserveText={
                          usesFixedRevealLayout
                            ? normalizeAnimatedRevealText(layout.text)
                            : null
                        }
                        showCursor={animation.showCursor}
                      >
                        {animation.visibleText}
                      </TextElementOverlayContent>

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
        />
      ) : (
        <div className="flex aspect-[4/3] w-full items-center justify-center rounded border border-dashed border-zinc-300 bg-white/90 px-3 text-center text-[11px] font-medium text-[#3f3f46] shadow-sm">
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
}: {
  src: string;
  trimStart: number;
  trimEnd: number | null;
  cardStartS: number;
  currentTimeS: number;
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
    />
  );
}
