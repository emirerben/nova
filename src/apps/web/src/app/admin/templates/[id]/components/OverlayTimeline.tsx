"use client";

import type { Dispatch } from "react";
import type { EditorAction, EditorSelection, RecipeSlot } from "./recipe-types";
import { OVERLAY_ROLE_COLORS, computeBarPosition } from "./overlay-constants";

interface OverlayTimelineProps {
  slot: RecipeSlot;
  slotIndex: number;
  currentTimeInSlot: number;
  selection: EditorSelection | null;
  dispatch: Dispatch<EditorAction>;
}

export function OverlayTimeline({
  slot,
  slotIndex,
  currentTimeInSlot,
  selection,
  dispatch,
}: OverlayTimelineProps) {
  const duration = slot.target_duration_s;
  if (duration <= 0) return null;

  const overlays = slot.text_overlays;
  if (overlays.length === 0) return null;

  const playheadPct = Math.max(0, Math.min(100, (currentTimeInSlot / duration) * 100));

  return (
    <div className="border border-zinc-800 rounded p-2">
      <span className="text-[10px] text-zinc-500 mb-1 block">
        Overlay Timeline — Slot {slot.position} ({duration.toFixed(1)}s)
      </span>

      <div className="relative" style={{ height: `${overlays.length * 20 + 4}px` }}>
        {/* Playhead */}
        <div
          className="absolute top-0 bottom-0 w-px bg-white/70 z-10"
          style={{ left: `${playheadPct}%` }}
        />

        {/* Overlay bars */}
        {overlays.map((overlay, oi) => {
          const { leftPct, widthPct } = computeBarPosition(overlay, duration);
          const color = OVERLAY_ROLE_COLORS[overlay.role] ?? "#6B7280";
          const isSelected =
            selection?.type === "overlay" &&
            selection.slotIndex === slotIndex &&
            selection.overlayIndex === oi;

          return (
            <button
              key={oi}
              className={`absolute rounded-sm transition-opacity ${
                isSelected ? "ring-1 ring-white/60" : "hover:opacity-80"
              }`}
              style={{
                top: `${oi * 20 + 2}px`,
                left: `${leftPct}%`,
                width: `${Math.max(widthPct, 1)}%`,
                height: "16px",
                backgroundColor: color,
                opacity: isSelected ? 1 : 0.7,
              }}
              title={`${overlay.role}: "${overlay.text?.slice(0, 20) || "(empty)"}"`}
              onClick={() =>
                dispatch({
                  type: "SET_SELECTED",
                  selection: { type: "overlay", slotIndex, overlayIndex: oi },
                })
              }
            />
          );
        })}
      </div>
    </div>
  );
}
