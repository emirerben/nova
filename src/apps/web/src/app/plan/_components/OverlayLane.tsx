"use client";

/**
 * OverlayLane — interactive Overlays lane for UnifiedTimeline.
 *
 * Owns: overlay drag-move, edge-trim (both timeline position and clip trim),
 * per-card timing track, per-card popover (position/scale/remove),
 * video TrimLane with thumbnail strip, and the upload zone.
 *
 * Extracted from UnifiedTimeline.tsx (T0 refactor). No logic changed.
 */

import { useEffect, useRef, useState } from "react";
import type { MediaOverlay } from "@/lib/plan-api";
import { Playhead } from "@/lib/timeline/Playhead";
import type { UploadFile, OverlayDragState } from "./UnifiedTimelineTypes";

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

const POSITION_PRESETS = [
  { label: "Top", value: "top" as const },
  { label: "Center", value: "center" as const },
  { label: "Bottom", value: "bottom" as const },
];

const THUMB_COUNT = 10;
const MIN_SCALE = 0.05;
const MAX_SCALE = 1.0;

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtTime(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
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
  onUpdateCard: (id: string, patch: Partial<MediaOverlay>) => void;
  onRemoveCard: (id: string) => void;
  onClearOverlays: () => void;
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
}: OverlayLaneProps) {
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

  // ── Per-card open state ───────────────────────────────────────────────────────

  const [openCardId, setOpenCardId] = useState<string | null>(null);

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
