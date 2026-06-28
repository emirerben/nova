"use client";

/**
 * ClipsLane — expandable Clips lane for UnifiedTimeline.
 *
 * Click-to-toggle inline panel (clipsPanel slot). No interactive drag system
 * yet — clips are rendered as a single full-width bar launcher.
 *
 * Extracted from UnifiedTimeline.tsx (T0 refactor). No logic changed.
 */

import { useState } from "react";
import { Playhead } from "@/lib/timeline/Playhead";

// ── Props ─────────────────────────────────────────────────────────────────────

export interface ClipsLaneProps {
  totalDurationS: number;
  currentTimeS: number;
  /** Inline clips editor — rendered inside the lane when expanded. */
  clipsPanel?: React.ReactNode;
  /** Called when the lane expands or collapses. */
  onClipsPanelChange?: (open: boolean) => void;
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function ClipsLane({
  totalDurationS,
  currentTimeS,
  clipsPanel,
  onClipsPanelChange,
}: ClipsLaneProps) {
  const [clipsOpen, setClipsOpen] = useState(false);

  function toggleClipsOpen() {
    const next = !clipsOpen;
    setClipsOpen(next);
    onClipsPanelChange?.(next);
  }

  return (
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
  );
}
