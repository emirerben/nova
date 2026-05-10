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
  FONT_SIZE_MAP,
  MAX_OVERLAY_TEXT_LEN,
  POSITION_Y_MAP,
  PREVIEW_W,
  SCALE,
  SNAP_ZONES,
  getFontCssFamily,
  isOverlayVisible,
  resolveOverlayPreview,
  resolveSpanColor,
  resolveSpanFont,
  resolveSpanSize,
  snapToNearestZone,
} from "./overlay-constants";
import { useOverlayPreview } from "./useOverlayPreview";

interface OverlayPreviewProps {
  slot: RecipeSlot;
  slotIndex: number;
  currentTimeInSlot: number;
  selection: EditorSelection | null;
  dispatch: Dispatch<EditorAction>;
  previewSubject: string;
}

export function OverlayPreview({
  slot,
  slotIndex,
  currentTimeInSlot,
  selection,
  dispatch,
  previewSubject,
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

  // Reset drag/edit state when slot changes.
  useEffect(() => {
    setDragState(null);
    setEditingIndex(null);
  }, [slotIndex]);

  // Server-rendered overlay PNG. Disabled while inline-editing because the
  // user is staring at the input, not the rendered text — and we don't want
  // the PNG to "lag" behind keystrokes.
  const { pngUrl, error: previewError } = useOverlayPreview({
    slotOverlays: slot.text_overlays,
    slotDurationS: slot.target_duration_s,
    timeInSlotS: currentTimeInSlot,
    previewSubject,
    enabled: editingIndex === null,
  });

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
      requestAnimationFrame(() => inputRef.current?.focus());
    },
    [],
  );

  const commitEdit = useCallback(() => {
    if (editingIndex === null) return;
    if (editingIndex >= slot.text_overlays.length) {
      setEditingIndex(null);
      return;
    }
    dispatch({
      type: "UPDATE_OVERLAY_FIELD",
      slotIndex,
      overlayIndex: editingIndex,
      field: "sample_text",
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
      {/* Server-rendered overlay layer (pixel-identical to export). next/image
          does not work with blob: URLs, so a plain <img> is correct here. */}
      {pngUrl && (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={pngUrl}
          alt=""
          aria-hidden="true"
          draggable={false}
          className="absolute inset-0 w-full h-full pointer-events-none select-none"
          style={{ objectFit: "contain" }}
        />
      )}

      {previewError && (
        <div
          className="absolute top-1 right-1 text-[10px] px-1.5 py-0.5 rounded
                     bg-red-900/70 text-red-200 pointer-events-none"
          title={previewError}
        >
          preview stale
        </div>
      )}

      {/* Snap zone guide lines (visible during drag) */}
      {dragState?.isDragging &&
        SNAP_ZONES.map((zone) => (
          <div
            key={zone.position}
            className="absolute left-0 right-0 border-t border-dashed border-white/20"
            style={{ top: `${zone.y * 100}%` }}
          />
        ))}

      {/* DOM text overlays — live fallback during playback / before the
          server PNG resolves. When pngUrl is set, the <img> above covers
          this layer so the only visible text is the pixel-perfect PNG.
          During playback (cursor changes faster than debounce), pngUrl
          stays null and these DOM overlays provide live feedback. */}
      {slot.text_overlays.map((overlay, oi) => {
        const visible = isOverlayVisible(currentTimeInSlot, overlay);
        const selected = isSelected(oi);
        const isDraggingThis = dragState?.overlayIndex === oi && dragState.isDragging;

        if (!visible && !selected) return null;

        let topPct: number;
        if (isDraggingThis && dragState) {
          const originalY = POSITION_Y_MAP[overlay.position] ?? 0.5;
          const containerHeight = PREVIEW_W * (16 / 9);
          const deltaFraction = (dragState.currentY - dragState.startY) / containerHeight;
          topPct = Math.max(5, Math.min(95, (originalY + deltaFraction) * 100));
        } else {
          topPct = (POSITION_Y_MAP[overlay.position] ?? 0.5) * 100;
        }

        const fontConfig = getFontCssFamily(overlay);
        const nominalSize = FONT_SIZE_MAP[overlay.text_size] ?? FONT_SIZE_MAP.medium;
        const scaledSize = Math.round(nominalSize * SCALE);

        const resolved = resolveOverlayPreview(overlay, previewSubject);
        const displayText =
          resolved || (overlay.role === "cta" ? "(CTA — auto)" : "(empty)");
        const isEditingThis = editingIndex === oi;

        // Hide DOM text when the matching server PNG is showing — the PNG
        // is the authoritative display. Keep this layer interactive (pointer
        // events) so drag/dblclick still work even when the PNG covers it.
        const hideDomText = pngUrl !== null && !isEditingThis;

        return (
          <div
            key={oi}
            className="absolute left-0 right-0 flex justify-center"
            style={{
              top: `${topPct}%`,
              transform: "translateY(-50%)",
              pointerEvents: "auto",
              opacity: visible ? 1 : 0.3,
              cursor: isEditingThis ? "text" : "grab",
            }}
            onPointerDown={(e) => handlePointerDown(e, oi)}
            onDoubleClick={() => startEditing(oi, overlay.sample_text)}
          >
            {isEditingThis ? (
              <input
                ref={inputRef}
                value={editText}
                onChange={(e) => setEditText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitEdit();
                  if (e.key === "Escape") cancelEdit();
                }}
                onBlur={(e) => {
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
                  fontWeight: fontConfig.weight,
                  color: overlay.text_color || "#FFFFFF",
                  textShadow: "0 2px 4px rgba(0,0,0,0.6)",
                  caretColor: "white",
                  width: "90%",
                }}
              />
            ) : overlay.spans && overlay.spans.length > 0 ? (
              <span
                className={`px-2 max-w-[90%] inline-flex flex-wrap items-baseline justify-center gap-x-1 ${
                  selected ? "outline outline-2 outline-dashed outline-white/70 rounded" : ""
                }`}
                style={{ visibility: hideDomText ? "hidden" : "visible" }}
              >
                {overlay.spans.map((span, si) => {
                  const sf = resolveSpanFont(span, overlay);
                  const sc = resolveSpanColor(span, overlay);
                  const ss = resolveSpanSize(span, overlay);
                  const sScaled = Math.round(ss * SCALE);
                  return (
                    <span
                      key={si}
                      style={{
                        fontFamily: sf.family,
                        fontWeight: sf.weight,
                        fontStyle: sf.italic ? "italic" : "normal",
                        fontSize: `${sScaled}px`,
                        color: sc,
                        textShadow: "0 2px 4px rgba(0,0,0,0.6)",
                      }}
                    >
                      {span.text || " "}
                    </span>
                  );
                })}
              </span>
            ) : (
              <span
                className={`px-2 truncate max-w-[90%] inline-block text-center ${
                  selected
                    ? "outline outline-2 outline-dashed outline-white/70 rounded"
                    : ""
                }`}
                style={{
                  fontFamily: fontConfig.family,
                  fontStyle: fontConfig.italic ? "italic" : "normal",
                  fontSize: `${scaledSize}px`,
                  fontWeight: fontConfig.weight,
                  color: overlay.text_color || "#FFFFFF",
                  textShadow: "0 2px 4px rgba(0,0,0,0.6)",
                  visibility: hideDomText ? "hidden" : "visible",
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
