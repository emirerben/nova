"use client";

import { useEffect, useRef, useState } from "react";
import type { MediaOverlay } from "@/lib/plan-api";

const MIN_SCALE = 0.05;
const MAX_SCALE = 1.0;

const POSITION_PRESETS = [
  { label: "Top", value: "top" as const },
  { label: "Center", value: "center" as const },
  { label: "Bottom", value: "bottom" as const },
];

const ALLOWED_MIME_TYPES = [
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/heic",
  "video/mp4",
  "video/quicktime",
];

const TRACK_COLORS = ["#8B5CF6", "#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#EC4899"];

function fmtTime(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

// ── Video thumbnail extractor ──────────────────────────────────────────────────

function useVideoThumbs(
  src: string | null | undefined,
  duration: number,
  count: number,
): (string | null)[] {
  const [thumbs, setThumbs] = useState<(string | null)[]>(() => Array(count).fill(null));
  const prevSrcRef = useRef<string | null>(null);

  useEffect(() => {
    // Only works reliably on blob: URLs (same-origin, no CORS).
    if (!src || !src.startsWith("blob:") || duration <= 0 || count <= 0) {
      setThumbs(Array(count).fill(null));
      return;
    }
    if (prevSrcRef.current === src) return;
    prevSrcRef.current = src;
    setThumbs(Array(count).fill(null));

    let cancelled = false;
    const v = document.createElement("video");
    v.muted = true;
    v.preload = "auto";
    const canvas = document.createElement("canvas");
    // 9:16 thumbnail cells
    canvas.width = 40;
    canvas.height = 71;
    const ctx = canvas.getContext("2d");
    if (!ctx) { v.src = ""; return; }

    const results: (string | null)[] = Array(count).fill(null);

    async function extract() {
      for (let i = 0; i < count; i++) {
        if (cancelled) break;
        const t = count <= 1 ? 0 : (duration * i) / (count - 1);
        v.currentTime = t;
        await new Promise<void>((res) => {
          const onSeeked = () => {
            v.removeEventListener("seeked", onSeeked);
            res();
          };
          v.addEventListener("seeked", onSeeked);
          setTimeout(res, 400);
        });
        if (cancelled) break;
        try {
          ctx!.drawImage(v, 0, 0, canvas.width, canvas.height);
          results[i] = canvas.toDataURL("image/jpeg", 0.5);
          setThumbs([...results]);
        } catch {
          // SecurityError or draw failure — leave null (gray cell shown instead)
        }
      }
      v.src = "";
    }

    v.addEventListener("loadeddata", extract, { once: true });
    v.onerror = () => { v.src = ""; };
    v.src = src;

    return () => {
      cancelled = true;
      v.src = "";
    };
  }, [src, duration, count]);

  return thumbs;
}

// ── Drag state ─────────────────────────────────────────────────────────────────

interface DragState {
  cardId: string;
  handle: "move" | "left" | "right" | "trim-left" | "trim-right";
  startX: number;
  origStart: number;
  origEnd: number;
  origTrimStart: number;
  origTrimEnd: number;
  containerWidth: number;
  scaleDuration: number;
  /** Source clip total duration — null for image cards (no sync needed). */
  clipDurationS: number | null;
}

// ── Visual timeline ────────────────────────────────────────────────────────────

interface TimelineProps {
  overlays: MediaOverlay[];
  totalDurationS: number;
  disabled: boolean;
  localPreviewUrls: Record<string, string>;
  onUpdateCard: (id: string, patch: Partial<MediaOverlay>) => void;
}

function OverlayCardTimeline({
  overlays,
  totalDurationS,
  disabled,
  localPreviewUrls,
  onUpdateCard,
}: TimelineProps) {
  const rulerRef = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<DragState | null>(null);

  const step = totalDurationS <= 10 ? 2 : totalDurationS <= 30 ? 5 : 10;
  const markers: number[] = [];
  for (let t = 0; t <= totalDurationS; t += step) markers.push(t);

  function startDrag(
    e: React.MouseEvent,
    cardId: string,
    handle: DragState["handle"],
    card: MediaOverlay,
    containerEl: HTMLElement,
  ) {
    if (disabled) return;
    e.preventDefault();
    e.stopPropagation();
    const rect = containerEl.getBoundingClientRect();
    const isTrim = handle === "trim-left" || handle === "trim-right";
    const clipDur = card.kind === "video" ? (card.clip_duration_s ?? null) : null;
    setDrag({
      cardId,
      handle,
      startX: e.clientX,
      origStart: card.start_s,
      origEnd: card.end_s,
      origTrimStart: card.clip_trim_start_s ?? 0,
      origTrimEnd: card.clip_trim_end_s ?? (clipDur ?? card.end_s - card.start_s),
      containerWidth: rect.width,
      scaleDuration: isTrim ? (clipDur ?? 10) : totalDurationS,
      clipDurationS: clipDur,
    });
  }

  useEffect(() => {
    if (!drag) return;
    const MIN_DUR = 0.1;

    function onMove(e: MouseEvent) {
      if (!drag) return;
      const dx = e.clientX - drag.startX;
      const ds = drag.containerWidth > 0 ? (dx / drag.containerWidth) * drag.scaleDuration : 0;
      let patch: Partial<MediaOverlay> = {};

      // Invariant for video cards: end_s - start_s === clip_trim_end_s - clip_trim_start_s
      // Every handle that changes duration on one side syncs the other side.
      const clipDur = drag.clipDurationS;

      switch (drag.handle) {
        case "move": {
          // Duration unchanged — no trim sync needed.
          const dur = drag.origEnd - drag.origStart;
          const ns = Math.max(0, Math.min(totalDurationS - dur, drag.origStart + ds));
          patch = {
            start_s: Math.round(ns * 10) / 10,
            end_s: Math.round((ns + dur) * 10) / 10,
          };
          break;
        }
        case "left": {
          // Timing start moves → duration changes → sync clip_trim_start_s (keep trim end).
          // Limit: can't play more clip than origTrimEnd allows.
          const minStart = Math.max(0, clipDur != null ? drag.origEnd - drag.origTrimEnd : 0);
          const ns = Math.max(minStart, Math.min(drag.origEnd - MIN_DUR, drag.origStart + ds));
          if (clipDur != null) {
            const newTrimStart = Math.max(0, drag.origTrimEnd - (drag.origEnd - ns));
            patch = {
              start_s: Math.round(ns * 10) / 10,
              clip_trim_start_s: Math.round(newTrimStart * 10) / 10,
            };
          } else {
            patch = { start_s: Math.round(ns * 10) / 10 };
          }
          break;
        }
        case "right": {
          // Timing end moves → duration changes → sync clip_trim_end_s (keep trim start).
          // Limit: can't exceed remaining clip content from current trim start.
          const maxEnd = clipDur != null
            ? Math.min(totalDurationS, drag.origStart + (clipDur - drag.origTrimStart))
            : totalDurationS;
          const ne = Math.min(maxEnd, Math.max(drag.origStart + MIN_DUR, drag.origEnd + ds));
          if (clipDur != null) {
            const newTrimEnd = Math.min(clipDur, drag.origTrimStart + (ne - drag.origStart));
            patch = {
              end_s: Math.round(ne * 10) / 10,
              clip_trim_end_s: Math.round(newTrimEnd * 10) / 10,
            };
          } else {
            patch = { end_s: Math.round(ne * 10) / 10 };
          }
          break;
        }
        case "trim-left": {
          // Trim start moves → duration changes → sync end_s (keep start_s).
          const ns = Math.max(0, Math.min(drag.origTrimEnd - MIN_DUR, drag.origTrimStart + ds));
          const newDur = drag.origTrimEnd - ns;
          // Cap: end_s can't exceed variant duration.
          const newEnd = Math.min(totalDurationS, drag.origStart + newDur);
          const actualDur = newEnd - drag.origStart;
          // If end_s was capped, trim start must match to keep invariant.
          const actualTrimStart = Math.max(0, drag.origTrimEnd - actualDur);
          patch = {
            clip_trim_start_s: Math.round(actualTrimStart * 10) / 10,
            end_s: Math.round(newEnd * 10) / 10,
          };
          break;
        }
        case "trim-right": {
          // Trim end moves → duration changes → sync end_s (keep start_s).
          const ne = Math.min(
            drag.scaleDuration,
            Math.max(drag.origTrimStart + MIN_DUR, drag.origTrimEnd + ds),
          );
          const newDur = ne - drag.origTrimStart;
          // Cap: end_s can't exceed variant duration.
          const newEnd = Math.min(totalDurationS, drag.origStart + newDur);
          const actualDur = newEnd - drag.origStart;
          // If end_s was capped, trim end must match to keep invariant.
          const actualTrimEnd = drag.origTrimStart + actualDur;
          patch = {
            clip_trim_end_s: Math.round(actualTrimEnd * 10) / 10,
            end_s: Math.round(newEnd * 10) / 10,
          };
          break;
        }
      }
      onUpdateCard(drag.cardId, patch);
    }

    function onUp() {
      setDrag(null);
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [drag, onUpdateCard, totalDurationS]);

  return (
    <div className={`select-none ${disabled ? "opacity-40 pointer-events-none" : ""}`}>
      {/* ── Ruler ──────────────────────────────────────────────────── */}
      <div ref={rulerRef} className="relative h-5 border-b border-white/10 mb-2">
        {markers.map((t) => (
          <div
            key={t}
            className="absolute top-0 flex flex-col items-center"
            style={{ left: `${(t / totalDurationS) * 100}%`, transform: "translateX(-50%)" }}
          >
            <span className="text-[9px] text-white/40 leading-none">{fmtTime(t)}</span>
            <div className="w-px h-1.5 bg-white/20 mt-0.5" />
          </div>
        ))}
      </div>

      {/* ── Card timing tracks ─────────────────────────────────────── */}
      <div className="flex flex-col gap-1">
        {overlays.map((card, i) => {
          const color = TRACK_COLORS[i % TRACK_COLORS.length];
          const lPct = (card.start_s / totalDurationS) * 100;
          const wPct = Math.max(((card.end_s - card.start_s) / totalDurationS) * 100, 1);
          const isDragging = drag?.cardId === card.id && !drag.handle.startsWith("trim");

          return (
            <div key={card.id} className="relative h-6">
              <div className="absolute inset-0 rounded bg-white/5" />
              <div
                className={`absolute top-0 h-full rounded flex items-center overflow-hidden transition-opacity ${
                  isDragging ? "opacity-100" : "opacity-70 hover:opacity-90"
                }`}
                style={{
                  left: `${lPct}%`,
                  width: `${wPct}%`,
                  backgroundColor: color,
                  cursor: disabled ? "default" : "grab",
                }}
                onMouseDown={(e) => startDrag(e, card.id, "move", card, rulerRef.current!)}
              >
                <div
                  className="absolute left-0 top-0 h-full w-2.5 flex items-center justify-center hover:bg-black/30 z-10"
                  style={{ cursor: "ew-resize" }}
                  onMouseDown={(e) => startDrag(e, card.id, "left", card, rulerRef.current!)}
                >
                  <div className="w-px h-3 bg-white/70 rounded-full" />
                </div>
                <span className="text-[10px] text-white font-medium px-3 truncate pointer-events-none">
                  {card.kind === "video" ? "▶" : "⊞"} {card.id.slice(0, 6)}
                </span>
                <div
                  className="absolute right-0 top-0 h-full w-2.5 flex items-center justify-center hover:bg-black/30 z-10"
                  style={{ cursor: "ew-resize" }}
                  onMouseDown={(e) => startDrag(e, card.id, "right", card, rulerRef.current!)}
                >
                  <div className="w-px h-3 bg-white/70 rounded-full" />
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* ── Video trim lanes ───────────────────────────────────────── */}
      {overlays
        .filter((c) => c.kind === "video" && c.clip_duration_s && c.clip_duration_s > 0)
        .map((card) => {
          const clipDur = card.clip_duration_s!;
          const trimStart = card.clip_trim_start_s ?? 0;
          const trimEnd = card.clip_trim_end_s ?? clipDur;
          const lPct = (trimStart / clipDur) * 100;
          const wPct = Math.max(((trimEnd - trimStart) / clipDur) * 100, 1);
          const isTrimDragging =
            drag?.cardId === card.id &&
            (drag.handle === "trim-left" || drag.handle === "trim-right");
          const videoSrc = localPreviewUrls[card.id] ?? card.preview_url ?? null;

          return (
            <TrimLane
              key={`trim-${card.id}`}
              card={card}
              videoSrc={videoSrc}
              clipDur={clipDur}
              trimStart={trimStart}
              trimEnd={trimEnd}
              lPct={lPct}
              wPct={wPct}
              isTrimDragging={isTrimDragging}
              onTrimLeftDown={(e) => {
                const el = (e.currentTarget.closest("[data-trim-container]") as HTMLElement) ?? rulerRef.current!;
                startDrag(e, card.id, "trim-left", card, el);
              }}
              onTrimRightDown={(e) => {
                const el = (e.currentTarget.closest("[data-trim-container]") as HTMLElement) ?? rulerRef.current!;
                startDrag(e, card.id, "trim-right", card, el);
              }}
            />
          );
        })}
    </div>
  );
}

// ── Trim lane with thumbnails ──────────────────────────────────────────────────

interface TrimLaneProps {
  card: MediaOverlay;
  videoSrc: string | null;
  clipDur: number;
  trimStart: number;
  trimEnd: number;
  lPct: number;
  wPct: number;
  isTrimDragging: boolean;
  onTrimLeftDown: (e: React.MouseEvent) => void;
  onTrimRightDown: (e: React.MouseEvent) => void;
}

const THUMB_COUNT = 10;

function TrimLane({
  card,
  videoSrc,
  clipDur,
  trimStart,
  trimEnd,
  lPct,
  wPct,
  isTrimDragging,
  onTrimLeftDown,
  onTrimRightDown,
}: TrimLaneProps) {
  const thumbs = useVideoThumbs(videoSrc, clipDur, THUMB_COUNT);
  const hasAnyThumb = thumbs.some(Boolean);

  return (
    <div className="mt-2">
      <span className="text-[9px] text-white/40 mb-1 block">
        Clip trim — {card.id.slice(0, 6)} ({fmtTime(trimStart)}–{fmtTime(trimEnd)} of{" "}
        {fmtTime(clipDur)})
      </span>
      <div className="relative h-10 rounded overflow-hidden bg-zinc-800" data-trim-container={card.id}>
        {/* Filmstrip: real thumbnails when available, gray cells as fallback */}
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

        {/* Dimmed outside-trim zones */}
        <div
          className="absolute top-0 left-0 h-full bg-black/60 pointer-events-none"
          style={{ width: `${lPct}%` }}
        />
        <div
          className="absolute top-0 right-0 h-full bg-black/60 pointer-events-none"
          style={{ width: `${100 - lPct - wPct}%` }}
        />

        {/* Active trim window border */}
        <div
          className={`absolute top-0 h-full border-2 rounded transition-colors ${
            isTrimDragging ? "border-white" : "border-white/60"
          }`}
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

// ── Main editor ────────────────────────────────────────────────────────────────

interface Props {
  overlays: MediaOverlay[];
  variantDurationS: number;
  /** True while a GCS upload is in progress — disables the drop zone only. */
  uploading: boolean;
  localPreviewUrls: Record<string, string>;
  onUploadRequest: (
    files: { file: File; filename: string; content_type: string; file_size_bytes: number }[],
  ) => void;
  onUpdateCard: (id: string, patch: Partial<MediaOverlay>) => void;
  onRemoveCard: (id: string) => void;
  onClear: () => void;
}

export default function MediaOverlayEditor({
  overlays,
  variantDurationS,
  uploading,
  localPreviewUrls,
  onUploadRequest,
  onUpdateCard,
  onRemoveCard,
  onClear,
}: Props) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);

  function handleFiles(fileList: FileList | null) {
    if (!fileList || fileList.length === 0) return;
    const valid: { file: File; filename: string; content_type: string; file_size_bytes: number }[] = [];
    for (const file of Array.from(fileList)) {
      if (!ALLOWED_MIME_TYPES.includes(file.type)) continue;
      valid.push({
        file,
        filename: file.name,
        content_type: file.type,
        file_size_bytes: file.size,
      });
    }
    if (valid.length > 0) onUploadRequest(valid);
  }

  return (
    <div className="flex flex-col gap-4 p-4">
      {/* ── Upload zone ───────────────────────────────────────────── */}
      <div
        className={`rounded-xl border-2 border-dashed p-5 text-center transition-colors cursor-pointer ${
          dragOver ? "border-lime-400 bg-lime-400/10" : "border-white/20 hover:border-white/40"
        } ${uploading ? "opacity-40 pointer-events-none" : ""}`}
        onClick={() => fileInputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          handleFiles(e.dataTransfer.files);
        }}
      >
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept={ALLOWED_MIME_TYPES.join(",")}
          className="hidden"
          onChange={(e) => handleFiles(e.target.files)}
        />
        <p className="text-sm text-white/60">Drop images, stickers, or short clips here</p>
        <p className="text-xs text-white/40 mt-1">PNG · JPEG · WEBP · HEIC · MP4 · MOV</p>
      </div>

      {overlays.length === 0 ? (
        <p className="text-xs text-white/40 text-center py-2">No cards yet. Add one above.</p>
      ) : (
        <>
          {/* ── Visual timeline ─────────────────────────────────────── */}
          <div className="rounded-xl border border-white/10 bg-white/5 p-3">
            <OverlayCardTimeline
              overlays={overlays}
              totalDurationS={variantDurationS || 30}
              disabled={false}
              localPreviewUrls={localPreviewUrls}
              onUpdateCard={onUpdateCard}
            />
          </div>

          {/* ── Per-card rows (position + scale) ────────────────────── */}
          <div className="flex flex-col gap-3">
            {overlays.map((card, i) => (
              <CardRow
                key={card.id}
                card={card}
                color={TRACK_COLORS[i % TRACK_COLORS.length]}
                disabled={false}
                onUpdate={(patch) => onUpdateCard(card.id, patch)}
                onRemove={() => onRemoveCard(card.id)}
              />
            ))}
          </div>
        </>
      )}

      {/* ── Clear button (explicit destructive action only) ──────── */}
      {overlays.length > 0 && (
        <div className="flex pt-1">
          <button
            onClick={onClear}
            className="rounded-lg border border-white/20 text-white/60 text-sm py-2 px-4 hover:border-white/40"
          >
            Clear all
          </button>
        </div>
      )}
    </div>
  );
}

// ── Per-card row (position + scale — timing lives in the timeline) ─────────────

interface CardRowProps {
  card: MediaOverlay;
  color: string;
  disabled: boolean;
  onUpdate: (patch: Partial<MediaOverlay>) => void;
  onRemove: () => void;
}

function CardRow({ card, color, disabled, onUpdate, onRemove }: CardRowProps) {
  const scalePercent = Math.round(card.scale * 100);

  return (
    <div
      className={`rounded-xl border border-white/10 bg-white/5 p-3 flex flex-col gap-2 ${
        disabled ? "opacity-50 pointer-events-none" : ""
      }`}
    >
      <div className="flex items-center justify-between">
        <span className="text-xs font-mono text-white/40 truncate max-w-[180px] flex items-center gap-1.5">
          <span
            className="inline-block w-2 h-2 rounded-full flex-shrink-0"
            style={{ backgroundColor: color }}
          />
          <span
            className={`inline-block rounded px-1 py-0.5 text-[10px] font-semibold ${
              card.kind === "video" ? "bg-blue-500/20 text-blue-300" : "bg-white/10 text-white/60"
            }`}
          >
            {card.kind === "video" ? "video" : "image"}
          </span>
          {card.id.slice(0, 8)}
        </span>
        <button
          onClick={onRemove}
          className="text-white/30 hover:text-white/70 text-xs px-1"
          aria-label="Remove card"
        >
          ✕
        </button>
      </div>

      <div className="flex gap-1">
        {POSITION_PRESETS.map((p) => (
          <button
            key={p.value}
            onClick={() => onUpdate({ position: p.value })}
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
          value={scalePercent}
          onChange={(e) => onUpdate({ scale: Number(e.target.value) / 100 })}
          className="flex-1 accent-lime-400"
        />
        <span className="text-xs text-white/60 w-10 text-right">{scalePercent}%</span>
      </div>

      <div className="flex items-center gap-2">
        <span className="text-xs text-white/30 w-10">Time</span>
        <span className="text-xs text-white/50">
          {card.start_s.toFixed(1)}s – {card.end_s.toFixed(1)}s
        </span>
        <span className="text-xs text-white/30 ml-auto">drag timeline ↑</span>
      </div>
    </div>
  );
}
