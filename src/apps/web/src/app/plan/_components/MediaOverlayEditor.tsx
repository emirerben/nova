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
}

// ── Visual timeline ────────────────────────────────────────────────────────────

interface TimelineProps {
  overlays: MediaOverlay[];
  totalDurationS: number;
  disabled: boolean;
  onUpdateCard: (id: string, patch: Partial<MediaOverlay>) => void;
}

function OverlayCardTimeline({ overlays, totalDurationS, disabled, onUpdateCard }: TimelineProps) {
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
    const clipDur = card.clip_duration_s ?? (card.end_s - card.start_s);
    setDrag({
      cardId,
      handle,
      startX: e.clientX,
      origStart: card.start_s,
      origEnd: card.end_s,
      origTrimStart: card.clip_trim_start_s ?? 0,
      origTrimEnd: card.clip_trim_end_s ?? clipDur,
      containerWidth: rect.width,
      scaleDuration: isTrim ? clipDur : totalDurationS,
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
      switch (drag.handle) {
        case "move": {
          const dur = drag.origEnd - drag.origStart;
          const ns = Math.max(0, Math.min(totalDurationS - dur, drag.origStart + ds));
          patch = {
            start_s: Math.round(ns * 10) / 10,
            end_s: Math.round((ns + dur) * 10) / 10,
          };
          break;
        }
        case "left": {
          const ns = Math.max(0, Math.min(drag.origEnd - MIN_DUR, drag.origStart + ds));
          patch = { start_s: Math.round(ns * 10) / 10 };
          break;
        }
        case "right": {
          const ne = Math.min(totalDurationS, Math.max(drag.origStart + MIN_DUR, drag.origEnd + ds));
          patch = { end_s: Math.round(ne * 10) / 10 };
          break;
        }
        case "trim-left": {
          const ns = Math.max(0, Math.min(drag.origTrimEnd - MIN_DUR, drag.origTrimStart + ds));
          patch = { clip_trim_start_s: Math.round(ns * 10) / 10 };
          break;
        }
        case "trim-right": {
          const ne = Math.min(
            drag.scaleDuration,
            Math.max(drag.origTrimStart + MIN_DUR, drag.origTrimEnd + ds),
          );
          patch = { clip_trim_end_s: Math.round(ne * 10) / 10 };
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
      <div
        ref={rulerRef}
        className="relative h-5 border-b border-white/10 mb-2"
      >
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
              {/* Track gutter */}
              <div className="absolute inset-0 rounded bg-white/5" />

              {/* Card block */}
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
                onMouseDown={(e) =>
                  startDrag(e, card.id, "move", card, rulerRef.current!)
                }
              >
                {/* Left resize handle */}
                <div
                  className="absolute left-0 top-0 h-full w-2.5 flex items-center justify-center hover:bg-black/30 z-10"
                  style={{ cursor: "ew-resize" }}
                  onMouseDown={(e) =>
                    startDrag(e, card.id, "left", card, rulerRef.current!)
                  }
                >
                  <div className="w-px h-3 bg-white/70 rounded-full" />
                </div>

                {/* Label */}
                <span className="text-[10px] text-white font-medium px-3 truncate pointer-events-none">
                  {card.kind === "video" ? "▶" : "⊞"} {card.id.slice(0, 6)}
                </span>

                {/* Right resize handle */}
                <div
                  className="absolute right-0 top-0 h-full w-2.5 flex items-center justify-center hover:bg-black/30 z-10"
                  style={{ cursor: "ew-resize" }}
                  onMouseDown={(e) =>
                    startDrag(e, card.id, "right", card, rulerRef.current!)
                  }
                >
                  <div className="w-px h-3 bg-white/70 rounded-full" />
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* ── Video trim lanes (one per video card with known clip_duration_s) ── */}
      {overlays
        .filter((c) => c.kind === "video" && c.clip_duration_s && c.clip_duration_s > 0)
        .map((card, i) => {
          const clipDur = card.clip_duration_s!;
          const trimStart = card.clip_trim_start_s ?? 0;
          const trimEnd = card.clip_trim_end_s ?? clipDur;
          const lPct = (trimStart / clipDur) * 100;
          const wPct = Math.max(((trimEnd - trimStart) / clipDur) * 100, 1);
          const isTrimDragging =
            drag?.cardId === card.id && (drag.handle === "trim-left" || drag.handle === "trim-right");

          return (
            <div key={`trim-${card.id}`} className="mt-2">
              <span className="text-[9px] text-white/40 mb-1 block">
                Clip trim — {card.id.slice(0, 6)} ({fmtTime(trimStart)}–{fmtTime(trimEnd)} of{" "}
                {fmtTime(clipDur)})
              </span>
              <div
                className="relative h-8 rounded overflow-hidden bg-zinc-800"
                data-trim-container={card.id}
              >
                {/* Filmstrip effect */}
                <div className="absolute inset-0 flex items-center overflow-hidden opacity-30">
                  {Array.from({ length: 16 }).map((_, fi) => (
                    <div
                      key={fi}
                      className="flex-1 h-full border-r border-black bg-zinc-700 flex items-center justify-center"
                    >
                      <div className="w-1 h-5 rounded-sm bg-zinc-600" />
                    </div>
                  ))}
                </div>

                {/* Dimmed outside trim */}
                <div
                  className="absolute top-0 left-0 h-full bg-black/60 pointer-events-none"
                  style={{ width: `${lPct}%` }}
                />
                <div
                  className="absolute top-0 right-0 h-full bg-black/60 pointer-events-none"
                  style={{ width: `${100 - lPct - wPct}%` }}
                />

                {/* Active trim region */}
                <div
                  className={`absolute top-0 h-full border-2 rounded transition-opacity ${
                    isTrimDragging ? "border-white" : "border-white/60"
                  }`}
                  style={{ left: `${lPct}%`, width: `${wPct}%` }}
                >
                  {/* Left trim handle */}
                  <div
                    className="absolute left-0 top-0 h-full w-3 bg-white/20 flex items-center justify-center"
                    style={{ cursor: "ew-resize" }}
                    onMouseDown={(e) => {
                      const el = (e.currentTarget.closest("[data-trim-container]") as HTMLElement) ?? rulerRef.current!;
                      startDrag(e, card.id, "trim-left", card, el);
                    }}
                  >
                    <div className="w-0.5 h-4 bg-white rounded-full" />
                  </div>
                  {/* Right trim handle */}
                  <div
                    className="absolute right-0 top-0 h-full w-3 bg-white/20 flex items-center justify-center"
                    style={{ cursor: "ew-resize" }}
                    onMouseDown={(e) => {
                      const el = (e.currentTarget.closest("[data-trim-container]") as HTMLElement) ?? rulerRef.current!;
                      startDrag(e, card.id, "trim-right", card, el);
                    }}
                  >
                    <div className="w-0.5 h-4 bg-white rounded-full" />
                  </div>
                </div>
              </div>
            </div>
          );
        })}
    </div>
  );
}

// ── Main editor ────────────────────────────────────────────────────────────────

interface Props {
  overlays: MediaOverlay[];
  variantDurationS: number;
  rendering: boolean;
  onUploadRequest: (
    files: { file: File; filename: string; content_type: string; file_size_bytes: number }[],
  ) => void;
  onUpdateCard: (id: string, patch: Partial<MediaOverlay>) => void;
  onRemoveCard: (id: string) => void;
  onApply: () => void;
  onClear: () => void;
}

export default function MediaOverlayEditor({
  overlays,
  variantDurationS,
  rendering,
  onUploadRequest,
  onUpdateCard,
  onRemoveCard,
  onApply,
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
        } ${rendering ? "opacity-40 pointer-events-none" : ""}`}
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
              disabled={rendering}
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
                disabled={rendering}
                onUpdate={(patch) => onUpdateCard(card.id, patch)}
                onRemove={() => onRemoveCard(card.id)}
              />
            ))}
          </div>
        </>
      )}

      {/* ── Action bar ────────────────────────────────────────────── */}
      <div className="flex gap-2 pt-2">
        <button
          disabled={rendering || overlays.length === 0}
          onClick={onApply}
          className="flex-1 rounded-lg bg-lime-400 text-black text-sm font-semibold py-2 px-4 disabled:opacity-40"
        >
          {rendering ? "Applying…" : "Apply cards"}
        </button>
        {overlays.length > 0 && (
          <button
            disabled={rendering}
            onClick={onClear}
            className="rounded-lg border border-white/20 text-white/60 text-sm py-2 px-4 hover:border-white/40 disabled:opacity-40"
          >
            Clear all
          </button>
        )}
      </div>
    </div>
  );
}

// ── Per-card row (position + scale only — timing lives in the timeline) ────────

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
      {/* Header: color dot + kind + id + remove */}
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

      {/* Position presets */}
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

      {/* Scale slider */}
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

      {/* Timing readout (editable fallback numbers) */}
      <div className="flex items-center gap-2">
        <span className="text-xs text-white/30 w-10">Time</span>
        <span className="text-xs text-white/50">
          {card.start_s.toFixed(1)}s – {card.end_s.toFixed(1)}s
        </span>
        <span className="text-xs text-white/30 ml-auto">drag timeline to adjust</span>
      </div>
    </div>
  );
}
