"use client";

/**
 * MediaOverlayEditor — lets users add, configure, and remove timed media-overlay
 * "cards" (images, stickers, short video clips) composited on top of a plan-item
 * variant.
 *
 * This is a presentation-only component: all mutations happen via callbacks so
 * the parent can handle optimistic state updates and API calls.
 *
 * Overlay types:
 *   - Images / stickers (PNG, JPEG, WEBP, HEIC) — static cards.
 *   - Short video clips (MP4, MOV) — frozen last-frame when shorter than window.
 *
 * Design mirrors the clip-timeline editor: a sheet panel opened from
 * PlanVariantEditor, with numeric/preset inputs (no live drag preview in slice 1).
 */

import { useRef, useState } from "react";
import type { MediaOverlay } from "@/lib/plan-api";

// Canvas constants — must match overlay-constants.ts and the backend schema.
const CANVAS_W = 1080;
const CANVAS_H = 1920;
const MIN_SCALE = 0.05;
const MAX_SCALE = 1.0;
const DEFAULT_SCALE = 0.35;

// Position presets (matching backend _POSITION_Y).
const POSITION_PRESETS = [
  { label: "Top", value: "top" as const },
  { label: "Center", value: "center" as const },
  { label: "Bottom", value: "bottom" as const },
];

type Position = "top" | "center" | "bottom" | "custom";

const ALLOWED_MIME_TYPES = [
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/heic",
  "video/mp4",
  "video/quicktime",
];

function kindFromMime(mime: string): "image" | "video" {
  return mime.startsWith("video/") ? "video" : "image";
}

interface Props {
  /** Current card list for this variant. */
  overlays: MediaOverlay[];
  /** Variant duration in seconds (for capping end_s inputs). */
  variantDurationS: number;
  /** Whether a render is in progress (disables edits). */
  rendering: boolean;
  /** Called when the user uploads new card files — parent handles the upload + append. */
  onUploadRequest: (
    files: { file: File; filename: string; content_type: string; file_size_bytes: number }[],
  ) => void;
  /** Called when the user changes an existing card's settings. */
  onUpdateCard: (id: string, patch: Partial<MediaOverlay>) => void;
  /** Called when the user removes a card. */
  onRemoveCard: (id: string) => void;
  /** Called when the user clicks "Apply cards" to trigger the render. */
  onApply: () => void;
  /** Called when the user clicks "Clear all" to remove all cards. */
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
    const valid: { file: File; filename: string; content_type: string; file_size_bytes: number }[] =
      [];
    for (const file of Array.from(fileList)) {
      if (!ALLOWED_MIME_TYPES.includes(file.type)) {
        // Silently skip unsupported types for now; a toast would be better.
        continue;
      }
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
        className={`rounded-xl border-2 border-dashed p-6 text-center transition-colors cursor-pointer ${
          dragOver
            ? "border-lime-400 bg-lime-400/10"
            : "border-white/20 hover:border-white/40"
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
        <p className="text-sm text-white/60">
          Drop images, stickers, or short clips here
        </p>
        <p className="text-xs text-white/40 mt-1">
          PNG · JPEG · WEBP · HEIC · MP4 · MOV
        </p>
      </div>

      {/* ── Card list ─────────────────────────────────────────────── */}
      {overlays.length === 0 ? (
        <p className="text-xs text-white/40 text-center py-2">
          No cards yet. Add one above.
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {overlays.map((card) => (
            <CardRow
              key={card.id}
              card={card}
              maxEndS={variantDurationS}
              disabled={rendering}
              onUpdate={(patch) => onUpdateCard(card.id, patch)}
              onRemove={() => onRemoveCard(card.id)}
            />
          ))}
        </div>
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

// ── Per-card row ───────────────────────────────────────────────────────────────

interface CardRowProps {
  card: MediaOverlay;
  maxEndS: number;
  disabled: boolean;
  onUpdate: (patch: Partial<MediaOverlay>) => void;
  onRemove: () => void;
}

function CardRow({ card, maxEndS, disabled, onUpdate, onRemove }: CardRowProps) {
  const scalePercent = Math.round(card.scale * 100);

  return (
    <div
      className={`rounded-xl border border-white/10 bg-white/5 p-3 flex flex-col gap-2 ${
        disabled ? "opacity-50 pointer-events-none" : ""
      }`}
    >
      {/* Header: kind badge + id + remove */}
      <div className="flex items-center justify-between">
        <span className="text-xs font-mono text-white/40 truncate max-w-[180px]">
          <span
            className={`inline-block rounded px-1 py-0.5 text-[10px] font-semibold mr-1 ${
              card.kind === "video"
                ? "bg-blue-500/20 text-blue-300"
                : "bg-white/10 text-white/60"
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

      {/* Timing */}
      <div className="flex items-center gap-2">
        <span className="text-xs text-white/40 w-10">From</span>
        <input
          type="number"
          min={0}
          max={maxEndS}
          step={0.1}
          value={card.start_s.toFixed(1)}
          onChange={(e) => {
            const v = Math.max(0, Math.min(maxEndS, parseFloat(e.target.value) || 0));
            onUpdate({ start_s: v });
          }}
          className="w-16 rounded bg-white/10 text-white text-xs px-2 py-1 text-right"
        />
        <span className="text-xs text-white/40">s to</span>
        <input
          type="number"
          min={0}
          max={maxEndS}
          step={0.1}
          value={card.end_s.toFixed(1)}
          onChange={(e) => {
            const v = Math.max(0, Math.min(maxEndS, parseFloat(e.target.value) || 0));
            onUpdate({ end_s: v });
          }}
          className="w-16 rounded bg-white/10 text-white text-xs px-2 py-1 text-right"
        />
        <span className="text-xs text-white/40">s</span>
      </div>
    </div>
  );
}
