"use client";

/**
 * TextLane — expandable Text lane for UnifiedTimeline.
 *
 * STUB (T0): current behaviour is a single amber bar that toggles the
 * textPanel slot inline. T5 will replace this stub with a fully interactive
 * multi-block lane (drag-move, edge-trim, add, delete, undo/redo).
 *
 * Extracted from UnifiedTimeline.tsx (T0 refactor). No logic changed.
 */

import { useState } from "react";
import { Playhead } from "@/lib/timeline/Playhead";

// ── Props ─────────────────────────────────────────────────────────────────────

export interface TextLaneProps {
  totalDurationS: number;
  currentTimeS: number;
  hasText: boolean;
  /** Inline text/font editing controls — rendered inside the lane when expanded. */
  textPanel?: React.ReactNode;
  /** Called when the lane expands or collapses — parent can use to switch hero mode. */
  onTextPanelChange?: (open: boolean) => void;
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function TextLane({
  totalDurationS,
  currentTimeS,
  hasText,
  textPanel,
  onTextPanelChange,
}: TextLaneProps) {
  const [textOpen, setTextOpen] = useState(false);

  if (!hasText) return null;

  function toggleTextOpen() {
    const next = !textOpen;
    setTextOpen(next);
    onTextPanelChange?.(next);
  }

  return (
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
  );
}
