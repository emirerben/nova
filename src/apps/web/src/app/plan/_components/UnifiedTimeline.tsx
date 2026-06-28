"use client";

/**
 * UnifiedTimeline — horizontal multi-lane timeline for plan-item variant editing.
 *
 * Four lanes, one shared playhead:
 *   SFX      — fully interactive (drag/trim/add/remove/undo-redo).
 *   Overlays — fully interactive (drag start_s/end_s, trim video clips,
 *               per-card popover for position/scale/remove, upload zone).
 *   Text     — expandable inline panel (click bar → toggle textPanel content).
 *   Clips    — read-only bar  (click → open "clips" tab / TimelineEditor sheet).
 *
 * All SFX mutations flow through the SFX reducer.
 * Overlay mutations flow through onUpdateCard/onRemoveCard/onClearOverlays
 * (same contracts as the retired MediaOverlayEditor).
 * Text mutations flow through textPanel (rendered inline when expanded).
 *
 * Kill switch: NEXT_PUBLIC_UNIFIED_TIMELINE_ENABLED (default on).
 */

import { useEffect, useReducer, useRef, useState } from "react";
import type { SoundEffectPlacement, MediaOverlay } from "@/lib/plan-api";
import type { SoundEffectSummary } from "@/lib/sfx-api";
import { computeBarPosition } from "@/lib/timeline/bar-position";
import { classifyZone, clampSeconds } from "@/lib/timeline/drag-zone";
import {
  initSfxEditorState,
  sfxReducer,
} from "@/lib/timeline/sfx-timeline-reducer";
import { Playhead } from "@/lib/timeline/Playhead";
import { formatTimecode } from "@/lib/timeline/time-format";

// ── Constants ─────────────────────────────────────────────────────────────────

const HANDLE_PX = 10;
const MIN_TRIM_S = 0.1;

const ALLOWED_AUDIO_MIME_TYPES = [
  "audio/mpeg",
  "audio/mp3",
  "audio/mp4",
  "audio/wav",
  "audio/x-wav",
  "audio/aac",
  "audio/ogg",
  "audio/webm",
];
const MAX_SFX_FILE_BYTES = 20 * 1024 * 1024;

const ALLOWED_OVERLAY_MIME_TYPES = [
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/heic",
  "video/mp4",
  "video/quicktime",
];

const TRACK_COLORS = ["#8B5CF6", "#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#EC4899"];

const POSITION_PRESETS = [
  { label: "Top", value: "top" as const },
  { label: "Center", value: "center" as const },
  { label: "Bottom", value: "bottom" as const },
];

const THUMB_COUNT = 10;
const MIN_SCALE = 0.05;
const MAX_SCALE = 1.0;

// ── Types ─────────────────────────────────────────────────────────────────────

interface UploadFile {
  file: File;
  filename: string;
  content_type: string;
  file_size_bytes: number;
}

export interface UnifiedTimelineProps {
  totalDurationS: number;
  currentTimeS: number;
  // SFX -----------------------------------------------------------------------
  sfxPlacements: SoundEffectPlacement[];
  sfxGlossaryEffects: SoundEffectSummary[];
  sfxGlossaryLoading: boolean;
  sfxRendering: boolean;
  sfxUploading: boolean;
  onSfxChange: (placements: SoundEffectPlacement[]) => void;
  onSfxUploadRequest: (files: UploadFile[]) => Promise<void>;
  // Overlays (interactive) ----------------------------------------------------
  overlayCards: MediaOverlay[];
  overlaysEnabled: boolean;
  overlayUploading: boolean;
  localPreviewUrls: Record<string, string>;
  onOverlayUploadRequest: (files: UploadFile[]) => void;
  onUpdateCard: (id: string, patch: Partial<MediaOverlay>) => void;
  onRemoveCard: (id: string) => void;
  onClearOverlays: () => void;
  // Text lane (inline editing) -----------------------------------------------
  hasText: boolean;
  /** Inline text/font editing controls — rendered inside the Text lane when expanded. */
  textPanel?: React.ReactNode;
  /** Called when the Text lane expands or collapses — parent can use to switch hero mode. */
  onTextPanelChange?: (open: boolean) => void;
  // Clips lane (inline editing) ----------------------------------------------
  /** Inline clips editor — rendered inside the Clips lane when expanded. */
  clipsPanel?: React.ReactNode;
  /** Called when the Clips lane expands or collapses. */
  onClipsPanelChange?: (open: boolean) => void;
}

// ── SFX drag ─────────────────────────────────────────────────────────────────

interface SfxDragState {
  id: string;
  handle: "body" | "left" | "right";
  startClientX: number;
  startAtS: number;
  startEndS: number;
  startTrimStartS: number;
  startTrimEndS: number;
  durationS: number | null;
  previewAtS: number;
  previewEndS: number;
}

// ── Overlay drag ──────────────────────────────────────────────────────────────

interface OverlayDragState {
  cardId: string;
  handle: "move" | "left" | "right" | "trim-left" | "trim-right";
  startX: number;
  origStart: number;
  origEnd: number;
  origTrimStart: number;
  origTrimEnd: number;
  containerWidth: number;
  scaleDuration: number;
  clipDurationS: number | null;
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

// ── Helpers ───────────────────────────────────────────────────────────────────

function tickIntervalFor(totalS: number): number {
  if (totalS <= 10) return 1;
  if (totalS <= 30) return 2;
  if (totalS <= 60) return 5;
  if (totalS <= 120) return 10;
  return 15;
}

function fmtTime(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function UnifiedTimeline({
  totalDurationS,
  currentTimeS,
  sfxPlacements,
  sfxGlossaryEffects,
  sfxGlossaryLoading,
  sfxRendering,
  sfxUploading,
  onSfxChange,
  onSfxUploadRequest,
  overlayCards,
  overlaysEnabled,
  overlayUploading,
  localPreviewUrls,
  onOverlayUploadRequest,
  onUpdateCard,
  onRemoveCard,
  onClearOverlays,
  hasText,
  textPanel,
  onTextPanelChange,
  clipsPanel,
  onClipsPanelChange,
}: UnifiedTimelineProps) {
  const [textOpen, setTextOpen] = useState(false);
  const [clipsOpen, setClipsOpen] = useState(false);

  function toggleTextOpen() {
    const next = !textOpen;
    setTextOpen(next);
    onTextPanelChange?.(next);
  }

  function toggleClipsOpen() {
    const next = !clipsOpen;
    setClipsOpen(next);
    onClipsPanelChange?.(next);
  }
  // ── SFX reducer (undo/redo) ─────────────────────────────────────────────────

  const lastEmitted = useRef<SoundEffectPlacement[]>(sfxPlacements);

  const [editorState, dispatch] = useReducer(
    sfxReducer,
    undefined,
    () => initSfxEditorState(sfxPlacements),
  );

  useEffect(() => {
    if (editorState.placements === lastEmitted.current) return;
    lastEmitted.current = editorState.placements;
    onSfxChange(editorState.placements);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editorState.placements]);

  useEffect(() => {
    if (sfxPlacements === lastEmitted.current) return;
    dispatch({ type: "RESET", placements: sfxPlacements });
    lastEmitted.current = sfxPlacements;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sfxPlacements]);

  const placements = editorState.placements;
  const canUndo = editorState.past.length > 0;
  const canRedo = editorState.future.length > 0;

  // ── SFX drag ────────────────────────────────────────────────────────────────

  const [sfxDrag, setSfxDrag] = useState<SfxDragState | null>(null);
  const laneContentRef = useRef<HTMLDivElement | null>(null);

  function pxToS(deltaX: number, ref: React.RefObject<HTMLDivElement | null>): number {
    const rect = ref.current?.getBoundingClientRect();
    if (!rect || rect.width === 0 || totalDurationS <= 0) return 0;
    return (deltaX / rect.width) * totalDurationS;
  }

  function handleBarPointerDown(
    e: React.PointerEvent<HTMLDivElement>,
    p: SoundEffectPlacement,
  ) {
    if (sfxRendering) return;
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);

    const zone = classifyZone(e.clientX, e.currentTarget.getBoundingClientRect(), HANDLE_PX);
    const durationS = p.duration_s ?? null;
    const trimStartS = p.trim_start_s ?? 0;
    const trimEndS = p.trim_end_s ?? (durationS ?? 0);
    const effectiveDur = durationS ? trimEndS - trimStartS : 0;
    const endS = p.at_s + effectiveDur;

    setSfxDrag({
      id: p.id,
      handle: zone,
      startClientX: e.clientX,
      startAtS: p.at_s,
      startEndS: endS,
      startTrimStartS: trimStartS,
      startTrimEndS: trimEndS,
      durationS,
      previewAtS: p.at_s,
      previewEndS: endS,
    });
  }

  function handleBarPointerMove(e: React.PointerEvent<HTMLDivElement>, id: string) {
    if (!sfxDrag || sfxDrag.id !== id) return;
    const deltaS = pxToS(e.clientX - sfxDrag.startClientX, laneContentRef);
    const effectiveDur = sfxDrag.startEndS - sfxDrag.startAtS;

    if (sfxDrag.handle === "body" || !sfxDrag.durationS) {
      const maxAt = Math.max(0, totalDurationS - effectiveDur);
      const newAtS = clampSeconds(sfxDrag.startAtS + deltaS, maxAt);
      setSfxDrag((d) => d ? { ...d, previewAtS: newAtS, previewEndS: newAtS + effectiveDur } : null);
    } else if (sfxDrag.handle === "right") {
      const newEndS = clampSeconds(sfxDrag.startEndS + deltaS, totalDurationS);
      const minEnd = sfxDrag.startAtS + MIN_TRIM_S;
      setSfxDrag((d) => d ? { ...d, previewEndS: Math.max(minEnd, newEndS) } : null);
    } else {
      const newAtS = clampSeconds(sfxDrag.startAtS + deltaS, sfxDrag.startEndS - MIN_TRIM_S);
      setSfxDrag((d) => d ? { ...d, previewAtS: Math.max(0, newAtS), previewEndS: sfxDrag.startEndS } : null);
    }
  }

  function handleBarPointerUp(e: React.PointerEvent<HTMLDivElement>, id: string) {
    if (!sfxDrag || sfxDrag.id !== id) return;
    e.currentTarget.releasePointerCapture(e.pointerId);

    const deltaS = pxToS(e.clientX - sfxDrag.startClientX, laneContentRef);
    const effectiveDur = sfxDrag.startEndS - sfxDrag.startAtS;
    const p = placements.find((x) => x.id === id);
    if (!p) { setSfxDrag(null); return; }

    if (sfxDrag.handle === "body" || !sfxDrag.durationS) {
      const maxAt = Math.max(0, totalDurationS - effectiveDur);
      const newAtS = clampSeconds(sfxDrag.startAtS + deltaS, maxAt);
      if (Math.abs(newAtS - p.at_s) > 0.01) dispatch({ type: "MOVE", id, atS: newAtS });
    } else if (sfxDrag.handle === "right") {
      const newEndS = clampSeconds(sfxDrag.startEndS + deltaS, totalDurationS);
      const minEnd = sfxDrag.startAtS + MIN_TRIM_S;
      const clampedEndS = Math.max(minEnd, newEndS);
      const newTrimEndS = sfxDrag.startTrimEndS + (clampedEndS - sfxDrag.startEndS);
      dispatch({
        type: "TRIM", id,
        trimStartS: p.trim_start_s ?? null,
        trimEndS: Math.min(sfxDrag.durationS, Math.max(sfxDrag.startTrimStartS + MIN_TRIM_S, newTrimEndS)),
      });
    } else {
      const newAtS = Math.max(0, clampSeconds(sfxDrag.startAtS + deltaS, sfxDrag.startEndS - MIN_TRIM_S));
      const newTrimStartS = Math.max(0, sfxDrag.startTrimStartS + deltaS);
      const maxStart = sfxDrag.startTrimEndS - MIN_TRIM_S;
      if (Math.abs(newAtS - p.at_s) > 0.01) dispatch({ type: "MOVE", id, atS: newAtS });
      if (Math.abs(newTrimStartS - sfxDrag.startTrimStartS) > 0.01) {
        dispatch({
          type: "TRIM", id,
          trimStartS: Math.min(maxStart, newTrimStartS),
          trimEndS: p.trim_end_s ?? null,
        });
      }
    }
    setSfxDrag(null);
  }

  // ── Overlay drag ─────────────────────────────────────────────────────────────

  const [overlayDrag, setOverlayDrag] = useState<OverlayDragState | null>(null);
  const overlayLaneRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!overlayDrag) return;
    const MIN_DUR = 0.1;

    function onMove(e: MouseEvent) {
      if (!overlayDrag) return;
      const dx = e.clientX - overlayDrag.startX;
      const ds = overlayDrag.containerWidth > 0
        ? (dx / overlayDrag.containerWidth) * overlayDrag.scaleDuration
        : 0;
      const clipDur = overlayDrag.clipDurationS;
      let patch: Partial<MediaOverlay> = {};

      switch (overlayDrag.handle) {
        case "move": {
          const dur = overlayDrag.origEnd - overlayDrag.origStart;
          const ns = Math.max(0, Math.min(totalDurationS - dur, overlayDrag.origStart + ds));
          patch = {
            start_s: Math.round(ns * 10) / 10,
            end_s: Math.round((ns + dur) * 10) / 10,
          };
          break;
        }
        case "left": {
          const minStart = Math.max(0, clipDur != null ? overlayDrag.origEnd - overlayDrag.origTrimEnd : 0);
          const ns = Math.max(minStart, Math.min(overlayDrag.origEnd - MIN_DUR, overlayDrag.origStart + ds));
          if (clipDur != null) {
            const newTrimStart = Math.max(0, overlayDrag.origTrimEnd - (overlayDrag.origEnd - ns));
            patch = { start_s: Math.round(ns * 10) / 10, clip_trim_start_s: Math.round(newTrimStart * 10) / 10 };
          } else {
            patch = { start_s: Math.round(ns * 10) / 10 };
          }
          break;
        }
        case "right": {
          const maxEnd = clipDur != null
            ? Math.min(totalDurationS, overlayDrag.origStart + (clipDur - overlayDrag.origTrimStart))
            : totalDurationS;
          const ne = Math.min(maxEnd, Math.max(overlayDrag.origStart + MIN_DUR, overlayDrag.origEnd + ds));
          if (clipDur != null) {
            const newTrimEnd = Math.min(clipDur, overlayDrag.origTrimStart + (ne - overlayDrag.origStart));
            patch = { end_s: Math.round(ne * 10) / 10, clip_trim_end_s: Math.round(newTrimEnd * 10) / 10 };
          } else {
            patch = { end_s: Math.round(ne * 10) / 10 };
          }
          break;
        }
        case "trim-left": {
          const ns = Math.max(0, Math.min(overlayDrag.origTrimEnd - MIN_DUR, overlayDrag.origTrimStart + ds));
          const newDur = overlayDrag.origTrimEnd - ns;
          const newEnd = Math.min(totalDurationS, overlayDrag.origStart + newDur);
          const actualDur = newEnd - overlayDrag.origStart;
          const actualTrimStart = Math.max(0, overlayDrag.origTrimEnd - actualDur);
          patch = { clip_trim_start_s: Math.round(actualTrimStart * 10) / 10, end_s: Math.round(newEnd * 10) / 10 };
          break;
        }
        case "trim-right": {
          const ne = Math.min(
            overlayDrag.scaleDuration,
            Math.max(overlayDrag.origTrimStart + MIN_DUR, overlayDrag.origTrimEnd + ds),
          );
          const newDur = ne - overlayDrag.origTrimStart;
          const newEnd = Math.min(totalDurationS, overlayDrag.origStart + newDur);
          const actualDur = newEnd - overlayDrag.origStart;
          const actualTrimEnd = overlayDrag.origTrimStart + actualDur;
          patch = { clip_trim_end_s: Math.round(actualTrimEnd * 10) / 10, end_s: Math.round(newEnd * 10) / 10 };
          break;
        }
      }
      onUpdateCard(overlayDrag.cardId, patch);
    }

    function onUp() { setOverlayDrag(null); }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [overlayDrag, onUpdateCard, totalDurationS]);

  function startOverlayDrag(
    e: React.MouseEvent,
    cardId: string,
    handle: OverlayDragState["handle"],
    card: MediaOverlay,
    containerEl: HTMLElement | null,
  ) {
    e.preventDefault();
    e.stopPropagation();
    const rect = (containerEl ?? overlayLaneRef.current)?.getBoundingClientRect();
    const isTrim = handle === "trim-left" || handle === "trim-right";
    const clipDur = card.kind === "video" ? (card.clip_duration_s ?? null) : null;
    setOverlayDrag({
      cardId,
      handle,
      startX: e.clientX,
      origStart: card.start_s,
      origEnd: card.end_s,
      origTrimStart: card.clip_trim_start_s ?? 0,
      origTrimEnd: card.clip_trim_end_s ?? (clipDur ?? card.end_s - card.start_s),
      containerWidth: rect?.width ?? 0,
      scaleDuration: isTrim ? (clipDur ?? 10) : totalDurationS,
      clipDurationS: clipDur,
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

  // ── Glossary + SFX upload ─────────────────────────────────────────────────────

  const [selectedGlossaryId, setSelectedGlossaryId] = useState("");
  const sfxFileInputRef = useRef<HTMLInputElement | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);

  function addFromGlossary() {
    if (!selectedGlossaryId) return;
    const effect = sfxGlossaryEffects.find((e) => e.id === selectedGlossaryId);
    if (!effect) return;
    dispatch({
      type: "ADD",
      placement: {
        id: crypto.randomUUID(),
        sound_effect_id: effect.id,
        src_gcs_path: "",
        at_s: Math.min(Math.max(0, currentTimeS), Math.max(0, totalDurationS - 0.1)),
        gain: 1.0,
        label: effect.name,
        duration_s: effect.duration_s ?? null,
      },
    });
    setSelectedGlossaryId("");
  }

  async function handleSfxFileSelect(files: FileList | File[]) {
    const valid = Array.from(files).filter(
      (f) => ALLOWED_AUDIO_MIME_TYPES.includes(f.type) && f.size <= MAX_SFX_FILE_BYTES,
    );
    if (valid.length === 0) {
      setUploadError("No valid audio files (mp3/wav/aac/ogg, max 20 MB).");
      return;
    }
    setUploadError(null);
    await onSfxUploadRequest(
      valid.map((f) => ({ file: f, filename: f.name, content_type: f.type || "audio/mpeg", file_size_bytes: f.size })),
    );
  }

  // ── Per-placement edit state ──────────────────────────────────────────────────

  const [openPlacementId, setOpenPlacementId] = useState<string | null>(null);
  const [openCardId, setOpenCardId] = useState<string | null>(null);
  const sfxDisabled = sfxRendering || sfxUploading;

  // ── Ruler ─────────────────────────────────────────────────────────────────────

  const tickInterval = tickIntervalFor(totalDurationS);
  const ticks =
    totalDurationS > 0
      ? Array.from(
          { length: Math.floor(totalDurationS / tickInterval) + 1 },
          (_, i) => i * tickInterval,
        )
      : [0];

  // ── SFX bar geometry ──────────────────────────────────────────────────────────

  function sfxBarGeometry(p: SoundEffectPlacement): { leftPct: number; widthPct: number } {
    const isInDrag = sfxDrag?.id === p.id;
    const atS = isInDrag ? sfxDrag.previewAtS : p.at_s;
    let endS: number;
    if (isInDrag && p.duration_s) {
      endS = sfxDrag.previewEndS;
    } else if (p.duration_s) {
      const trimEndS = p.trim_end_s ?? p.duration_s;
      const trimStartS = p.trim_start_s ?? 0;
      endS = p.at_s + (trimEndS - trimStartS);
    } else {
      endS = atS + Math.max(0.5, totalDurationS * 0.02);
    }
    return computeBarPosition(atS, endS, totalDurationS);
  }

  // ── Render ────────────────────────────────────────────────────────────────────

  return (
    <div className="select-none overflow-x-auto" data-testid="unified-timeline">
      {/* ── Ruler ── */}
      <div className="flex h-5" style={{ minWidth: "100%" }}>
        <div className="flex-shrink-0 w-14" />
        <div className="relative flex-1 bg-zinc-900/40 border-b border-zinc-800/60">
          {totalDurationS > 0 &&
            ticks.map((t) => {
              const pct = (t / totalDurationS) * 100;
              return (
                <div
                  key={t}
                  className="absolute top-0 h-full pointer-events-none"
                  style={{ left: `${pct}%` }}
                >
                  <div className="w-px h-2 bg-zinc-700" />
                  <span className="absolute left-0.5 top-2 text-[8px] leading-none text-zinc-500 whitespace-nowrap">
                    {formatTimecode(t)}
                  </span>
                </div>
              );
            })}
        </div>
      </div>

      {/* ── Clips lane (inline editing) ── */}
      <div>
        <div
          role="button"
          tabIndex={0}
          className="flex h-10 group cursor-pointer"
          onClick={toggleClipsOpen}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleClipsOpen(); } }}
        >
          <div className="flex-shrink-0 w-14 flex items-center justify-end pr-2">
            <span className="text-[9px] font-semibold text-zinc-400 uppercase tracking-wider truncate">Clips</span>
          </div>
          <div className="relative flex-1 bg-zinc-800/15 border-y border-zinc-700/30 overflow-hidden group-hover:bg-zinc-800/30 transition-colors">
            <Playhead currentTimeS={currentTimeS} totalDurationS={totalDurationS} />
            <button
              type="button"
              className="absolute inset-1 rounded flex items-center px-2 border border-sky-600/40 bg-sky-700/30 hover:bg-sky-700/50 text-sky-300/80 transition-colors"
              onClick={(e) => { e.stopPropagation(); toggleClipsOpen(); }}
            >
              <span className="text-[10px] truncate pointer-events-none">
                {clipsOpen ? "Clips ▲" : "Edit clips ▼"}
              </span>
            </button>
          </div>
        </div>
        {clipsOpen && clipsPanel && (
          <div className="pl-14 pr-2 pb-3 pt-2 border-b border-zinc-700/30">
            {clipsPanel}
          </div>
        )}
      </div>

      {/* ── Text lane (inline editing) ── */}
      {hasText && (
        <div>
          <div
            role="button"
            tabIndex={0}
            className="flex h-10 group cursor-pointer"
            onClick={toggleTextOpen}
            onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleTextOpen(); } }}
          >
            <div className="flex-shrink-0 w-14 flex items-center justify-end pr-2">
              <span className="text-[9px] font-semibold text-zinc-400 uppercase tracking-wider truncate">Text</span>
            </div>
            <div className="relative flex-1 bg-zinc-800/15 border-y border-zinc-700/30 overflow-hidden group-hover:bg-zinc-800/30 transition-colors">
              <Playhead currentTimeS={currentTimeS} totalDurationS={totalDurationS} />
              <button
                type="button"
                className="absolute inset-1 rounded flex items-center px-2 border border-amber-600/40 bg-amber-700/30 hover:bg-amber-700/50 text-amber-300/80 transition-colors"
                onClick={(e) => { e.stopPropagation(); toggleTextOpen(); }}
              >
                <span className="text-[10px] truncate pointer-events-none">
                  {textOpen ? "Text ▲" : "Edit text ▼"}
                </span>
              </button>
            </div>
          </div>
          {textOpen && textPanel && (
            <div className="pl-14 pr-2 pb-3 pt-2 border-b border-zinc-700/30">
              {textPanel}
            </div>
          )}
        </div>
      )}

      {/* ── Overlays lane (interactive) ── */}
      {(overlayCards.length > 0 || overlaysEnabled) && (
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
              className="relative flex-1 bg-zinc-800/15 border-y border-zinc-700/30 h-full"
            >
              <Playhead currentTimeS={currentTimeS} totalDurationS={totalDurationS} />
            </div>
          </div>

          {/* Per-card timing tracks */}
          {overlayCards.length > 0 && (
            <div className="ml-14 flex flex-col gap-1 py-1">
              {overlayCards.map((card, i) => {
                const color = TRACK_COLORS[i % TRACK_COLORS.length];
                const lPct = totalDurationS > 0 ? (card.start_s / totalDurationS) * 100 : 0;
                const wPct = totalDurationS > 0
                  ? Math.max(((card.end_s - card.start_s) / totalDurationS) * 100, 1)
                  : 1;
                const isDragging = overlayDrag?.cardId === card.id && !overlayDrag.handle.startsWith("trim");
                const isOpen = openCardId === card.id;

                return (
                  <div key={card.id}>
                    {/* Timing bar */}
                    <div className="relative h-6">
                      <div className="absolute inset-0 rounded bg-white/5" />
                      <div
                        className={`absolute top-0 h-full rounded flex items-center overflow-hidden transition-opacity ${
                          isDragging ? "opacity-100" : "opacity-70 hover:opacity-90"
                        }`}
                        style={{ left: `${lPct}%`, width: `${wPct}%`, backgroundColor: color, cursor: "grab" }}
                        onMouseDown={(e) => startOverlayDrag(e, card.id, "move", card, overlayLaneRef.current)}
                      >
                        <div
                          className="absolute left-0 top-0 h-full w-2.5 flex items-center justify-center hover:bg-black/30 z-10"
                          style={{ cursor: "ew-resize" }}
                          onMouseDown={(e) => {
                            e.stopPropagation();
                            startOverlayDrag(e, card.id, "left", card, overlayLaneRef.current);
                          }}
                        >
                          <div className="w-px h-3 bg-white/70 rounded-full" />
                        </div>
                        <span
                          className="text-[10px] text-white font-medium px-3 truncate"
                          onMouseDown={(e) => e.stopPropagation()}
                          onClick={(e) => { e.stopPropagation(); setOpenCardId(isOpen ? null : card.id); }}
                        >
                          {card.kind === "video" ? "▶" : "⊞"} {card.id.slice(0, 6)}
                        </span>
                        <div
                          className="absolute right-0 top-0 h-full w-2.5 flex items-center justify-center hover:bg-black/30 z-10"
                          style={{ cursor: "ew-resize" }}
                          onMouseDown={(e) => {
                            e.stopPropagation();
                            startOverlayDrag(e, card.id, "right", card, overlayLaneRef.current);
                          }}
                        >
                          <div className="w-px h-3 bg-white/70 rounded-full" />
                        </div>
                      </div>
                    </div>

                    {/* Per-card popover */}
                    {isOpen && (
                      <div className="bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 mt-1 space-y-2">
                        <div className="flex items-center justify-between">
                          <span className="text-xs text-white/40 font-mono">
                            {card.kind === "video" ? "video" : "image"} · {card.id.slice(0, 8)}
                          </span>
                          <div className="flex gap-1">
                            <span className="text-xs text-zinc-500 tabular-nums">
                              {(card.start_s ?? 0).toFixed(1)}s – {(card.end_s ?? 0).toFixed(1)}s
                            </span>
                            <button
                              type="button"
                              onClick={() => { onRemoveCard(card.id); setOpenCardId(null); }}
                              className="ml-2 text-white/30 hover:text-red-400 text-xs px-1"
                              aria-label="Remove card"
                            >
                              ✕
                            </button>
                          </div>
                        </div>
                        <div className="flex gap-1">
                          {POSITION_PRESETS.map((p) => (
                            <button
                              key={p.value}
                              type="button"
                              onClick={() => onUpdateCard(card.id, { position: p.value })}
                              className={`flex-1 text-xs rounded py-1 transition-colors ${
                                card.position === p.value
                                  ? "bg-lime-400 text-black font-semibold"
                                  : "bg-white/10 text-white/60 hover:bg-white/20"
                              }`}
                            >
                              {p.label}
                            </button>
                          ))}
                        </div>
                        <div className="flex items-center gap-2">
                          <span className="text-xs text-white/40 w-10">Scale</span>
                          <input
                            type="range"
                            min={Math.round(MIN_SCALE * 100)}
                            max={Math.round(MAX_SCALE * 100)}
                            value={Math.round((card.scale ?? 0.35) * 100)}
                            onChange={(e) => onUpdateCard(card.id, { scale: Number(e.target.value) / 100 })}
                            className="flex-1 accent-lime-400"
                          />
                          <span className="text-xs text-white/60 w-10 text-right">
                            {Math.round((card.scale ?? 0.35) * 100)}%
                          </span>
                        </div>
                      </div>
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
                        onTrimLeftDown={(e) => startOverlayDrag(e, card.id, "trim-left", card, null)}
                        onTrimRightDown={(e) => startOverlayDrag(e, card.id, "trim-right", card, null)}
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
                {overlayUploading ? "Uploading…" : "Drop image/video overlay or click to browse"}
              </div>
              {overlayCards.length > 0 && (
                <button
                  type="button"
                  onClick={onClearOverlays}
                  className="mt-1 text-[10px] text-white/30 hover:text-white/60 transition-colors"
                >
                  Clear all overlays
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {/* ── SFX lane (interactive) ── */}
      <div className="flex h-11 mb-1">
        <div className="flex-shrink-0 w-14 flex items-center justify-end pr-2">
          <span className="text-[9px] font-semibold text-zinc-400 uppercase tracking-wider">
            SFX
          </span>
        </div>
        <div
          ref={laneContentRef}
          className="relative flex-1 bg-zinc-800/25 border-y border-zinc-700/40 overflow-hidden"
          data-testid="sfx-lane"
        >
          <Playhead currentTimeS={currentTimeS} totalDurationS={totalDurationS} />

          {placements.length === 0 && !sfxRendering && (
            <p className="absolute inset-0 flex items-center justify-center text-[10px] text-zinc-600 pointer-events-none">
              Add a sound effect below
            </p>
          )}

          {placements.map((p) => {
            const { leftPct, widthPct } = sfxBarGeometry(p);
            const isBeingDragged = sfxDrag?.id === p.id;
            const isOpen = openPlacementId === p.id;

            return (
              <div
                key={p.id}
                data-placement-id={p.id}
                role="button"
                aria-label={`Sound effect ${p.label ?? ""} at ${(p.at_s ?? 0).toFixed(1)}s`}
                aria-pressed={isOpen}
                className={[
                  "absolute inset-y-1 rounded select-none border flex items-center overflow-hidden",
                  "transition-opacity",
                  isBeingDragged ? "opacity-60 z-10 shadow-lg" : "opacity-100",
                  sfxDisabled ? "opacity-40 cursor-not-allowed" : "cursor-grab active:cursor-grabbing",
                  "bg-lime-700/40 border-lime-500/50 hover:bg-lime-700/60",
                ].join(" ")}
                style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                onPointerDown={(e) => handleBarPointerDown(e, p)}
                onPointerMove={(e) => handleBarPointerMove(e, p.id)}
                onPointerUp={(e) => handleBarPointerUp(e, p.id)}
                onClick={(e) => {
                  if (sfxDrag !== null) return;
                  e.stopPropagation();
                  setOpenPlacementId(isOpen ? null : p.id);
                }}
              >
                {p.duration_s && (
                  <>
                    <div className="absolute left-0 top-0 bottom-0 w-2.5 cursor-col-resize z-10" />
                    <div className="absolute right-0 top-0 bottom-0 w-2.5 cursor-col-resize z-10" />
                  </>
                )}
                <span className="px-1.5 text-[9px] text-lime-100 truncate pointer-events-none leading-none">
                  {p.label ?? formatTimecode(p.at_s)}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {/* ── Per-SFX edit rows ── */}
      {placements.map((p) =>
        openPlacementId === p.id ? (
          <div
            key={`edit-${p.id}`}
            className="ml-14 mr-0 mb-2 bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 space-y-2"
          >
            <div className="flex items-center gap-2 flex-wrap">
              <input
                value={p.label ?? ""}
                onChange={(e) => dispatch({ type: "SET_LABEL", id: p.id, label: e.target.value })}
                disabled={sfxDisabled}
                placeholder="Label (optional)"
                className="flex-1 min-w-0 bg-zinc-700 border border-zinc-600 rounded px-2 py-1 text-xs text-white placeholder-zinc-500 focus:outline-none focus:border-lime-500"
              />
              <span className="text-xs text-zinc-500 shrink-0 tabular-nums">
                @{(p.at_s ?? 0).toFixed(1)}s
              </span>
              <button
                type="button"
                onClick={() => { dispatch({ type: "REMOVE", id: p.id }); setOpenPlacementId(null); }}
                disabled={sfxDisabled}
                className="shrink-0 text-xs text-zinc-500 hover:text-red-400 disabled:opacity-40"
              >
                Remove
              </button>
            </div>
            <label className="flex items-center gap-2 text-xs text-zinc-400">
              <span className="w-5 shrink-0">Vol</span>
              <input
                type="range"
                min={0}
                max={2}
                step={0.05}
                value={p.gain ?? 1}
                onChange={(e) => dispatch({ type: "SET_GAIN", id: p.id, gain: parseFloat(e.target.value) })}
                disabled={sfxDisabled}
                className="flex-1 accent-lime-500 disabled:opacity-50"
              />
              <span className="w-10 text-right tabular-nums shrink-0">{(p.gain ?? 1).toFixed(2)}×</span>
            </label>
          </div>
        ) : null,
      )}

      {/* ── Add SFX controls ── */}
      <div className="pl-14 pr-0 pt-2 space-y-2">
        <div className="flex gap-2">
          <select
            value={selectedGlossaryId}
            onChange={(e) => setSelectedGlossaryId(e.target.value)}
            disabled={sfxDisabled || sfxGlossaryLoading}
            className="flex-1 bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-xs text-white disabled:opacity-50"
          >
            <option value="">
              {sfxGlossaryLoading ? "Loading effects…" : "Pick a sound effect (placed at playhead)…"}
            </option>
            {sfxGlossaryEffects.map((e) => (
              <option key={e.id} value={e.id}>
                {e.name}
                {e.duration_s != null ? ` · ${e.duration_s.toFixed(1)}s` : ""}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={addFromGlossary}
            disabled={sfxDisabled || !selectedGlossaryId}
            className="shrink-0 px-3 py-1 bg-lime-800/60 hover:bg-lime-700 text-lime-100 text-xs rounded disabled:opacity-40 transition-colors"
          >
            + Add
          </button>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => sfxFileInputRef.current?.click()}
            disabled={sfxDisabled}
            className="px-3 py-1 bg-zinc-700 hover:bg-zinc-600 text-zinc-300 text-xs rounded disabled:opacity-40 transition-colors"
          >
            Upload audio…
          </button>
          <input
            ref={sfxFileInputRef}
            type="file"
            accept={ALLOWED_AUDIO_MIME_TYPES.join(",")}
            multiple
            className="hidden"
            onChange={(e) => { if (e.target.files) handleSfxFileSelect(e.target.files); e.target.value = ""; }}
          />
          <div className="flex-1" />
          <button
            type="button"
            onClick={() => dispatch({ type: "UNDO" })}
            disabled={!canUndo || sfxDisabled}
            title="Undo"
            className="px-2 py-1 text-sm text-zinc-400 hover:text-white disabled:opacity-25 transition-colors"
          >
            ↩
          </button>
          <button
            type="button"
            onClick={() => dispatch({ type: "REDO" })}
            disabled={!canRedo || sfxDisabled}
            title="Redo"
            className="px-2 py-1 text-sm text-zinc-400 hover:text-white disabled:opacity-25 transition-colors"
          >
            ↪
          </button>
        </div>

        {uploadError && <p className="text-xs text-red-400">{uploadError}</p>}
      </div>

      <p className="pl-14 pt-1.5 text-[9px] text-zinc-600">
        Clips lane — click to expand inline · Text lane — click to expand inline
      </p>
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

interface ReadOnlyLaneProps {
  label: string;
  totalDurationS: number;
  currentTimeS: number;
  onClick: () => void;
  children: React.ReactNode;
}

function ReadOnlyLane({ label, totalDurationS, currentTimeS, onClick, children }: ReadOnlyLaneProps) {
  return (
    <div
      role="button"
      tabIndex={0}
      className="flex h-10 group cursor-pointer"
      onClick={onClick}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(); } }}
    >
      <div className="flex-shrink-0 w-14 flex items-center justify-end pr-2">
        <span className="text-[9px] font-semibold text-zinc-400 uppercase tracking-wider truncate">
          {label}
        </span>
      </div>
      <div className="relative flex-1 bg-zinc-800/15 border-y border-zinc-700/30 overflow-hidden group-hover:bg-zinc-800/30 transition-colors">
        <Playhead currentTimeS={currentTimeS} totalDurationS={totalDurationS} />
        {children}
      </div>
    </div>
  );
}

interface FullWidthBarProps {
  label: string;
  colorClass: string;
  onClick: () => void;
}

function FullWidthBar({ label, colorClass, onClick }: FullWidthBarProps) {
  return (
    <button
      type="button"
      className={["absolute inset-1 rounded flex items-center px-2 border transition-colors", colorClass].join(" ")}
      onClick={(e) => { e.stopPropagation(); onClick(); }}
    >
      <span className="text-[10px] truncate pointer-events-none">{label}</span>
    </button>
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
  onTrimLeftDown: (e: React.MouseEvent) => void;
  onTrimRightDown: (e: React.MouseEvent) => void;
}

function TrimLane({
  card,
  videoSrc,
  clipDur,
  trimStart,
  trimEnd,
  isTrimDragging,
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
          className={`absolute top-0 h-full border-2 rounded transition-colors ${isTrimDragging ? "border-white" : "border-white/60"}`}
          style={{ left: `${lPct}%`, width: `${wPct}%` }}
        >
          <div
            className="absolute left-0 top-0 h-full w-3 bg-white/20 flex items-center justify-center"
            style={{ cursor: "ew-resize" }}
            onMouseDown={onTrimLeftDown}
          >
            <div className="w-0.5 h-5 bg-white rounded-full" />
          </div>
          <div
            className="absolute right-0 top-0 h-full w-3 bg-white/20 flex items-center justify-center"
            style={{ cursor: "ew-resize" }}
            onMouseDown={onTrimRightDown}
          >
            <div className="w-0.5 h-5 bg-white rounded-full" />
          </div>
        </div>
      </div>
    </div>
  );
}
