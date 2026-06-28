"use client";

/**
 * TextLane — interactive multi-block Text lane for UnifiedTimeline.
 *
 * T5: replaces the T0 stub (single amber bar) with a fully interactive
 * multi-block lane: drag-move, edge-trim, click-select, add, delete, undo/redo.
 *
 * Modelled on SfxLane.tsx (pointer-capture drag, useReducer, emit-on-change
 * pattern) and OverlayLane.tsx (bar geometry, trim handles, per-bar panel).
 *
 * T6 will wire real text_elements from the API.
 * T7 will replace the placeholder panel with real property controls.
 */

import { useEffect, useReducer, useRef, useState } from "react";
import { computeBarPosition } from "@/lib/timeline/bar-position";
import { classifyZone, clampSeconds } from "@/lib/timeline/drag-zone";
import {
  initTextEditorState,
  textReducer,
  type TextElementBar,
} from "@/lib/timeline/text-timeline-reducer";
import { Playhead } from "@/lib/timeline/Playhead";

// ── Re-export so callers can import the type from this file ───────────────────

export type { TextElementBar };

// ── Constants ─────────────────────────────────────────────────────────────────

/** Edge handle hit-zone in px. Matches SfxLane's HANDLE_PX. */
const HANDLE_PX = 10;
/** Minimum bar duration in seconds (prevents zero-duration edge-trim glitches). */
const MIN_DUR_S = 0.2;
/** Default duration for a newly-added text bar. */
const DEFAULT_DUR_S = 2.0;

// ── Drag state ─────────────────────────────────────────────────────────────────

interface TextBarDragState {
  id: string;
  handle: "body" | "left" | "right";
  startClientX: number;
  origStartS: number;
  origEndS: number;
  /** Live-preview start while dragging (before dispatch on pointer-up). */
  previewStartS: number;
  /** Live-preview end while dragging. */
  previewEndS: number;
}

// ── Props ─────────────────────────────────────────────────────────────────────

export interface TextLaneProps {
  /** Current text element bars (from API + local edits). T6 wires real data. */
  textElements: TextElementBar[];
  /** Total assembled-video duration in seconds. */
  durationSeconds: number;
  /** Playhead position in seconds (from the hero player). */
  currentTime: number;
  /** Called after every reducer mutation so the parent can persist. */
  onTextElementsChange: (bars: TextElementBar[]) => void;
  /** Which bar's inline panel is open (controlled from outside for T7 compat). */
  expandedBarId: string | null;
  /** Called on bar click / keyboard Enter / click-outside. */
  onBarSelect: (id: string | null) => void;
  /**
   * When true: the entire lane is read-only (no drag, no add, no delete).
   * Pass true for sequence-mode variants (`intro_mode === "sequence"`).
   */
  readOnly?: boolean;
  /**
   * T10 State 4: called when a drag trim is clamped to the minimum bar duration
   * (MIN_DUR_S). Parent can show a "Minimum 0.Xs" note.
   */
  onTrimClamped?: () => void;
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function TextLane({
  textElements,
  durationSeconds,
  currentTime,
  onTextElementsChange,
  expandedBarId,
  onBarSelect,
  readOnly = false,
  onTrimClamped,
}: TextLaneProps) {
  // ── Reducer (undo/redo) ───────────────────────────────────────────────────────

  const lastEmitted = useRef<TextElementBar[]>(textElements);

  const [editorState, dispatch] = useReducer(
    textReducer,
    undefined,
    () => initTextEditorState(textElements),
  );

  // Emit to parent whenever bars change (but not on RESET from parent)
  useEffect(() => {
    if (editorState.bars === lastEmitted.current) return;
    lastEmitted.current = editorState.bars;
    onTextElementsChange(editorState.bars);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editorState.bars]);

  // Accept incoming state updates from parent (T6 will trigger this on API refresh)
  useEffect(() => {
    if (textElements === lastEmitted.current) return;
    dispatch({ type: "RESET", bars: textElements });
    lastEmitted.current = textElements;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [textElements]);

  const bars = editorState.bars;
  const canUndo = editorState.past.length > 0;
  const canRedo = editorState.future.length > 0;

  // ── Pointer-capture drag ──────────────────────────────────────────────────────

  const [drag, setDrag] = useState<TextBarDragState | null>(null);
  const laneContentRef = useRef<HTMLDivElement | null>(null);

  /** Convert a pixel delta to seconds given the current lane width. */
  function pxToS(deltaX: number): number {
    const rect = laneContentRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0 || durationSeconds <= 0) return 0;
    return (deltaX / rect.width) * durationSeconds;
  }

  function handleBarPointerDown(
    e: React.PointerEvent<HTMLDivElement>,
    bar: TextElementBar,
  ) {
    if (readOnly || isBarLocked(bar)) return;
    e.preventDefault();
    e.stopPropagation();
    // Pointer capture: keeps events arriving here even when pointer leaves the element.
    e.currentTarget.setPointerCapture(e.pointerId);

    const zone = classifyZone(
      e.clientX,
      e.currentTarget.getBoundingClientRect(),
      HANDLE_PX,
    );
    setDrag({
      id: bar.id,
      handle: zone,
      startClientX: e.clientX,
      origStartS: bar.start_s,
      origEndS: bar.end_s,
      previewStartS: bar.start_s,
      previewEndS: bar.end_s,
    });
  }

  function handleBarPointerMove(
    e: React.PointerEvent<HTMLDivElement>,
    id: string,
  ) {
    if (!drag || drag.id !== id) return;
    const deltaS = pxToS(e.clientX - drag.startClientX);
    const dur = drag.origEndS - drag.origStartS;

    if (drag.handle === "body") {
      const maxStart = Math.max(0, durationSeconds - dur);
      const newStart = clampSeconds(drag.origStartS + deltaS, maxStart);
      setDrag((d) =>
        d ? { ...d, previewStartS: newStart, previewEndS: newStart + dur } : null,
      );
    } else if (drag.handle === "right") {
      const newEnd = clampSeconds(drag.origEndS + deltaS, durationSeconds);
      const minEnd = drag.origStartS + MIN_DUR_S;
      setDrag((d) => d ? { ...d, previewEndS: Math.max(minEnd, newEnd) } : null);
    } else {
      // left handle — trim start
      const newStart = clampSeconds(
        drag.origStartS + deltaS,
        drag.origEndS - MIN_DUR_S,
      );
      setDrag((d) =>
        d ? { ...d, previewStartS: Math.max(0, newStart) } : null,
      );
    }
  }

  function handleBarPointerUp(
    e: React.PointerEvent<HTMLDivElement>,
    id: string,
  ) {
    if (!drag || drag.id !== id) return;
    e.currentTarget.releasePointerCapture(e.pointerId);

    const deltaS = pxToS(e.clientX - drag.startClientX);
    const bar = bars.find((b) => b.id === id);
    if (!bar) { setDrag(null); return; }

    const dur = drag.origEndS - drag.origStartS;

    if (drag.handle === "body") {
      const maxStart = Math.max(0, durationSeconds - dur);
      const newStart = clampSeconds(drag.origStartS + deltaS, maxStart);
      if (Math.abs(newStart - bar.start_s) > 0.01) {
        dispatch({ type: "MOVE_BAR", id, start_s: newStart });
      }
    } else if (drag.handle === "right") {
      const rawEnd = clampSeconds(drag.origEndS + deltaS, durationSeconds);
      const clampedEnd = Math.max(drag.origStartS + MIN_DUR_S, rawEnd);
      if (Math.abs(clampedEnd - bar.end_s) > 0.01) {
        dispatch({ type: "TRIM_END", id, end_s: clampedEnd });
      }
      // State 4: notify parent when right-trim was clamped to minimum duration.
      if (rawEnd < drag.origStartS + MIN_DUR_S) onTrimClamped?.();
    } else {
      const intendedStart = drag.origStartS + deltaS;
      const newStart = Math.max(
        0,
        clampSeconds(intendedStart, drag.origEndS - MIN_DUR_S),
      );
      if (Math.abs(newStart - bar.start_s) > 0.01) {
        dispatch({ type: "TRIM_START", id, start_s: newStart });
      }
      // State 4: notify parent when left-trim was clamped to minimum duration.
      if (intendedStart > drag.origEndS - MIN_DUR_S) onTrimClamped?.();
    }
    setDrag(null);
  }

  // ── Locked-bar predicate ──────────────────────────────────────────────────────

  /**
   * Sequence bars are locked individually even in a non-readOnly lane.
   * (readOnly locks the whole lane; sequence role locks the specific bar.)
   */
  function isBarLocked(bar: TextElementBar): boolean {
    return readOnly || bar.role === "generative_sequence";
  }

  // ── Add ───────────────────────────────────────────────────────────────────────

  function handleAdd() {
    if (readOnly) return;
    // Place after the last bar's end (+ small gap), or at playhead if no bars.
    const lastBar = bars[bars.length - 1];
    const startAt = lastBar
      ? Math.min(lastBar.end_s + 0.5, Math.max(0, durationSeconds - DEFAULT_DUR_S))
      : Math.min(currentTime, Math.max(0, durationSeconds - DEFAULT_DUR_S));
    const endAt = Math.min(startAt + DEFAULT_DUR_S, durationSeconds);
    const newBar: TextElementBar = {
      id: crypto.randomUUID(),
      text: "",
      start_s: Math.round(startAt * 10) / 10,
      end_s: Math.round(endAt * 10) / 10,
      role: "generative_intro",
    };
    dispatch({ type: "ADD_TEXT", bar: newBar });
    // Auto-open the new bar's panel so the user can see it was added.
    onBarSelect(newBar.id);
  }

  // ── Keyboard ──────────────────────────────────────────────────────────────────

  function handleBarKeyDown(
    e: React.KeyboardEvent<HTMLDivElement>,
    bar: TextElementBar,
  ) {
    if (e.key === "Enter") {
      e.preventDefault();
      onBarSelect(expandedBarId === bar.id ? null : bar.id);
    } else if ((e.key === "Delete" || e.key === "Backspace") && !isBarLocked(bar)) {
      e.preventDefault();
      dispatch({ type: "DELETE_BAR", id: bar.id });
      if (expandedBarId === bar.id) onBarSelect(null);
    }
  }

  // ── Bar geometry ──────────────────────────────────────────────────────────────

  function barGeometry(bar: TextElementBar): { leftPct: number; widthPct: number } {
    const isInDrag = drag?.id === bar.id;
    const startS = isInDrag ? drag.previewStartS : bar.start_s;
    const endS = isInDrag ? drag.previewEndS : bar.end_s;
    return computeBarPosition(startS, endS, durationSeconds);
  }

  // ── Render ────────────────────────────────────────────────────────────────────

  return (
    <div role="group" aria-label="Text elements lane">
      {/* ── Lane header track ── */}
      <div className="flex h-11 mb-0">
        {/* Label */}
        <div className="flex-shrink-0 w-14 flex items-center justify-end pr-2">
          <span className="text-[9px] font-semibold text-zinc-400 uppercase tracking-wider">
            Text
          </span>
        </div>

        {/* Timeline content */}
        <div
          ref={laneContentRef}
          className="relative flex-1 bg-zinc-800/25 border-y border-zinc-700/40 overflow-hidden"
          data-testid="text-lane"
          onClick={(e) => {
            // Click directly on the track (not on a bar) → deselect
            if (e.target === e.currentTarget) onBarSelect(null);
          }}
        >
          <Playhead currentTimeS={currentTime} totalDurationS={durationSeconds} />

          {/* Empty state */}
          {bars.length === 0 && (
            <button
              type="button"
              onClick={handleAdd}
              disabled={readOnly}
              className="absolute inset-0 flex items-center justify-center text-[10px] text-zinc-600 hover:text-amber-400/60 transition-colors disabled:pointer-events-none disabled:cursor-default"
            >
              No text yet — ＋ Add text
            </button>
          )}

          {/* Text bars */}
          {bars.map((bar) => {
            const { leftPct, widthPct } = barGeometry(bar);
            const isBeingDragged = drag?.id === bar.id;
            const isExpanded = expandedBarId === bar.id;
            const locked = isBarLocked(bar);
            const textPreview = bar.text.trim() || "—";

            return (
              <div
                key={bar.id}
                role="button"
                tabIndex={0}
                aria-label={`Text '${textPreview.slice(0, 30)}' ${bar.start_s.toFixed(1)}s–${bar.end_s.toFixed(1)}s`}
                aria-pressed={isExpanded}
                className={[
                  "absolute inset-y-1 rounded select-none border flex items-center overflow-hidden",
                  "transition-opacity",
                  isBeingDragged ? "opacity-60 z-10 shadow-lg" : "opacity-100",
                  locked
                    ? "bg-amber-900/25 border-amber-800/30 cursor-not-allowed opacity-60"
                    : isExpanded
                    ? "bg-amber-400/40 border-amber-400/70 ring-1 ring-amber-400/50 cursor-grab active:cursor-grabbing"
                    : "bg-amber-700/40 border-amber-500/50 hover:bg-amber-700/60 cursor-grab active:cursor-grabbing",
                ].join(" ")}
                style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                onPointerDown={(e) => handleBarPointerDown(e, bar)}
                onPointerMove={(e) => handleBarPointerMove(e, bar.id)}
                onPointerUp={(e) => handleBarPointerUp(e, bar.id)}
                onClick={(e) => {
                  if (drag !== null) return; // ignore click if pointer-up ended a drag
                  e.stopPropagation();
                  onBarSelect(isExpanded ? null : bar.id);
                }}
                onKeyDown={(e) => handleBarKeyDown(e, bar)}
              >
                {/* Left trim handle (hidden for locked bars) */}
                {!locked && (
                  <div
                    className="absolute left-0 top-0 bottom-0 w-2.5 cursor-col-resize z-10 flex items-center justify-center hover:bg-black/20"
                    aria-hidden="true"
                  >
                    <div className="w-px h-3 bg-amber-300/50 rounded-full" />
                  </div>
                )}

                {/* Text preview */}
                <span className="px-2 text-[9px] text-amber-100 truncate pointer-events-none leading-none">
                  {textPreview}
                </span>

                {/* Right trim handle */}
                {!locked && (
                  <div
                    className="absolute right-0 top-0 bottom-0 w-2.5 cursor-col-resize z-10 flex items-center justify-center hover:bg-black/20"
                    aria-hidden="true"
                  >
                    <div className="w-px h-3 bg-amber-300/50 rounded-full" />
                  </div>
                )}
              </div>
            );
          })}

          {/* Add button — floats at top-right of the lane */}
          {!readOnly && bars.length > 0 && (
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); handleAdd(); }}
              title="Add text block"
              aria-label="Add text block"
              className="absolute top-0.5 right-0.5 h-5 w-5 flex items-center justify-center rounded text-amber-400/50 hover:text-amber-300 hover:bg-amber-500/10 text-xs transition-colors leading-none z-20"
            >
              +
            </button>
          )}
        </div>
      </div>

      {/* ── Per-bar inline panel (Phase 4 placeholder) ── */}
      {bars.map((bar) =>
        expandedBarId === bar.id ? (
          <div key={`panel-${bar.id}`} className="ml-14 mr-0 mb-1">
            <div className="bg-zinc-800 rounded-lg p-3 mt-1 text-xs text-zinc-400">
              Text styling panel — coming in Phase 4
            </div>
          </div>
        ) : null,
      )}

      {/* ── Undo / redo row (visible when bars exist and not read-only) ── */}
      {!readOnly && bars.length > 0 && (
        <div className="pl-14 pr-1 pb-1 flex justify-end gap-0">
          <button
            type="button"
            onClick={() => dispatch({ type: "UNDO" })}
            disabled={!canUndo}
            title="Undo"
            className="px-2 py-0.5 text-sm text-zinc-500 hover:text-white disabled:opacity-25 transition-colors"
          >
            ↩
          </button>
          <button
            type="button"
            onClick={() => dispatch({ type: "REDO" })}
            disabled={!canRedo}
            title="Redo"
            className="px-2 py-0.5 text-sm text-zinc-500 hover:text-white disabled:opacity-25 transition-colors"
          >
            ↪
          </button>
        </div>
      )}
    </div>
  );
}
