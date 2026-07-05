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

import { type Dispatch, useEffect, useReducer, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { computeBarPosition } from "@/lib/timeline/bar-position";
import { classifyZone, clampSeconds } from "@/lib/timeline/drag-zone";
import {
  initTextEditorState,
  textReducer,
  type TextEditorAction,
  type TextElementBar,
} from "@/lib/timeline/text-timeline-reducer";
import { Playhead } from "@/lib/timeline/Playhead";
import { INTRO_FONTS } from "@/lib/overlay-constants";

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
   * T7: Called when the user clicks "Apply" in the property panel. Triggers an
   * immediate API persist (bypasses the debounce in handleTextElementsChange).
   * Optional — falls back to the debounced path when not provided.
   */
  onApply?: (bars: TextElementBar[]) => void;
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
  /**
   * T8: true when the variant is an Editorial (sequence) variant and the user
   * hasn't made any text-element edits yet.  Shows a one-time amber note to
   * signal that editing the flow makes it user-owned.
   */
  isFirstSequenceEdit?: boolean;
  /**
   * When provided, the TextPropertyPanel for the selected bar portals into this
   * element instead of rendering inline (used by the 3-column TikTok layout to
   * place the panel in the right column).
   */
  textPanelPortalTarget?: HTMLElement | null;
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function TextLane({
  textElements,
  durationSeconds,
  currentTime,
  onTextElementsChange,
  expandedBarId,
  onBarSelect,
  onApply,
  readOnly = false,
  onTrimClamped,
  isFirstSequenceEdit = false,
  textPanelPortalTarget,
}: TextLaneProps) {
  // ── T8: one-time "now user-owned" note for sequence variants ──────────────────
  // Declared before the emit useEffect so the ref is in scope when bars change.
  const [showSequenceNote, setShowSequenceNote] = useState(isFirstSequenceEdit);
  // Ref mirrors the state so we can dismiss from inside the emit useEffect without
  // adding showSequenceNote to its dependency array (which would cause re-runs).
  const showSequenceNoteRef = useRef(isFirstSequenceEdit);
  // Sync with prop: re-show if the parent resets to "first edit" state.
  useEffect(() => {
    if (isFirstSequenceEdit) {
      setShowSequenceNote(true);
      showSequenceNoteRef.current = true;
    }
  }, [isFirstSequenceEdit]);
  // Auto-dismiss after 5 seconds.
  useEffect(() => {
    if (!showSequenceNote) return;
    const timer = setTimeout(() => setShowSequenceNote(false), 5000);
    return () => clearTimeout(timer);
  }, [showSequenceNote]);

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
    // T8: dismiss the sequence note on first user edit.
    if (showSequenceNoteRef.current) {
      showSequenceNoteRef.current = false;
      setShowSequenceNote(false);
    }
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
   * PR-E: All bar roles are now draggable; readOnly gates the entire lane.
   * (Sequence bars were previously locked, but PR-E wires their timing to
   * patchPlanItemSceneTiming so they can be freely dragged too.)
   */
  function isBarLocked(bar: TextElementBar): boolean {
    return readOnly;
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
    // New bar inherits the lane's role: caption lane → narrated_caption, else generative_intro.
    const role: TextElementBar["role"] =
      bars[0]?.role === "narrated_caption" ? "narrated_caption" : "generative_intro";
    const newBar: TextElementBar = {
      id: crypto.randomUUID(),
      text: "",
      start_s: Math.round(startAt * 10) / 10,
      end_s: Math.round(endAt * 10) / 10,
      role,
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
          <span className="text-[9px] font-semibold text-zinc-500 uppercase tracking-wider">
            Text
          </span>
        </div>

        {/* Timeline content */}
        <div
          ref={laneContentRef}
          className="relative flex-1 bg-zinc-50 border-y border-zinc-200 overflow-hidden"
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
              className="absolute inset-0 flex items-center justify-center text-[10px] text-zinc-400 hover:text-amber-500 transition-colors disabled:pointer-events-none disabled:cursor-default"
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
                  bar.role === "narrated_caption"
                    ? locked
                      ? "bg-teal-50 border-teal-200 cursor-not-allowed opacity-60"
                      : isExpanded
                      ? "bg-teal-200 border-teal-400 ring-1 ring-teal-300 cursor-grab active:cursor-grabbing"
                      : "bg-teal-100 border-teal-300 hover:bg-teal-150 cursor-grab active:cursor-grabbing"
                    : locked
                    ? "bg-amber-50 border-amber-200 cursor-not-allowed opacity-60"
                    : isExpanded
                    ? "bg-amber-200 border-amber-400 ring-1 ring-amber-300 cursor-grab active:cursor-grabbing"
                    : "bg-amber-100 border-amber-300 hover:bg-amber-150 cursor-grab active:cursor-grabbing",
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
                    className="absolute left-0 top-0 bottom-0 w-2.5 cursor-col-resize z-10 flex items-center justify-center hover:bg-black/10"
                    aria-hidden="true"
                  >
                    <div className={`w-px h-3 rounded-full ${bar.role === "narrated_caption" ? "bg-teal-500/60" : "bg-amber-500/60"}`} />
                  </div>
                )}

                {/* Text preview */}
                <span className={`px-2 text-[9px] truncate pointer-events-none leading-none ${bar.role === "narrated_caption" ? "text-teal-700" : "text-amber-700"}`}>
                  {textPreview}
                </span>

                {/* Right trim handle */}
                {!locked && (
                  <div
                    className="absolute right-0 top-0 bottom-0 w-2.5 cursor-col-resize z-10 flex items-center justify-center hover:bg-black/10"
                    aria-hidden="true"
                  >
                    <div className={`w-px h-3 rounded-full ${bar.role === "narrated_caption" ? "bg-teal-500/60" : "bg-amber-500/60"}`} />
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
              className={`absolute top-0.5 right-0.5 h-5 w-5 flex items-center justify-center rounded text-xs transition-colors leading-none z-20 ${bars[0]?.role === "narrated_caption" ? "text-teal-400/50 hover:text-teal-300 hover:bg-teal-500/10" : "text-amber-400/50 hover:text-amber-300 hover:bg-amber-500/10"}`}
            >
              +
            </button>
          )}
        </div>
      </div>

      {/* ── Per-bar property panel — portals to right column when target provided ── */}
      {bars.map((bar) =>
        expandedBarId === bar.id ? (
          textPanelPortalTarget
            ? createPortal(
                <TextPropertyPanel
                  key={`panel-${bar.id}`}
                  bar={bar}
                  bars={bars}
                  dispatch={dispatch}
                  onApply={onApply}
                />,
                textPanelPortalTarget,
              )
            : (
                <TextPropertyPanel
                  key={`panel-${bar.id}`}
                  bar={bar}
                  bars={bars}
                  dispatch={dispatch}
                  onApply={onApply}
                />
              )
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
            className="px-2 py-0.5 text-sm text-zinc-400 hover:text-zinc-700 disabled:opacity-25 transition-colors"
          >
            ↩
          </button>
          <button
            type="button"
            onClick={() => dispatch({ type: "REDO" })}
            disabled={!canRedo}
            title="Redo"
            className="px-2 py-0.5 text-sm text-zinc-400 hover:text-zinc-700 disabled:opacity-25 transition-colors"
          >
            ↪
          </button>
        </div>
      )}

      {/* T8: one-time "now user-owned" note for sequence (Editorial) variants. */}
      {showSequenceNote && (
        <div className="ml-14 mr-0 mt-1">
          <div className="text-xs text-amber-400 bg-amber-950/40 rounded px-2 py-1">
            Editing this flow makes it yours — Nova won&apos;t regenerate it automatically.
          </div>
        </div>
      )}
    </div>
  );
}

// ── TextPropertyPanel ─────────────────────────────────────────────────────────
//
// Inline tiered property panel rendered below the selected bar.
//
// Tier 1 (always visible): text content, size, color, highlight color.
// Tier 2 (below fold, scrollable): alignment, stroke width, effect, font.
//
// Phase 5 fields (bg box, shadow, bold/italic, per-word color sweep) are
// intentionally omitted — the Skia renderer does not honor them yet (A5 gate).
//
// Mobile (≤375px): tabbed bottom-sheet layout ("Text" / "Style").
// Desktop (>375px): max-h-[60vh] scrollable with a sticky Apply row.

interface TextPropertyPanelProps {
  bar: TextElementBar;
  bars: TextElementBar[];
  dispatch: Dispatch<TextEditorAction>;
  onApply?: (bars: TextElementBar[]) => void;
}

// ── Panel constants ───────────────────────────────────────────────────────────

const PANEL_SIZE_PRESETS: Array<{ label: string; value: string }> = [
  { label: "S",  value: "small"  },
  { label: "M",  value: "medium" },
  { label: "L",  value: "large"  },
  { label: "XL", value: "xlarge" },
  { label: "J",  value: "jumbo"  },
];

/**
 * Effects supported by the TextElement API schema
 * (Literal["static","fade-in","slide-up","karaoke-line"]).
 * The full INTRO_ANIMATIONS list (e.g. "pop-in","bounce") is only valid for
 * the intro-overlay POST /edit path and would 422 against PUT /text-elements.
 * Expand after widening TextElement.effect in the backend schema.
 * INTRO_FONTS is imported above for the PANEL_FONTS derivation.
 */
const PANEL_EFFECTS: Array<{ label: string; value: string }> = [
  { label: "Static",   value: "static"       },
  { label: "Fade in",  value: "fade-in"      },
  { label: "Slide up", value: "slide-up"     },
  { label: "Karaoke",  value: "karaoke-line" },
];

/**
 * Full font list — derived from INTRO_FONTS (overlay-constants.ts, all
 * non-deprecated registry entries). Previously a 4-font shortlist (PANEL_FONTS);
 * expanded to match EditToolbar so text-lane and instant-editor share the same
 * options (plan D). The `label` field is the registry name (no extra "Bold" suffix).
 */
const PANEL_FONTS: Array<{
  name: string;
  label: string;
  cssFamily: string;
  weight: number;
}> = INTRO_FONTS.map((f) => ({
  name: f.name,
  label: f.name,
  cssFamily: f.cssFamily,
  weight: f.weight,
}));

// ── Helpers ───────────────────────────────────────────────────────────────────

function isValidHex(s: string): boolean {
  return /^#?[0-9a-fA-F]{6}$/.test(s.trim());
}
function normalizeHex(s: string): string {
  const t = s.trim();
  return t.startsWith("#") ? t : `#${t}`;
}

// ── Component ─────────────────────────────────────────────────────────────────

function TextPropertyPanel({
  bar,
  bars,
  dispatch,
  onApply,
}: TextPropertyPanelProps) {
  // Mobile tab (≤375px only — on desktop both tiers are always visible)
  const [tab, setTab] = useState<"text" | "style">("text");

  // Controlled hex inputs — draft avoids dispatching on every keystroke
  const [colorDraft, setColorDraft] = useState(bar.color ?? "");
  const [hlDraft, setHlDraft]       = useState(bar.highlight_color ?? "");

  // Sync drafts when bar changes externally (undo / redo / reset from API)
  useEffect(() => setColorDraft(bar.color ?? ""),         [bar.color]);
  useEffect(() => setHlDraft(bar.highlight_color ?? ""),  [bar.highlight_color]);

  function patch(p: Partial<Omit<TextElementBar, "id" | "role">>) {
    dispatch({ type: "PATCH_BAR", id: bar.id, patch: p });
  }

  function commitColor(hex: string) {
    if (!hex.trim()) return;
    if (isValidHex(hex)) patch({ color: normalizeHex(hex) });
  }
  function commitHighlight(hex: string) {
    if (!hex.trim()) { patch({ highlight_color: undefined }); return; }
    if (isValidHex(hex)) patch({ highlight_color: normalizeHex(hex) });
  }

  const charCount = bar.text.length;
  const sizePx    = bar.size_px      ?? null;
  const strokeW   = bar.stroke_width ?? 0;

  // ── Tier 1 ───────────────────────────────────────────────────────────────────

  const tier1 = (
    <div className="space-y-3">
      {/* Text content */}
      <div>
        <label className="block text-[10px] text-zinc-500 uppercase tracking-wide mb-1">
          Text
        </label>
        <textarea
          value={bar.text}
          onChange={(e) =>
            dispatch({ type: "EDIT_TEXT", id: bar.id, text: e.target.value })
          }
          maxLength={500}
          rows={3}
          className="w-full text-xs bg-zinc-50 border border-zinc-200 rounded-lg px-2 py-1.5 text-zinc-900 placeholder-zinc-400 focus:border-amber-400 focus:outline-none resize-none leading-relaxed"
          placeholder="Enter text…"
        />
        <div
          className={`text-[9px] text-right mt-0.5 tabular-nums ${
            charCount >= 450 ? "text-amber-500" : "text-zinc-400"
          }`}
        >
          {charCount}/500
        </div>
      </div>

      {/* Size — numeric stepper + preset chips */}
      <div>
        <label className="block text-[10px] text-zinc-500 uppercase tracking-wide mb-1">
          Size
        </label>
        <div className="flex items-center gap-2 mb-1.5">
          <button
            type="button"
            onClick={() =>
              patch({ size_px: Math.max(8, (sizePx ?? 72) - 1), size_class: undefined })
            }
            className="w-6 h-6 flex items-center justify-center rounded bg-zinc-100 text-zinc-700 hover:bg-zinc-200 leading-none select-none"
            aria-label="Decrease font size"
          >
            −
          </button>
          <span className="w-12 text-center text-xs text-zinc-700 tabular-nums">
            {sizePx !== null ? `${sizePx}px` : "—"}
          </span>
          <button
            type="button"
            onClick={() =>
              patch({ size_px: Math.min(300, (sizePx ?? 72) + 1), size_class: undefined })
            }
            className="w-6 h-6 flex items-center justify-center rounded bg-zinc-100 text-zinc-700 hover:bg-zinc-200 leading-none select-none"
            aria-label="Increase font size"
          >
            ＋
          </button>
        </div>
        {/* Preset chips */}
        <div className="flex gap-1">
          {PANEL_SIZE_PRESETS.map((p) => (
            <button
              key={p.value}
              type="button"
              onClick={() => patch({ size_class: p.value, size_px: undefined })}
              aria-pressed={bar.size_class === p.value}
              className={`flex-1 text-[10px] rounded py-1 transition-colors ${
                bar.size_class === p.value
                  ? "bg-lime-400 text-black font-semibold"
                  : "bg-zinc-100 text-zinc-600 hover:bg-zinc-200"
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      {/* Text color */}
      <div>
        <label className="block text-[10px] text-zinc-500 uppercase tracking-wide mb-1">
          Color
        </label>
        <div className="flex items-center gap-2">
          <div
            className="w-6 h-6 rounded border border-zinc-300 flex-shrink-0"
            style={{ backgroundColor: bar.color ?? "#ffffff" }}
            aria-hidden="true"
          />
          <input
            type="text"
            value={colorDraft}
            onChange={(e) => setColorDraft(e.target.value)}
            onBlur={(e) => commitColor(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") commitColor(colorDraft); }}
            maxLength={7}
            placeholder="#ffffff"
            className="flex-1 text-xs bg-zinc-50 border border-zinc-200 rounded px-2 py-1 text-zinc-900 placeholder-zinc-400 focus:border-amber-400 focus:outline-none font-mono"
            aria-label="Text color (6-digit hex)"
          />
        </div>
      </div>

      {/* Highlight color (optional) */}
      <div>
        <label className="block text-[10px] text-zinc-500 uppercase tracking-wide mb-1">
          Highlight{" "}
          <span className="text-zinc-400 normal-case font-normal">(optional)</span>
        </label>
        <div className="flex items-center gap-2">
          <div
            className="w-6 h-6 rounded border border-zinc-300 flex-shrink-0"
            style={{
              backgroundColor: bar.highlight_color ?? "transparent",
              backgroundImage: bar.highlight_color
                ? "none"
                : "repeating-linear-gradient(45deg,#52525b 0,#52525b 2px,transparent 0,transparent 50%)",
              backgroundSize: "6px 6px",
            }}
            aria-hidden="true"
          />
          <input
            type="text"
            value={hlDraft}
            onChange={(e) => setHlDraft(e.target.value)}
            onBlur={(e) => commitHighlight(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") commitHighlight(hlDraft); }}
            maxLength={7}
            placeholder="#ffee00 or empty"
            className="flex-1 text-xs bg-zinc-50 border border-zinc-200 rounded px-2 py-1 text-zinc-900 placeholder-zinc-400 focus:border-amber-400 focus:outline-none font-mono"
            aria-label="Highlight color (6-digit hex, optional)"
          />
          {bar.highlight_color && (
            <button
              type="button"
              onClick={() => { setHlDraft(""); patch({ highlight_color: undefined }); }}
              className="text-zinc-400 hover:text-red-500 text-xs px-1 leading-none"
              aria-label="Clear highlight color"
            >
              ✕
            </button>
          )}
        </div>
      </div>
    </div>
  );

  // ── Tier 2 ───────────────────────────────────────────────────────────────────

  const tier2 = (
    <div className="space-y-3">
      {/* Alignment */}
      <div>
        <label className="block text-[10px] text-zinc-500 uppercase tracking-wide mb-1">
          Alignment
        </label>
        <div className="flex gap-1" role="group" aria-label="Text alignment">
          {(["left", "center", "right"] as const).map((a) => (
            <button
              key={a}
              type="button"
              onClick={() => patch({ alignment: a })}
              aria-pressed={bar.alignment === a}
              aria-label={`Align ${a}`}
              className={`flex-1 text-sm py-1 rounded transition-colors ${
                bar.alignment === a
                  ? "bg-lime-400 text-black font-semibold"
                  : "bg-zinc-100 text-zinc-600 hover:bg-zinc-200"
              }`}
            >
              {a === "left" ? "◀" : a === "center" ? "▬" : "▶"}
            </button>
          ))}
        </div>
      </div>

      {/* Stroke width */}
      <div>
        <label className="block text-[10px] text-zinc-500 uppercase tracking-wide mb-1">
          Stroke
        </label>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() =>
              patch({
                stroke_width: Math.max(0, parseFloat((strokeW - 0.5).toFixed(1))),
              })
            }
            className="w-6 h-6 flex items-center justify-center rounded bg-zinc-100 text-zinc-700 hover:bg-zinc-200 leading-none select-none"
            aria-label="Decrease stroke width"
          >
            −
          </button>
          <span className="w-10 text-center text-xs text-zinc-700 tabular-nums">
            {strokeW.toFixed(1)}
          </span>
          <button
            type="button"
            onClick={() =>
              patch({
                stroke_width: Math.min(20, parseFloat((strokeW + 0.5).toFixed(1))),
              })
            }
            className="w-6 h-6 flex items-center justify-center rounded bg-zinc-100 text-zinc-700 hover:bg-zinc-200 leading-none select-none"
            aria-label="Increase stroke width"
          >
            ＋
          </button>
        </div>
      </div>

      {/* Effect */}
      <div>
        <label className="block text-[10px] text-zinc-500 uppercase tracking-wide mb-1">
          Effect
        </label>
        <div className="flex flex-wrap gap-1">
          {PANEL_EFFECTS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              onClick={() => patch({ effect: opt.value })}
              aria-pressed={bar.effect === opt.value}
              className={`text-[10px] px-2 py-1 rounded transition-colors ${
                bar.effect === opt.value
                  ? "bg-lime-400 text-black font-semibold"
                  : "bg-zinc-100 text-zinc-600 hover:bg-zinc-200"
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {/* Font — rendered in their own typeface as preview */}
      <div>
        <label className="block text-[10px] text-zinc-500 uppercase tracking-wide mb-1">
          Font
        </label>
        <div className="grid grid-cols-2 gap-1">
          {PANEL_FONTS.map((f) => (
            <button
              key={f.name}
              type="button"
              onClick={() => patch({ font_family: f.name })}
              aria-pressed={bar.font_family === f.name}
              style={{ fontFamily: f.cssFamily, fontWeight: f.weight }}
              className={`text-[11px] px-2 py-1.5 rounded transition-colors text-left truncate ${
                bar.font_family === f.name
                  ? "bg-lime-400 text-black"
                  : "bg-zinc-100 text-zinc-600 hover:bg-zinc-200"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );

  // ── Render ────────────────────────────────────────────────────────────────────

  return (
    <div className="ml-14 mr-0 mb-1">
      <div
        className="bg-white border border-zinc-200 rounded-lg mt-1 flex flex-col overflow-hidden"
        style={{ maxHeight: "60vh" }}
      >
        {/* Mobile tabs — hidden on desktop, visible on ≤375px */}
        <div className="hidden max-[375px]:flex border-b border-zinc-200 flex-shrink-0">
          {(["text", "style"] as const).map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setTab(t)}
              className={`flex-1 text-xs py-2 capitalize transition-colors ${
                tab === t
                  ? "text-zinc-900 font-semibold border-b-2 border-amber-500"
                  : "text-zinc-500 hover:text-zinc-700"
              }`}
            >
              {t === "text" ? "Text" : "Style"}
            </button>
          ))}
        </div>

        {/* Scrollable body */}
        <div className="overflow-y-auto flex-1 p-3">
          {/* Desktop (>375px): Tier 1 + Tier 2 stacked */}
          <div className="max-[375px]:hidden">
            {tier1}
            <div className="border-t border-zinc-200 mt-3 pt-3">
              {tier2}
            </div>
          </div>
          {/* Mobile (≤375px): one tier at a time via tabs */}
          <div className="hidden max-[375px]:block">
            {tab === "text" ? tier1 : tier2}
          </div>
        </div>

        {/* Sticky Apply row — always at bottom even when content is scrolled */}
        <div className="border-t border-zinc-200 px-3 py-2 flex items-center justify-between bg-zinc-50 flex-shrink-0">
          <button
            type="button"
            onClick={() => dispatch({ type: "UNDO" })}
            className="text-xs text-zinc-400 hover:text-zinc-700 transition-colors px-2 py-1 rounded hover:bg-zinc-100"
          >
            ↩ Undo
          </button>
          <button
            type="button"
            onClick={() => onApply?.(bars)}
            className="text-xs px-4 py-1.5 rounded-full bg-lime-400 text-black font-semibold hover:bg-lime-300 active:bg-lime-500 transition-colors"
          >
            Apply
          </button>
        </div>
      </div>
    </div>
  );
}
