"use client";

/**
 * ClipsLane — interactive Clips lane for UnifiedTimeline.
 *
 * When `clipHandle` is provided (via useClipTimeline in the parent), the lane
 * renders one positioned, draggable segment bar per clip — matching the SFX /
 * Overlays / Text lanes.  The expanded InlineClipsEditor panel continues to
 * work as before; header bars and panel share one draft via the handle.
 *
 * When `clipHandle` is not provided (or still loading), falls back to the
 * single full-width launcher bar (original behaviour).
 *
 * Drag semantics (mirrors InlineClipsEditor bar drag):
 *   left-edge  → SET_IN  (trims the clip in-point / start)
 *   right-edge → SET_WINDOW (trims the clip duration / end)
 *   body       → click opens the expanded panel; no free-drag (clips are
 *                sequential, so position = order not absolute start-time)
 */

import { useRef, useState } from "react";
import { Playhead } from "@/lib/timeline/Playhead";
import { classifyZone } from "@/lib/timeline/drag-zone";
import type { ClipTimelineHandle } from "./useClipTimeline";

// ── Constants ─────────────────────────────────────────────────────────────────

/** Edge handle hit-zone in px. Matches SfxLane / TextLane HANDLE_PX. */
const HANDLE_PX = 10;
/** Minimum bar width as % of lane so thin bars stay grabbable. */
const MIN_BAR_PCT = 1.5;

// ── Drag state ─────────────────────────────────────────────────────────────────

interface ClipDragState {
  key: string;
  handle: "left" | "right" | "body";
  startX: number;
  startInS: number;
  startDurS: number;
  /** Total seconds represented by the lane's pixel width at drag-start. */
  scaleS: number;
  containerW: number;
}

// ── Props ─────────────────────────────────────────────────────────────────────

export interface ClipsLaneProps {
  totalDurationS: number;
  currentTimeS: number;
  /** Inline clips editor — rendered inside the lane when expanded. */
  clipsPanel?: React.ReactNode;
  /** Called when the lane expands or collapses. */
  onClipsPanelChange?: (open: boolean) => void;
  /**
   * Clip timeline handle from useClipTimeline.
   * When provided, the lane renders per-clip segment bars with drag.
   * When absent, falls back to the single launcher button.
   */
  clipHandle?: ClipTimelineHandle;
  /**
   * Plan C fix: called when the user clicks a clip bar body (not an edge
   * drag).  Receives the slot.key so the parent can pre-select that clip
   * in InlineClipsEditor, showing only its trim panel on first click.
   */
  onClipBodyClick?: (key: string) => void;
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function ClipsLane({
  totalDurationS,
  currentTimeS,
  clipsPanel,
  onClipsPanelChange,
  clipHandle,
  onClipBodyClick,
}: ClipsLaneProps) {
  const [clipsOpen, setClipsOpen] = useState(false);
  const [drag, setDrag] = useState<ClipDragState | null>(null);
  const laneRef = useRef<HTMLDivElement | null>(null);

  function toggleClipsOpen() {
    const next = !clipsOpen;
    setClipsOpen(next);
    onClipsPanelChange?.(next);
  }

  // ── Derived from clipHandle ───────────────────────────────────────────────────

  const slots = clipHandle?.state.slots ?? [];
  const windows = clipHandle?.windows ?? [];
  const totalS = clipHandle?.totalS ?? totalDurationS;
  const dispatch = clipHandle?.dispatch;

  /** Whether we can render interactive segment bars. */
  const hasSegments =
    clipHandle?.loadState === "ready" &&
    slots.some((s) => !s.removed);

  // ── Drag handlers ──────────────────────────────────────────────────────────────

  function handleBarPointerDown(
    e: React.PointerEvent<HTMLDivElement>,
    key: string,
    inS: number,
    durS: number,
  ) {
    if (!dispatch) return;
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);

    const zone = classifyZone(
      e.clientX,
      e.currentTarget.getBoundingClientRect(),
      HANDLE_PX,
    );

    const containerW = laneRef.current?.getBoundingClientRect().width ?? 1;
    setDrag({
      key,
      handle: zone,
      startX: e.clientX,
      startInS: inS,
      startDurS: durS,
      scaleS: totalS,
      containerW,
    });
  }

  function handleBarPointerMove(
    e: React.PointerEvent<HTMLDivElement>,
    key: string,
  ) {
    if (!drag || drag.key !== key || !dispatch) return;
    const delta = ((e.clientX - drag.startX) / drag.containerW) * drag.scaleS;

    if (drag.handle === "left") {
      // Trim in-point: move left edge, keep end fixed.
      dispatch({
        type: "SET_IN",
        key,
        inS: Math.max(0, drag.startInS + delta),
        record: false,
      });
    } else if (drag.handle === "right") {
      // Trim duration: move right edge, keep in-point fixed.
      dispatch({
        type: "SET_WINDOW",
        key,
        inS: drag.startInS,
        durationS: Math.max(0.3, drag.startDurS + delta),
        record: false,
      });
    }
    // body → no drag; click opens panel (handled in onClick)
  }

  function handleBarPointerUp(
    e: React.PointerEvent<HTMLDivElement>,
    key: string,
  ) {
    if (!drag || drag.key !== key || !dispatch) return;
    // Record the final value so it lands in undo history.
    const slot = slots.find((s) => s.key === key);
    if (slot) {
      if (drag.handle === "left") {
        dispatch({ type: "SET_IN", key, inS: slot.inS, record: true });
      } else if (drag.handle === "right") {
        dispatch({
          type: "SET_WINDOW",
          key,
          inS: slot.inS,
          durationS: slot.durationS ?? drag.startDurS,
          record: true,
        });
      }
    }
    setDrag(null);
  }

  // ── Render ─────────────────────────────────────────────────────────────────────

  return (
    <div>
      <div
        role="button"
        tabIndex={0}
        className="flex h-10 group cursor-pointer"
        onClick={toggleClipsOpen}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggleClipsOpen();
          }
        }}
      >
        {/* Label gutter */}
        <div className="flex-shrink-0 w-14 flex items-center justify-end pr-2">
          <span className="text-[9px] font-semibold text-zinc-500 uppercase tracking-wider truncate">
            Clips
          </span>
        </div>

        {/* Track */}
        <div
          ref={laneRef}
          className="relative flex-1 bg-zinc-50 border-y border-zinc-200 overflow-hidden group-hover:bg-zinc-100 transition-colors"
          onClick={(e) => e.stopPropagation()} // individual bars handle their own clicks
        >
          <Playhead currentTimeS={currentTimeS} totalDurationS={totalDurationS} />

          {hasSegments ? (
            /* ── Per-clip segment bars ────────────────────────────────────────── */
            <>
              {slots.map((slot, i) => {
                if (slot.removed) return null;
                const win = windows[i];
                if (!win || win.durationS <= 0) return null;

                const leftPct = ((win.startS ?? 0) / (totalS || 1)) * 100;
                const widthPct = Math.max(
                  ((win.durationS) / (totalS || 1)) * 100,
                  MIN_BAR_PCT,
                );

                const isDragging = drag?.key === slot.key;
                const clipNum = i + 1;

                return (
                  <div
                    key={slot.key}
                    data-testid={`clip-bar-${slot.key}`}
                    className={[
                      "absolute inset-y-1 rounded border select-none touch-none",
                      "border-sky-400 bg-sky-100",
                      isDragging
                        ? "border-sky-500 bg-sky-200"
                        : "hover:border-sky-500 hover:bg-sky-150",
                      "cursor-default transition-colors",
                    ].join(" ")}
                    style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                    onPointerDown={(e) =>
                      handleBarPointerDown(e, slot.key, slot.inS, slot.durationS ?? 0)
                    }
                    onPointerMove={(e) => handleBarPointerMove(e, slot.key)}
                    onPointerUp={(e) => handleBarPointerUp(e, slot.key)}
                    onPointerCancel={() => setDrag(null)}
                    onClick={(e) => {
                      // Body click → open panel for THIS clip; edge drags skip this.
                      // Plan C: always OPEN (never close) on a bar click — use the ▲/▼
                      // button to collapse.  Emit the key so the parent pre-selects it
                      // in InlineClipsEditor and skips the "all clips" redraw.
                      const zone = classifyZone(
                        e.clientX,
                        e.currentTarget.getBoundingClientRect(),
                        HANDLE_PX,
                      );
                      if (zone === "body") {
                        if (!clipsOpen) {
                          setClipsOpen(true);
                          onClipsPanelChange?.(true);
                        }
                        onClipBodyClick?.(slot.key);
                      }
                    }}
                  >
                    {/* Left trim handle */}
                    <div className="absolute left-0 top-0 bottom-0 w-[10px] flex items-center justify-center cursor-col-resize z-10">
                      <div className="w-[1.5px] h-3 bg-sky-500/70 rounded-full" />
                    </div>

                    {/* Label */}
                    <span className="absolute inset-0 flex items-center justify-center text-[9px] text-sky-700 truncate px-3 pointer-events-none">
                      {widthPct > 8 ? `Clip ${clipNum}` : clipNum}
                    </span>

                    {/* Right trim handle */}
                    <div className="absolute right-0 top-0 bottom-0 w-[10px] flex items-center justify-center cursor-col-resize z-10">
                      <div className="w-[1.5px] h-3 bg-sky-500/70 rounded-full" />
                    </div>
                  </div>
                );
              })}

              {/* Subtle "open editor" affordance on the right */}
              <button
                type="button"
                aria-label="Edit clips"
                className="absolute right-1 inset-y-1 px-1.5 rounded text-[9px] text-sky-600/70 hover:text-sky-700 hover:bg-sky-100 transition-colors"
                onClick={(e) => { e.stopPropagation(); toggleClipsOpen(); }}
              >
                {clipsOpen ? "▲" : "▼"}
              </button>
            </>
          ) : (
            /* ── Fallback: single launcher button ────────────────────────────── */
            <button
              type="button"
              className="absolute inset-1 rounded flex items-center px-2 border border-sky-300 bg-sky-50 hover:bg-sky-100 text-sky-700 transition-colors"
              onClick={(e) => { e.stopPropagation(); toggleClipsOpen(); }}
            >
              <span className="text-[10px] truncate pointer-events-none">
                {clipsOpen ? "Clips ▲" : "Edit clips ▼"}
              </span>
            </button>
          )}
        </div>
      </div>

      {/* Expanded panel */}
      {clipsOpen && clipsPanel && (
        <div className="pl-14 pr-2 pb-3 pt-2 border-b border-zinc-200">
          {clipsPanel}
        </div>
      )}
    </div>
  );
}
