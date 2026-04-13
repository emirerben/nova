import type { Dispatch } from "react";
import type { EditorAction, RecipeTextOverlay, TextSpan, TextSize } from "./recipe-types";
import { TEXT_SIZE_OPTIONS } from "./recipe-types";
import {
  FONT_NAMES,
  FONT_REGISTRY,
  resolveSpanFont,
  resolveSpanColor,
  resolveSpanSize,
  SCALE,
} from "./overlay-constants";

// ── Shared styles (match PropertyPanel) ─────────────────────────────────────

const inputClass =
  "w-full bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-zinc-500";

const selectClass =
  "w-full bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-zinc-500";

// ── SpanEditor ──────────────────────────────────────────────────────────────

export function SpanEditor({
  overlay,
  slotIndex,
  overlayIndex,
  dispatch,
}: {
  overlay: RecipeTextOverlay;
  slotIndex: number;
  overlayIndex: number;
  dispatch: Dispatch<EditorAction>;
}) {
  const spans = overlay.spans ?? [];

  const updateSpans = (newSpans: TextSpan[]) => {
    dispatch({
      type: "UPDATE_OVERLAY_FIELD",
      slotIndex,
      overlayIndex,
      field: "spans",
      value: newSpans.length > 0 ? newSpans : undefined,
    });
  };

  const updateSpan = (spanIndex: number, patch: Partial<TextSpan>) => {
    const updated = spans.map((s, i) => (i === spanIndex ? { ...s, ...patch } : s));
    updateSpans(updated);
  };

  const addSpan = () => {
    updateSpans([...spans, { text: "" }]);
  };

  const removeSpan = (spanIndex: number) => {
    const updated = spans.filter((_, i) => i !== spanIndex);
    updateSpans(updated);
  };

  const splitIntoSpans = () => {
    const text = overlay.sample_text || overlay.text || "";
    if (!text.trim()) return;
    const words = text.split(/\s+/).filter(Boolean);
    const newSpans: TextSpan[] = words.map((word) => ({ text: word }));
    updateSpans(newSpans);
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-zinc-400 uppercase tracking-wide">
          Spans ({spans.length})
        </span>
        <div className="flex items-center gap-2">
          {spans.length === 0 && (
            <button
              onClick={splitIntoSpans}
              className="text-xs text-zinc-500 hover:text-zinc-300"
            >
              Split text
            </button>
          )}
          <button
            onClick={addSpan}
            className="text-xs text-blue-400 hover:text-blue-300"
          >
            + Add span
          </button>
        </div>
      </div>

      {/* Inline preview of all spans together */}
      {spans.length > 0 && (
        <div className="flex flex-wrap items-baseline gap-1 bg-zinc-950 rounded px-2 py-1.5 min-h-[28px]">
          {spans.map((span, idx) => {
            const fontConfig = resolveSpanFont(span, overlay);
            const color = resolveSpanColor(span, overlay);
            const size = resolveSpanSize(span, overlay);
            const scaledSize = Math.max(10, Math.round(size * SCALE));
            return (
              <span
                key={idx}
                style={{
                  fontFamily: fontConfig.family,
                  fontWeight: fontConfig.weight,
                  fontStyle: "normal",
                  color,
                  fontSize: `${scaledSize}px`,
                }}
              >
                {span.text || "\u00A0"}
              </span>
            );
          })}
        </div>
      )}

      {spans.map((span, idx) => (
        <SpanRow
          key={idx}
          span={span}
          spanIndex={idx}
          overlay={overlay}
          onUpdate={(patch) => updateSpan(idx, patch)}
          onRemove={() => removeSpan(idx)}
        />
      ))}
    </div>
  );
}

// ── SpanRow ─────────────────────────────────────────────────────────────────

function SpanRow({
  span,
  spanIndex,
  overlay,
  onUpdate,
  onRemove,
}: {
  span: TextSpan;
  spanIndex: number;
  overlay: RecipeTextOverlay;
  onUpdate: (patch: Partial<TextSpan>) => void;
  onRemove: () => void;
}) {
  return (
    <div className="bg-zinc-900/30 border border-zinc-800 rounded px-2 py-1.5 space-y-1.5">
      <div className="flex items-center gap-2">
        <span className="text-[10px] text-zinc-600 w-4 shrink-0">{spanIndex + 1}</span>
        <input
          type="text"
          value={span.text}
          onChange={(e) => onUpdate({ text: e.target.value })}
          placeholder="Span text"
          className={inputClass + " flex-1"}
        />
        <button
          onClick={onRemove}
          className="text-[10px] text-zinc-600 hover:text-red-400 shrink-0"
        >
          &times;
        </button>
      </div>

      <div className="grid grid-cols-4 gap-1.5">
        {/* Font */}
        <select
          value={span.font_family ?? ""}
          onChange={(e) => onUpdate({ font_family: e.target.value || undefined })}
          className={selectClass}
        >
          <option value="">Inherit</option>
          {FONT_NAMES.map((name) => {
            const entry = FONT_REGISTRY[name];
            return (
              <option
                key={name}
                value={name}
                style={{ fontFamily: entry.css_family, fontWeight: entry.weight }}
              >
                {name}
              </option>
            );
          })}
        </select>

        {/* Size */}
        <select
          value={span.text_size ?? ""}
          onChange={(e) =>
            onUpdate({ text_size: (e.target.value as TextSize) || undefined })
          }
          className={selectClass}
        >
          <option value="">Inherit</option>
          {TEXT_SIZE_OPTIONS.map((opt) => (
            <option key={opt} value={opt}>
              {opt}
            </option>
          ))}
        </select>

        {/* Color */}
        <div className="flex items-center gap-1">
          <input
            type="color"
            value={span.text_color || overlay.text_color || "#FFFFFF"}
            onChange={(e) => onUpdate({ text_color: e.target.value.toUpperCase() })}
            className="w-6 h-6 rounded border border-zinc-700 bg-transparent cursor-pointer p-0"
          />
          <input
            type="text"
            value={span.text_color ?? ""}
            onChange={(e) => onUpdate({ text_color: e.target.value || undefined })}
            placeholder="Inherit"
            className={inputClass + " flex-1"}
          />
        </div>

        {/* Placeholder — italic deferred to V2 (requires font variant registry) */}
        <div />
      </div>
    </div>
  );
}
