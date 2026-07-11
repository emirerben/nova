"use client";

/**
 * OverlayLane — interactive Overlays lane for UnifiedTimeline.
 *
 * Owns: overlay drag-move, edge-trim (both timeline position and clip trim),
 * per-card timing track, video TrimLane with thumbnail strip, and the upload
 * zone. The per-card popover lives in OverlayCardPopover.tsx (009 T3 split).
 *
 * Extracted from UnifiedTimeline.tsx (T0 refactor). No logic changed.
 *
 * 006 T3 (005-4A lane rendering): pending AI suggestions render as editable
 * cards alongside manual cards — dashed lime-600 border + ✦ badge provenance.
 * Every existing interaction (drag-move, edge trim, popover scale/position,
 * TrimLane clip trim) works on them, but edits route to
 * `onSuggestionEdit(suggestionId, patch)` instead of `onUpdateCard` — the
 * manual media_overlays state is never touched and no API call fires
 * (suggestions only persist via the rail's Apply). Once staged the card flips
 * dashed→solid and the ✦ fades (005-6A, motion-safe).
 *
 * 009 T3 (fullscreen cutaways):
 *  - display_mode "fullscreen" chips render taller (h-8 vs h-6) with a solid
 *    ink fill + "⛶ Full" glyph; below ~24px width the glyph hides and the
 *    edge-trim handles are suppressed (timing edits via popover fields).
 *    Lime stays exclusively provenance — the dashed lime-600 + ✦ suggestion
 *    treatment layers over either mode unchanged.
 *  - Drag/resize hard-stops at fullscreen boundaries during the gesture
 *    (fullscreenGapBounds) — no post-release snap-back; the server-side E4
 *    overlap helper 422s as the backstop.
 *  - Manual chips are focusable (tabIndex=0) with mode-aware aria-labels;
 *    F toggles display_mode on a focused chip or while the popover is open.
 *  - Fullscreen video windows that outrun their trimmed footage hard-snap
 *    end_s (snap, not freeze, for manual fullscreen).
 */

import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import type { MediaOverlay } from "@/lib/plan-api";
import { Playhead } from "@/lib/timeline/Playhead";
import type {
  UploadFile,
  OverlayDragState,
  SuggestionLaneEntry,
} from "./UnifiedTimelineTypes";
import OverlayCardPopover, {
  demotePatch,
  fullscreenOutrunSnapEnd,
  type OverlayAssetMeta,
} from "./OverlayCardPopover";

// ── Constants ─────────────────────────────────────────────────────────────────

const ALLOWED_OVERLAY_MIME_TYPES = [
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/heic",
  "video/mp4",
  "video/quicktime",
];

const TRACK_COLORS = ["#8B5CF6", "#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#EC4899"];

const THUMB_COUNT = 10;

/** Below this rendered chip width (px) a fullscreen chip drops its glyph and
 *  edge handles — the solid ink fill is the identifier (009 T3). */
const TINY_CHIP_PX = 24;
const COARSE_TINY_CHIP_PX = 48;
const TOUCH_TAP_SLOP_PX = 8;
const MOUSE_TAP_SLOP_PX = 3;
const HANDLE_HIT_PX = 44;
const FINE_HANDLE_HIT_PX = 12;
const MIN_COARSE_CHIP_BODY_PX = 16;

/** Solid ink fill for fullscreen chips (DESIGN.md §2 --ink). */
const INK = "#0c0c0e";

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtTime(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

// ── Fullscreen drag hard-stop (009 T3) ────────────────────────────────────────

/**
 * Bounds of the gap the moving card sits in, considering only windows it must
 * never overlap: if the moving card is fullscreen, EVERY other card blocks;
 * if it is pip, only fullscreen cards block (pip+pip overlap stays legal —
 * z-order handles it). Gestures clamp into [lower, upper] so the chip
 * hard-stops at the boundary during the drag — no post-release snap-back.
 * Exported for unit tests.
 */
export function fullscreenGapBounds(opts: {
  movingId: string;
  movingFullscreen: boolean;
  /** Pre-gesture window — determines which side of each blocker we're on. */
  origStart: number;
  origEnd: number;
  cards: Pick<MediaOverlay, "id" | "start_s" | "end_s" | "display_mode">[];
  totalDurationS: number;
}): { lower: number; upper: number } {
  let lower = 0;
  let upper = opts.totalDurationS;
  for (const c of opts.cards) {
    if (c.id === opts.movingId) continue;
    const blockerFullscreen = (c.display_mode ?? "pip") === "fullscreen";
    if (!opts.movingFullscreen && !blockerFullscreen) continue;
    if (c.end_s <= opts.origStart + 1e-6) {
      lower = Math.max(lower, c.end_s);
    } else if (c.start_s >= opts.origEnd - 1e-6) {
      upper = Math.min(upper, c.start_s);
    }
    // else: pre-existing overlap (legacy data) — don't wedge the gesture; the
    // server-side E4 overlap check is the backstop.
  }
  return { lower, upper };
}

// ── Video thumbnail extractor ─────────────────────────────────────────────────

function useVideoThumbs(
  src: string | null | undefined,
  duration: number,
  count: number,
): (string | null)[] {
  const [thumbs, setThumbs] = useState<(string | null)[]>(() => Array(count).fill(null));
  const prevSrcRef = useRef<string | null>(null);

  useEffect(() => {
    if (!src || !src.startsWith("blob:") || duration <= 0 || count <= 0) {
      setThumbs(Array(count).fill(null));
      return;
    }
    if (prevSrcRef.current === src) return;
    prevSrcRef.current = src;
    setThumbs(Array(count).fill(null));

    const video = document.createElement("video");
    video.src = src;
    video.preload = "metadata";
    video.crossOrigin = "anonymous";
    video.muted = true;

    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const captured: (string | null)[] = Array(count).fill(null);
    let capturedCount = 0;

    function seekNext(i: number) {
      if (i >= count) return;
      video.currentTime = (i / (count - 1 || 1)) * duration;
    }

    video.addEventListener("loadedmetadata", () => {
      canvas.width = 80;
      canvas.height = Math.round(80 * (video.videoHeight / (video.videoWidth || 1)));
      seekNext(0);
    });

    video.addEventListener("seeked", () => {
      const i = Math.round((video.currentTime / duration) * (count - 1));
      try {
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        captured[i] = canvas.toDataURL("image/jpeg", 0.5);
      } catch {
        captured[i] = null;
      }
      capturedCount++;
      if (capturedCount < count) {
        seekNext(capturedCount);
      } else {
        setThumbs([...captured]);
      }
    });

    video.load();
    return () => { video.src = ""; };
  }, [src, duration, count]);

  return thumbs;
}

// ── Props ─────────────────────────────────────────────────────────────────────

export interface OverlayLaneProps {
  totalDurationS: number;
  currentTimeS: number;
  overlayCards: MediaOverlay[];
  overlaysEnabled: boolean;
  overlayUploading: boolean;
  localPreviewUrls: Record<string, string>;
  onOverlayUploadRequest: (files: UploadFile[]) => void;
  onUpdateCard: (
    id: string,
    patch: Partial<MediaOverlay>,
    options?: { record?: boolean },
  ) => void;
  onRemoveCard: (id: string) => void;
  onClearOverlays: () => void;
  /** Pending AI suggestions rendered as editable provenance cards (006 T3). */
  suggestions?: SuggestionLaneEntry[];
  /** Lane edit on a suggestion card — patches the staged envelope, no network. */
  onSuggestionEdit?: (suggestionId: string, patch: Partial<MediaOverlay>) => void;
  /**
   * 009 T3: intro-text window rendered as a hatched zinc keep-out band on the
   * lane and used for the "Covers your intro text" fullscreen warning. Timing
   * comes from the variant's intro fields upstream (page.tsx owns the wiring
   * via UnifiedTimeline) — the lane never derives its own copy.
   */
  introTextWindow?: { start_s: number; end_s: number } | null;
  /**
   * 009 T3: resolves aspect/pixel metadata for an overlay's src_gcs_path so
   * the fullscreen popover can raise crop/low-res warnings. Optional — those
   * warnings are suppressed (never faked) when the resolver or a field is
   * absent. Wired by the page-owning side.
   */
  resolveAssetMeta?: (srcGcsPath: string) => OverlayAssetMeta | undefined;
  /**
   * 009 T5 (D5/E9): when set, fullscreen promotion is unavailable on this
   * variant (lyrics) — forwarded to the popover, which disables the
   * "Full screen" option with this copy; the chip-level F promote is guarded
   * here too. Demote paths stay live for legacy fullscreen cards.
   */
  fullscreenDisabledReason?: string | null;
  /**
   * R2 (review C8): web twin of the api FULLSCREEN_CUTAWAYS_ENABLED. When false,
   * the NEW fullscreen PROMOTE affordances (popover "Full screen" option, the
   * "Make full screen →" max-scale affordance, and the F-to-fullscreen chip
   * shortcut) are hidden/no-op so a previewed fullscreen can't bake as pip
   * against an api that predates display_mode. Demote paths + existing
   * fullscreen cards are unaffected. Defaults true (pre-flag behavior).
   */
  fullscreenPromoteEnabled?: boolean;
  /**
   * 009 T3 external-edit contract: when this changes to a card id present in
   * the lane, that card's popover opens and onExternalEditHandled() fires
   * (hero preview click-to-edit — the page owns the handoff state).
   */
  externalEditCardId?: string | null;
  onExternalEditHandled?: () => void;
}

/** Internal render entry: a manual card or a suggestion's embedded overlay. */
interface LaneCardEntry {
  card: MediaOverlay;
  /** Envelope id when this entry is an AI suggestion; null for manual cards. */
  suggestionId: string | null;
  staged: boolean;
}

type ActiveOverlayDrag = OverlayDragState & {
  pointerId: number;
  pointerType: string;
  startY: number;
  latestX: number;
  latestY: number;
  intent: "pending" | "dragging";
  livePatch: Partial<MediaOverlay>;
};

function tapSlopFor(pointerType: string): number {
  return pointerType === "touch" ? TOUCH_TAP_SLOP_PX : MOUSE_TAP_SLOP_PX;
}

function handleHitStyle(
  side: "left" | "right",
  chipPx: number,
  coarsePointer: boolean,
): CSSProperties {
  if (!coarsePointer) {
    return side === "left"
      ? { width: FINE_HANDLE_HIT_PX, left: 0 }
      : { width: FINE_HANDLE_HIT_PX, right: 0 };
  }
  const measured = Number.isFinite(chipPx) ? chipPx : HANDLE_HIT_PX;
  const width = measured < HANDLE_HIT_PX
    ? HANDLE_HIT_PX / 2 + measured / 2
    : HANDLE_HIT_PX;
  return side === "left"
    ? { width, left: -HANDLE_HIT_PX / 2 }
    : { width, right: -HANDLE_HIT_PX / 2 };
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function OverlayLane({
  totalDurationS,
  currentTimeS,
  overlayCards,
  overlaysEnabled,
  overlayUploading,
  localPreviewUrls,
  onOverlayUploadRequest,
  onUpdateCard,
  onRemoveCard,
  onClearOverlays,
  suggestions,
  onSuggestionEdit,
  introTextWindow,
  resolveAssetMeta,
  fullscreenDisabledReason,
  fullscreenPromoteEnabled = true,
  externalEditCardId,
  onExternalEditHandled,
}: OverlayLaneProps) {
  // Manual cards first (their TRACK_COLORS indices stay byte-identical),
  // suggestion cards appended with provenance styling.
  const laneCards: LaneCardEntry[] = [
    ...overlayCards.map((card) => ({ card, suggestionId: null, staged: false })),
    ...(suggestions ?? []).map((s) => ({
      card: s.overlay,
      suggestionId: s.id,
      staged: s.staged,
    })),
  ];

  /** Route a patch: suggestion cards → staged envelope; manual → media_overlays. */
  function patchCard(entry: LaneCardEntry, patch: Partial<MediaOverlay>) {
    if (entry.suggestionId != null) onSuggestionEdit?.(entry.suggestionId, patch);
    else onUpdateCard(entry.card.id, patch);
  }

  /** Total seconds of manual fullscreen coverage — the >15s popover warning. */
  const manualFullscreenTotalS = overlayCards
    .filter((c) => (c.display_mode ?? "pip") === "fullscreen")
    .reduce((acc, c) => acc + Math.max(0, c.end_s - c.start_s), 0);

  // ── Per-card open state ───────────────────────────────────────────────────────

  const [openCardId, setOpenCardId] = useState<string | null>(null);

  // 009 T3 external-edit contract: the hero preview's click-to-edit hands us a
  // card id; if it's in the lane, open its popover and ack the handoff.
  useEffect(() => {
    if (!externalEditCardId) return;
    if (laneCards.some((entry) => entry.card.id === externalEditCardId)) {
      setOpenCardId(externalEditCardId);
      onExternalEditHandled?.();
    }
    // laneCards is rebuilt every render; the contract keys off the id change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [externalEditCardId]);

  // ── Overlay drag ─────────────────────────────────────────────────────────────

  const [overlayDrag, setOverlayDrag] = useState<ActiveOverlayDrag | null>(null);
  const overlayLaneRef = useRef<HTMLDivElement | null>(null);
  // Fullscreen hard-stop bounds, frozen at gesture start (blockers don't move
  // during a drag). A ref so mid-drag effect re-subscriptions can't lose it.
  const gapBoundsRef = useRef<{ lower: number; upper: number }>({ lower: 0, upper: Infinity });
  const activePointerIdRef = useRef<number | null>(null);
  // History-owning parents snapshot state BEFORE applying a record:true patch
  // (EditorShell.history.record, timeline reducers), so the FIRST patch of a
  // gesture must carry record:true (capturing the pre-drag state) and every
  // later patch — including the pointerup/pointercancel commit — record:false.
  // A ref, not drag.intent: burst moves in one task see a stale closure intent.
  const dragRecordedRef = useRef(false);

  const patchForOverlayDrag = useCallback((drag: ActiveOverlayDrag, clientX: number): Partial<MediaOverlay> => {
    const MIN_DUR = 0.1;
    const dx = clientX - drag.startX;
    const ds = drag.containerWidth > 0
      ? (dx / drag.containerWidth) * drag.scaleDuration
      : 0;
    const clipDur = drag.clipDurationS;
    const bounds = gapBoundsRef.current;

    switch (drag.handle) {
      case "move": {
        const dur = drag.origEnd - drag.origStart;
        const lo = Math.max(0, bounds.lower);
        const hi = Math.max(lo, Math.min(totalDurationS - dur, bounds.upper - dur));
        const ns = Math.max(lo, Math.min(hi, drag.origStart + ds));
        return {
          start_s: Math.round(ns * 10) / 10,
          end_s: Math.round((ns + dur) * 10) / 10,
        };
      }
      case "left": {
        const minStart = Math.max(
          bounds.lower,
          Math.max(0, clipDur != null ? drag.origEnd - drag.origTrimEnd : 0),
        );
        const ns = Math.max(minStart, Math.min(drag.origEnd - MIN_DUR, drag.origStart + ds));
        if (clipDur != null) {
          const newTrimStart = Math.max(0, drag.origTrimEnd - (drag.origEnd - ns));
          return { start_s: Math.round(ns * 10) / 10, clip_trim_start_s: Math.round(newTrimStart * 10) / 10 };
        }
        return { start_s: Math.round(ns * 10) / 10 };
      }
      case "right": {
        const maxEnd = Math.min(
          bounds.upper,
          clipDur != null
            ? Math.min(totalDurationS, drag.origStart + (clipDur - drag.origTrimStart))
            : totalDurationS,
        );
        const ne = Math.min(maxEnd, Math.max(drag.origStart + MIN_DUR, drag.origEnd + ds));
        if (clipDur != null) {
          const newTrimEnd = Math.min(clipDur, drag.origTrimStart + (ne - drag.origStart));
          return { end_s: Math.round(ne * 10) / 10, clip_trim_end_s: Math.round(newTrimEnd * 10) / 10 };
        }
        return { end_s: Math.round(ne * 10) / 10 };
      }
      case "trim-left": {
        const ns = Math.max(0, Math.min(drag.origTrimEnd - MIN_DUR, drag.origTrimStart + ds));
        const newDur = drag.origTrimEnd - ns;
        const newEnd = Math.min(totalDurationS, bounds.upper, drag.origStart + newDur);
        const actualDur = newEnd - drag.origStart;
        const actualTrimStart = Math.max(0, drag.origTrimEnd - actualDur);
        return { clip_trim_start_s: Math.round(actualTrimStart * 10) / 10, end_s: Math.round(newEnd * 10) / 10 };
      }
      case "trim-right": {
        const ne = Math.min(
          drag.scaleDuration,
          Math.max(drag.origTrimStart + MIN_DUR, drag.origTrimEnd + ds),
        );
        const newDur = ne - drag.origTrimStart;
        const newEnd = Math.min(totalDurationS, bounds.upper, drag.origStart + newDur);
        const actualDur = newEnd - drag.origStart;
        const actualTrimEnd = drag.origTrimStart + actualDur;
        return { clip_trim_end_s: Math.round(actualTrimEnd * 10) / 10, end_s: Math.round(newEnd * 10) / 10 };
      }
    }
  }, [totalDurationS]);

  const applyOverlayDragPatch = useCallback((
    drag: ActiveOverlayDrag,
    patch: Partial<MediaOverlay>,
    options: { record?: boolean } = {},
  ) => {
    // Suggestion drags patch the staged envelope (no manual-state mutation,
    // no network); manual drags keep the original onUpdateCard path.
    if (drag.suggestionId != null) {
      onSuggestionEdit?.(drag.suggestionId, patch);
    } else {
      onUpdateCard(drag.cardId, patch, options);
    }
  }, [onSuggestionEdit, onUpdateCard]);

  useEffect(() => {
    if (!overlayDrag) return;
    const drag = overlayDrag;

    function onMove(e: PointerEvent) {
      if (activePointerIdRef.current !== e.pointerId) return;
      if (e.pointerId !== drag.pointerId) return;
      const dx = e.clientX - drag.startX;
      const dy = e.clientY - drag.startY;
      const distance = Math.hypot(dx, dy);
      const slop = tapSlopFor(drag.pointerType);
      const nextBase = { latestX: e.clientX, latestY: e.clientY };

      if (drag.intent === "pending") {
        if (distance < slop) {
          setOverlayDrag((cur) => cur?.pointerId === e.pointerId ? { ...cur, ...nextBase } : cur);
          return;
        }
        if (drag.handle === "move" && drag.pointerType === "touch" && Math.abs(dx) <= Math.abs(dy)) {
          activePointerIdRef.current = null;
          setOverlayDrag(null);
          return;
        }
      }

      const patch = patchForOverlayDrag(drag, e.clientX);
      const record = !dragRecordedRef.current;
      if (record) dragRecordedRef.current = true;
      applyOverlayDragPatch(drag, patch, { record });
      setOverlayDrag((cur) =>
        cur?.pointerId === e.pointerId
          ? { ...cur, ...nextBase, intent: "dragging", livePatch: patch }
          : cur,
      );
    }

    function finish(e: PointerEvent) {
      if (activePointerIdRef.current !== e.pointerId) return;
      if (e.pointerId !== drag.pointerId) return;
      const dx = e.clientX - drag.startX;
      const dy = e.clientY - drag.startY;
      const distance = Math.hypot(dx, dy);
      const slop = tapSlopFor(drag.pointerType);
      const isTap = distance < slop;
      const reachedHorizontalIntent =
        distance >= slop &&
        (drag.handle !== "move" || drag.pointerType !== "touch" || Math.abs(dx) > Math.abs(dy));

      if (drag.intent === "dragging" || reachedHorizontalIntent) {
        const patch = Object.keys(drag.livePatch).length > 0
          ? drag.livePatch
          : patchForOverlayDrag(drag, e.clientX);
        const record = !dragRecordedRef.current;
        if (record) dragRecordedRef.current = true;
        applyOverlayDragPatch(drag, patch, { record });
      } else if (e.type !== "pointercancel" && drag.handle === "move" && isTap) {
        // A body press that stayed within pointer-type slop is a tap — toggle
        // the popover so glyph-less tiny chips still have an open path.
        const id = drag.cardId;
        setOpenCardId((prev) => (prev === id ? null : id));
      }
      activePointerIdRef.current = null;
      setOverlayDrag(null);
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
    };
  }, [applyOverlayDragPatch, overlayDrag, patchForOverlayDrag]);

  function startOverlayDrag(
    e: React.PointerEvent<HTMLElement>,
    cardId: string,
    handle: OverlayDragState["handle"],
    card: MediaOverlay,
    containerEl: HTMLElement | null,
    suggestionId: string | null = null,
  ) {
    if (activePointerIdRef.current != null) {
      e.preventDefault();
      e.stopPropagation();
      return;
    }
    if (e.pointerType === "mouse" && e.button !== 0) return;
    if (e.pointerType !== "touch" || handle !== "move") e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    const rect = (containerEl ?? overlayLaneRef.current)?.getBoundingClientRect();
    const isTrim = handle === "trim-left" || handle === "trim-right";
    const clipDur = card.kind === "video" ? (card.clip_duration_s ?? null) : null;
    activePointerIdRef.current = e.pointerId;
    dragRecordedRef.current = false;
    // 009 T3: freeze the fullscreen hard-stop bounds for this gesture.
    gapBoundsRef.current = fullscreenGapBounds({
      movingId: card.id,
      movingFullscreen: (card.display_mode ?? "pip") === "fullscreen",
      origStart: card.start_s,
      origEnd: card.end_s,
      cards: laneCards.map((entry) => entry.card),
      totalDurationS,
    });
    setOverlayDrag({
      cardId,
      handle,
      pointerId: e.pointerId,
      pointerType: e.pointerType,
      startX: e.clientX,
      startY: e.clientY,
      latestX: e.clientX,
      latestY: e.clientY,
      intent: "pending",
      livePatch: {},
      origStart: card.start_s,
      origEnd: card.end_s,
      origTrimStart: card.clip_trim_start_s ?? 0,
      origTrimEnd: card.clip_trim_end_s ?? (clipDur ?? card.end_s - card.start_s),
      containerWidth: rect?.width ?? 0,
      scaleDuration: isTrim ? (clipDur ?? 10) : totalDurationS,
      clipDurationS: clipDur,
      suggestionId,
    });
  }

  // ── Overlay upload ────────────────────────────────────────────────────────────

  const overlayFileInputRef = useRef<HTMLInputElement | null>(null);
  const [overlayDragOver, setOverlayDragOver] = useState(false);

  function handleOverlayFiles(fileList: FileList | null) {
    if (!fileList || fileList.length === 0) return;
    const valid: UploadFile[] = [];
    for (const file of Array.from(fileList)) {
      if (!ALLOWED_OVERLAY_MIME_TYPES.includes(file.type)) continue;
      valid.push({ file, filename: file.name, content_type: file.type, file_size_bytes: file.size });
    }
    if (valid.length > 0) onOverlayUploadRequest(valid);
  }

  // ── Fullscreen trim-outrun snap (009 T3) ─────────────────────────────────────
  // A manual fullscreen VIDEO window may never outrun its trimmed footage —
  // hard-snap end_s (snap, not freeze). Typically triggered by a mode flip on
  // a card whose pip window relied on the plan-006 freeze. Suggestions are
  // left alone (the server's rule (e) owns AI cards).
  const outrunSig = laneCards
    .map(({ card: c }) =>
      [c.id, c.display_mode ?? "pip", c.start_s, c.end_s,
        c.clip_trim_start_s ?? "", c.clip_trim_end_s ?? "", c.clip_duration_s ?? ""].join(":"),
    )
    .join("|");
  useEffect(() => {
    for (const entry of laneCards) {
      if (entry.suggestionId != null) continue;
      const snapEnd = fullscreenOutrunSnapEnd(entry.card);
      if (snapEnd != null) patchCard(entry, { end_s: snapEnd });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outrunSig]);

  // ── Lane pixel width (tiny-chip degradation, 009 T3) ─────────────────────────

  const [laneWidthPx, setLaneWidthPx] = useState(0);
  const [coarsePointer, setCoarsePointer] = useState(false);
  useEffect(() => {
    const el = overlayLaneRef.current;
    if (!el) return;
    const measure = () => setLaneWidthPx(el.getBoundingClientRect().width);
    measure();
    if (typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver(measure);
      ro.observe(el);
      return () => ro.disconnect();
    }
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(pointer: coarse)");
    const update = () => setCoarsePointer(media.matches);
    update();
    media.addEventListener?.("change", update);
    return () => media.removeEventListener?.("change", update);
  }, []);

  // ── Render ────────────────────────────────────────────────────────────────────

  return (
    <div>
      {/* Lane header row */}
      <div className="flex h-5 items-center">
        <div className="flex-shrink-0 w-14 flex items-center justify-end pr-2">
          <span className="text-[9px] font-semibold text-zinc-400 uppercase tracking-wider">
            Overlays
          </span>
        </div>
        <div
          ref={overlayLaneRef}
          className="relative flex-1 touch-pan-y bg-zinc-800/15 border-y border-zinc-700/30 h-full"
        >
          {/* 009 T3: hatched zinc keep-out band over the intro-text window —
              fullscreen cards placed here trigger "Covers your intro text". */}
          {introTextWindow != null &&
            totalDurationS > 0 &&
            introTextWindow.end_s > introTextWindow.start_s && (
              <div
                aria-hidden
                data-testid="intro-text-band"
                className="absolute top-0 h-full pointer-events-none"
                style={{
                  left: `${(Math.max(0, introTextWindow.start_s) / totalDurationS) * 100}%`,
                  width: `${(Math.min(totalDurationS, introTextWindow.end_s - Math.max(0, introTextWindow.start_s)) / totalDurationS) * 100}%`,
                  backgroundImage:
                    "repeating-linear-gradient(45deg, rgba(113,113,122,0.3) 0px, rgba(113,113,122,0.3) 4px, transparent 4px, transparent 8px)",
                }}
              />
            )}
          <Playhead currentTimeS={currentTimeS} totalDurationS={totalDurationS} />
        </div>
      </div>

      {/* Per-card timing tracks — manual cards + suggestion provenance cards */}
      {laneCards.length > 0 && (
        <div className="ml-14 flex flex-col gap-1 py-1">
          {laneCards.map((entry, i) => {
            const { card, suggestionId, staged } = entry;
            const isSuggestion = suggestionId != null;
            const isFullscreen = (card.display_mode ?? "pip") === "fullscreen";
            const color = TRACK_COLORS[i % TRACK_COLORS.length];
            const lPct = totalDurationS > 0 ? (card.start_s / totalDurationS) * 100 : 0;
            const wPct = totalDurationS > 0
              ? Math.max(((card.end_s - card.start_s) / totalDurationS) * 100, 1)
              : 1;
            // Rendered chip width — Infinity while unmeasured so chips never
            // flash into the degraded state before the first layout pass.
            const chipPx = laneWidthPx > 0 ? (wPct / 100) * laneWidthPx : Number.POSITIVE_INFINITY;
            // Below ~24px a fullscreen chip drops its glyph (the ink fill is
            // the identifier) and its edge handles (timing edits via popover).
            const tinyFullscreen = isFullscreen && chipPx < TINY_CHIP_PX;
            const coarseTinyChip =
              coarsePointer &&
              chipPx < Math.max(COARSE_TINY_CHIP_PX, HANDLE_HIT_PX + MIN_COARSE_CHIP_BODY_PX);
            const suppressChipControls = tinyFullscreen || coarseTinyChip;
            const activeHandle = overlayDrag?.cardId === card.id ? overlayDrag.handle : null;
            const activePatch = overlayDrag?.cardId === card.id ? overlayDrag.livePatch : null;
            const liveStart = activePatch?.start_s ?? card.start_s;
            const liveEnd = activePatch?.end_s ?? card.end_s;
            const liveDuration = Math.max(0, liveEnd - liveStart);
            const liveThumbPct =
              activeHandle === "right" || activeHandle === "trim-right"
                ? 100
                : activeHandle === "left" || activeHandle === "trim-left"
                  ? 0
                  : 50;
            const isCardPointerActive = overlayDrag?.cardId === card.id;
            const isOpen = openCardId === card.id;

            return (
              <div key={suggestionId ?? card.id}>
                {/* Timing bar — fullscreen chips get the taller h-8 track row */}
                <div className={`relative ${isFullscreen ? "h-8" : "h-6"}`}>
                  <div className="absolute inset-0 rounded bg-white/5" />
                  <div
                    // Provenance (006 T3 / DESIGN §12): dashed lime-600 + ✦ while
                    // pending; staged flips the border solid (005-6A accept).
                    // 009 T3: the provenance treatment layers over either display
                    // mode unchanged — lime stays exclusively provenance; the
                    // fullscreen identifier is the solid ink fill, never lime.
                    className={`absolute top-0 h-full touch-pan-y rounded flex items-center overflow-visible transition-opacity ${
                      isCardPointerActive ? "opacity-100" : "opacity-70 hover:opacity-90"
                    } focus-visible:outline focus-visible:outline-2${
                      isSuggestion
                        ? ` border-[1.5px] border-lime-600${isFullscreen ? "" : " bg-lime-600/30"} focus-visible:outline-lime-500 ${
                            staged ? "border-solid" : "border-dashed"
                          }`
                        : " focus-visible:outline-white/70"
                    }`}
                    style={{
                      left: `${lPct}%`,
                      width: `${wPct}%`,
                      backgroundColor: isFullscreen ? INK : isSuggestion ? undefined : color,
                      cursor: "grab",
                    }}
                    tabIndex={0}
                    role="button"
                    aria-label={
                      isSuggestion
                        ? `Suggested overlay ${card.id.slice(0, 6)}, ${(card.start_s ?? 0).toFixed(1)}s to ${(card.end_s ?? 0).toFixed(1)}s`
                        : isFullscreen
                          ? `Full-screen cutaway, ${(card.start_s ?? 0).toFixed(1)} to ${(card.end_s ?? 0).toFixed(1)} seconds`
                          : `Visual card, ${(card.start_s ?? 0).toFixed(1)} to ${(card.end_s ?? 0).toFixed(1)} seconds`
                    }
                    data-suggestion-card={suggestionId ?? undefined}
                    data-overlay-chip={card.id}
                    data-display-mode={isFullscreen ? "fullscreen" : "pip"}
                    onPointerDown={(e) => startOverlayDrag(e, card.id, "move", card, overlayLaneRef.current, suggestionId)}
                    onKeyDown={(e) => {
                      const t = e.target as HTMLElement;
                      if (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable) return;
                      if (e.key === "Enter" || e.key === " ") {
                        // R4 (C10): every focusable chip — manual OR suggestion —
                        // opens its popover on Enter/Space (WCAG 2.1.1). Suggestion
                        // popover edits route through onSuggestionEdit, so opening
                        // is safe (this was a keyboard-inoperable no-op before, so
                        // keyboard users could focus a suggestion but not edit it).
                        e.preventDefault();
                        setOpenCardId(isOpen ? null : card.id);
                      } else if (e.key === "f" || e.key === "F") {
                        // F promote/demote stays MANUAL-chip only (006 T3): a
                        // suggestion's mode is edited through its popover +
                        // rail controls, not a bare chip shortcut. Enter/Space
                        // above already gives keyboard users the popover.
                        if (isSuggestion) return;
                        // D5/E9: never promote while fullscreen is disabled on
                        // this variant (lyrics). R2/C8: also never promote while
                        // the fullscreen-cutaways flag is off (old-api skew).
                        // Demote of an existing fullscreen card stays available
                        // in both cases.
                        e.preventDefault();
                        e.stopPropagation();
                        if (!isFullscreen && (fullscreenDisabledReason != null || !fullscreenPromoteEnabled)) return;
                        patchCard(entry, isFullscreen ? demotePatch(card) : { display_mode: "fullscreen" });
                      }
                    }}
                  >
                    {!suppressChipControls && (
                      <div
                        className={`absolute top-1/2 z-10 flex ${
                          coarsePointer ? "h-11" : "h-full"
                        } -translate-y-1/2 touch-none items-center justify-center hover:bg-black/30 ${
                          activeHandle === "left" ? "z-20 scale-105 bg-black/40" : ""
                        }`}
                        style={{ ...handleHitStyle("left", chipPx, coarsePointer), cursor: "ew-resize" }}
                        aria-label="Trim overlay start"
                        role="button"
                        tabIndex={-1}
                        data-chip-handle={`left-${card.id}`}
                        onPointerDown={(e) => {
                          e.stopPropagation();
                          startOverlayDrag(e, card.id, "left", card, overlayLaneRef.current, suggestionId);
                        }}
                      >
                        <div className={`h-4 w-3 rounded-sm ${activeHandle === "left" ? "bg-white" : "bg-white/70"}`} />
                      </div>
                    )}
                    <span
                      className="min-w-0 truncate px-3 text-[10px] font-medium text-white"
                      onMouseDown={(e) => e.stopPropagation()}
                      onPointerDown={(e) => e.stopPropagation()}
                      onClick={(e) => { e.stopPropagation(); setOpenCardId(isOpen ? null : card.id); }}
                    >
                      {isSuggestion && (
                        <span
                          aria-hidden
                          data-testid={`suggestion-badge-${suggestionId}`}
                          className={`motion-safe:transition-opacity motion-safe:duration-300 ${
                            staged ? "opacity-0" : "opacity-100"
                          }`}
                        >
                          ✦{" "}
                        </span>
                      )}
                      {suppressChipControls
                        ? null
                        : isFullscreen
                          ? "⛶ Full"
                          : `${card.kind === "video" ? "▶" : "⊞"} ${card.id.slice(0, 6)}`}
                    </span>
                    {!suppressChipControls && (
                      <div
                        className={`absolute top-1/2 z-10 flex ${
                          coarsePointer ? "h-11" : "h-full"
                        } -translate-y-1/2 touch-none items-center justify-center hover:bg-black/30 ${
                          activeHandle === "right" ? "z-20 scale-105 bg-black/40" : ""
                        }`}
                        data-chip-handle={`right-${card.id}`}
                        aria-label="Trim overlay end"
                        role="button"
                        tabIndex={-1}
                        onPointerDown={(e) => {
                          e.stopPropagation();
                          startOverlayDrag(e, card.id, "right", card, overlayLaneRef.current, suggestionId);
                        }}
                        style={{ ...handleHitStyle("right", chipPx, coarsePointer), cursor: "ew-resize" }}
                      >
                        <div className={`h-4 w-3 rounded-sm ${activeHandle === "right" ? "bg-white" : "bg-white/70"}`} />
                      </div>
                    )}
                    {overlayDrag?.cardId === card.id && overlayDrag.intent === "dragging" && (
                      <div
                        className="pointer-events-none absolute bottom-full mb-1 -translate-x-1/2 rounded-full bg-[#0c0c0e] px-2 py-1 font-mono text-[10px] text-white shadow-lg"
                        style={{
                          left: `${Math.max(0, Math.min(100, liveThumbPct))}%`,
                        }}
                      >
                        {fmtTime(liveStart)}–{fmtTime(liveEnd)} · {liveDuration.toFixed(1)}s
                      </div>
                    )}
                  </div>
                </div>

                {/* Per-card popover (extracted — 009 T3) */}
                {isOpen && (
                  <OverlayCardPopover
                    card={card}
                    isSuggestion={isSuggestion}
                    totalDurationS={totalDurationS}
                    introTextWindow={introTextWindow}
                    assetMeta={resolveAssetMeta?.(card.src_gcs_path)}
                    manualFullscreenTotalS={manualFullscreenTotalS}
                    fullscreenDisabledReason={fullscreenDisabledReason}
                    fullscreenPromoteEnabled={fullscreenPromoteEnabled}
                    onPatch={(patch) => patchCard(entry, patch)}
                    onRemove={
                      !isSuggestion
                        ? () => { onRemoveCard(card.id); setOpenCardId(null); }
                        : undefined
                    }
                  />
                )}

                {/* Video trim lane */}
                {card.kind === "video" && card.clip_duration_s && card.clip_duration_s > 0 && (
                  <TrimLane
                    card={card}
                    videoSrc={localPreviewUrls[card.id] ?? card.preview_url ?? null}
                    clipDur={card.clip_duration_s}
                    trimStart={card.clip_trim_start_s ?? 0}
                    trimEnd={card.clip_trim_end_s ?? card.clip_duration_s}
                    isTrimDragging={
                      overlayDrag?.cardId === card.id &&
                      (overlayDrag.handle === "trim-left" || overlayDrag.handle === "trim-right")
                    }
                    activeHandle={activeHandle}
                    coarsePointer={coarsePointer}
                    onTrimLeftDown={(e) => startOverlayDrag(e, card.id, "trim-left", card, null, suggestionId)}
                    onTrimRightDown={(e) => startOverlayDrag(e, card.id, "trim-right", card, null, suggestionId)}
                  />
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Overlay upload zone */}
      {overlaysEnabled && (
        <div className="ml-14 mt-1 mb-2">
          <div
            className={`rounded-lg border border-dashed p-2 text-center transition-colors cursor-pointer text-xs ${
              overlayDragOver
                ? "border-violet-400 bg-violet-400/10"
                : "border-white/20 hover:border-white/40 text-white/40"
            } ${overlayUploading ? "opacity-40 pointer-events-none" : ""}`}
            onClick={() => overlayFileInputRef.current?.click()}
            onDragOver={(e) => { e.preventDefault(); setOverlayDragOver(true); }}
            onDragLeave={() => setOverlayDragOver(false)}
            onDrop={(e) => { e.preventDefault(); setOverlayDragOver(false); handleOverlayFiles(e.dataTransfer.files); }}
          >
            <input
              ref={overlayFileInputRef}
              type="file"
              multiple
              accept={ALLOWED_OVERLAY_MIME_TYPES.join(",")}
              className="hidden"
              onChange={(e) => { handleOverlayFiles(e.target.files); e.target.value = ""; }}
            />
            {overlayUploading ? (
              <div
                role="status"
                aria-live="polite"
                className="flex min-h-11 items-center justify-center gap-2 text-lime-700"
              >
                <span className="relative flex h-2.5 w-2.5" aria-hidden>
                  <span className="absolute inline-flex h-full w-full rounded-full bg-lime-600 opacity-70 motion-safe:animate-ping" />
                  <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-lime-600" />
                </span>
                <span className="font-display text-sm text-white/70">
                  Uploading your clip…
                </span>
              </div>
            ) : (
              "Drop image/video overlay or click to browse"
            )}
          </div>
          {overlayCards.length > 0 && (
            <button
              type="button"
              onClick={onClearOverlays}
              className="mt-1 min-h-11 px-2 text-[10px] text-white/30 transition-colors hover:text-white/60 sm:min-h-0 sm:px-0"
            >
              Clear all overlays
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ── TrimLane ──────────────────────────────────────────────────────────────────

interface TrimLaneProps {
  card: MediaOverlay;
  videoSrc: string | null;
  clipDur: number;
  trimStart: number;
  trimEnd: number;
  isTrimDragging: boolean;
  activeHandle: ActiveOverlayDrag["handle"] | null;
  coarsePointer: boolean;
  onTrimLeftDown: (e: React.PointerEvent<HTMLElement>) => void;
  onTrimRightDown: (e: React.PointerEvent<HTMLElement>) => void;
}

function TrimLane({
  card,
  videoSrc,
  clipDur,
  trimStart,
  trimEnd,
  isTrimDragging,
  activeHandle,
  coarsePointer,
  onTrimLeftDown,
  onTrimRightDown,
}: TrimLaneProps) {
  const thumbs = useVideoThumbs(videoSrc, clipDur, THUMB_COUNT);
  const hasAnyThumb = thumbs.some(Boolean);
  const lPct = (trimStart / clipDur) * 100;
  const wPct = Math.max(((trimEnd - trimStart) / clipDur) * 100, 1);

  return (
    <div className="mt-1 ml-0">
      <span className="text-[9px] text-white/40 mb-1 block">
        Clip trim — {card.id.slice(0, 6)} ({fmtTime(trimStart)}–{fmtTime(trimEnd)} of {fmtTime(clipDur)})
      </span>
      <div className="relative h-10 rounded overflow-hidden bg-zinc-800" data-trim-container={card.id}>
        <div className="absolute inset-0 flex">
          {thumbs.map((thumb, i) => (
            <div key={i} className="flex-1 h-full overflow-hidden border-r border-black/40">
              {thumb ? (
                <img src={thumb} className="h-full w-full object-cover" alt="" draggable={false} />
              ) : (
                <div className={`h-full ${hasAnyThumb ? "bg-zinc-700/60" : "bg-zinc-700"}`} />
              )}
            </div>
          ))}
        </div>
        <div className="absolute top-0 left-0 h-full bg-black/60 pointer-events-none" style={{ width: `${lPct}%` }} />
        <div className="absolute top-0 right-0 h-full bg-black/60 pointer-events-none" style={{ width: `${100 - lPct - wPct}%` }} />
        <div
          className={`absolute top-0 h-full overflow-visible border-2 rounded transition-colors ${isTrimDragging ? "border-white" : "border-white/60"}`}
          style={{ left: `${lPct}%`, width: `${wPct}%` }}
        >
          <div
            className={`absolute top-1/2 flex h-11 -translate-y-1/2 touch-none items-center justify-center ${
              activeHandle === "trim-left" ? "z-20 scale-105 bg-white/30" : "z-10 bg-white/20"
            }`}
            style={{ ...handleHitStyle("left", Number.POSITIVE_INFINITY, coarsePointer), cursor: "ew-resize" }}
            data-trim-handle={`left-${card.id}`}
            onPointerDown={onTrimLeftDown}
          >
            <div className={`h-5 w-3 rounded-sm ${activeHandle === "trim-left" ? "bg-white" : "bg-white/80"}`} />
          </div>
          <div
            className={`absolute top-1/2 flex h-11 -translate-y-1/2 touch-none items-center justify-center ${
              activeHandle === "trim-right" ? "z-20 scale-105 bg-white/30" : "z-10 bg-white/20"
            }`}
            style={{ ...handleHitStyle("right", Number.POSITIVE_INFINITY, coarsePointer), cursor: "ew-resize" }}
            data-trim-handle={`right-${card.id}`}
            onPointerDown={onTrimRightDown}
          >
            <div className={`h-5 w-3 rounded-sm ${activeHandle === "trim-right" ? "bg-white" : "bg-white/80"}`} />
          </div>
        </div>
      </div>
    </div>
  );
}
