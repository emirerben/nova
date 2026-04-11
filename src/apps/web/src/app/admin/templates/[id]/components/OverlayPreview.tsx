"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { Dispatch } from "react";
import type {
  EditorAction,
  EditorSelection,
  RecipeSlot,
  RecipeTextOverlay,
} from "./recipe-types";
import {
  FONT_FAMILY_MAP,
  FONT_SIZE_MAP,
  MAX_OVERLAY_TEXT_LEN,
  POSITION_Y_MAP,
  PREVIEW_W,
  SCALE,
  SNAP_ZONES,
  isOverlayVisible,
  snapToNearestZone,
} from "./overlay-constants";

interface OverlayPreviewProps {
  slot: RecipeSlot;
  slotIndex: number;
  currentTimeInSlot: number;
  selection: EditorSelection | null;
  dispatch: Dispatch<EditorAction>;
}

export function OverlayPreview({
  slot,
  slotIndex,
  currentTimeInSlot,
  selection,
  dispatch,
}: OverlayPreviewProps) {
  const [dragState, setDragState] = useState<{
    overlayIndex: number;
    startY: number;
    currentY: number;
    isDragging: boolean;
  } | null>(null);

  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editText, setEditText] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  // Reset drag/edit state when slot changes (prevents stale state across slot switches)
  useEffect(() => {
    setDragState(null);
    setEditingIndex(null);
  }, [slotIndex]);

  const isSelected = useCallback(
    (oi: number) =>
      selection?.type === "overlay" &&
      selection.slotIndex === slotIndex &&
      selection.overlayIndex === oi,
    [selection, slotIndex],
  );

  // ── Drag handlers ──────────────────────────────────────────────────────

  const handlePointerDown = useCallback(
    (e: React.PointerEvent, overlayIndex: number) => {
      if (editingIndex !== null) return;
      e.preventDefault();
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
      setDragState({
        overlayIndex,
        startY: e.clientY,
        currentY: e.clientY,
        isDragging: false,
      });
    },
    [editingIndex],
  );

  const handlePointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (!dragState) return;
      const dy = Math.abs(e.clientY - dragState.startY);
      setDragState((prev) =>
        prev
          ? { ...prev, currentY: e.clientY, isDragging: prev.isDragging || dy > 4 }
          : null,
      );
    },
    [dragState],
  );

  const handlePointerUp = useCallback(
    (e: React.PointerEvent) => {
      if (!dragState) return;

      if (dragState.isDragging) {
        // Compute Y fraction from pointer position relative to container
        const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
        const yFraction = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
        const newPosition = snapToNearestZone(yFraction);

        dispatch({
          type: "UPDATE_OVERLAY_FIELD",
          slotIndex,
          overlayIndex: dragState.overlayIndex,
          field: "position",
          value: newPosition,
        });
      } else {
        // Click only — select
        dispatch({
          type: "SET_SELECTED",
          selection: {
            type: "overlay",
            slotIndex,
            overlayIndex: dragState.overlayIndex,
          },
        });
      }

      setDragState(null);
    },
    [dragState, dispatch, slotIndex],
  );

  // ── Inline edit handlers ───────────────────────────────────────────────

  const startEditing = useCallback(
    (overlayIndex: number, currentText: string) => {
      setEditingIndex(overlayIndex);
      setEditText(currentText);
      // Focus input after render
      requestAnimationFrame(() => inputRef.current?.focus());
    },
    [],
  );

  const commitEdit = useCallback(() => {
    if (editingIndex === null) return;
    // Guard against stale index: if an overlay was deleted externally while we
    // were editing, the index may no longer be valid — cancel silently instead
    // of writing to the wrong overlay.
    if (editingIndex >= slot.text_overlays.length) {
      setEditingIndex(null);
      return;
    }
    dispatch({
      type: "UPDATE_OVERLAY_FIELD",
      slotIndex,
      overlayIndex: editingIndex,
      field: "text",
      value: editText,
    });
    setEditingIndex(null);
  }, [editingIndex, editText, dispatch, slotIndex, slot.text_overlays.length]);

  const cancelEdit = useCallback(() => {
    setEditingIndex(null);
  }, []);

  // ── Render ─────────────────────────────────────────────────────────────

  return (
    <div
      className="absolute inset-0 z-10"
      style={{ pointerEvents: "none" }}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={() => setDragState(null)}
    >
      {/* Snap zone guide lines (visible during drag) */}
      {dragState?.isDragging &&
        SNAP_ZONES.map((zone) => (
          <div
            key={zone.position}
            className="absolute left-0 right-0 border-t border-dashed border-white/20"
            style={{ top: `${zone.y * 100}%` }}
          />
        ))}

      {/* Overlay elements */}
      {slot.text_overlays.map((overlay, oi) => {
        const visible = isOverlayVisible(currentTimeInSlot, overlay);
        const selected = isSelected(oi);
        const isDraggingThis = dragState?.overlayIndex === oi && dragState.isDragging;

        // Don't render if not visible and not selected
        if (!visible && !selected) return null;

        const fontConfig = FONT_FAMILY_MAP[overlay.font_style] ?? FONT_FAMILY_MAP.sans;
        const nominalSize = FONT_SIZE_MAP[overlay.text_size] ?? FONT_SIZE_MAP.medium;
        const scaledSize = Math.round(nominalSize * SCALE);

        // Y position: use drag position or snap zone
        let topPct: number;
        if (isDraggingThis && dragState) {
          // During drag, follow pointer relative to container
          // We approximate using the delta from start
          const originalY = POSITION_Y_MAP[overlay.position] ?? 0.5;
          const containerHeight = PREVIEW_W * (16 / 9);
          const deltaFraction = (dragState.currentY - dragState.startY) / containerHeight;
          topPct = Math.max(5, Math.min(95, (originalY + deltaFraction) * 100));
        } else {
          topPct = (POSITION_Y_MAP[overlay.position] ?? 0.5) * 100;
        }

        const displayText = overlay.text || "(empty)";

        return (
          <div
            key={oi}
            className="absolute left-0 right-0 flex justify-center"
            style={{
              top: `${topPct}%`,
              transform: "translateY(-50%)",
              pointerEvents: "auto",
              opacity: visible ? 1 : 0.3,
              cursor: editingIndex === oi ? "text" : "grab",
            }}
            onPointerDown={(e) => handlePointerDown(e, oi)}
            onDoubleClick={() => startEditing(oi, overlay.text)}
          >
            {editingIndex === oi ? (
              <input
                ref={inputRef}
                value={editText}
                onChange={(e) => setEditText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitEdit();
                  if (e.key === "Escape") cancelEdit();
                }}
                onBlur={(e) => {
                  // Don't commit when focus moves to the timeline — the timeline's
                  // pointerdown fires after blur, and we don't want partial text committed
                  const relatedTarget = e.relatedTarget as HTMLElement | null;
                  if (relatedTarget?.closest("[data-overlay-timeline]")) return;
                  commitEdit();
                }}
                maxLength={MAX_OVERLAY_TEXT_LEN}
                className="bg-transparent border-none outline-none text-center px-2"
                style={{
                  fontFamily: fontConfig.family,
                  fontStyle: fontConfig.italic ? "italic" : "normal",
                  fontSize: `${scaledSize}px`,
                  fontWeight: overlay.font_style === "display" || overlay.font_style === "sans" ? 800 : 400,
                  color: overlay.text_color || "#FFFFFF",
                  textShadow: "0 2px 4px rgba(0,0,0,0.6)",
                  caretColor: "white",
                  width: "90%",
                }}
              />
            ) : (
              <span
                className={`px-2 truncate max-w-[90%] inline-block text-center ${
                  selected ? "outline outline-2 outline-dashed outline-white/70 rounded" : ""
                }`}
                style={{
                  fontFamily: fontConfig.family,
                  fontStyle: fontConfig.italic ? "italic" : "normal",
                  fontSize: `${scaledSize}px`,
                  fontWeight: overlay.font_style === "display" || overlay.font_style === "sans" ? 800 : 400,
                  color: overlay.text_color || "#FFFFFF",
                  textShadow: "0 2px 4px rgba(0,0,0,0.6)",
                }}
              >
                {displayText}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}
