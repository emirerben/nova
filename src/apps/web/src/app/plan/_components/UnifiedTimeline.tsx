"use client";

/**
 * UnifiedTimeline — horizontal multi-lane timeline for plan-item variant editing.
 *
 * Four lanes, one shared playhead:
 *   SFX      — fully interactive (drag/trim/add/remove/undo-redo).
 *   Overlays — read-only bars (click → open "overlays" tab).
 *   Text     — read-only bar  (click → open "text" tab).
 *   Clips    — read-only bar  (click → open "clips" tab / TimelineEditor sheet).
 *
 * All mutations flow through the SFX reducer; everything else is callback-driven.
 * Backend contracts are unchanged — SFX still uses setVariantSoundEffects (debounced).
 *
 * Kill switch: NEXT_PUBLIC_UNIFIED_TIMELINE_ENABLED (default on).
 */

import { useEffect, useReducer, useRef, useState } from "react";
import type { SoundEffectPlacement, MediaOverlay } from "@/lib/plan-api";
import type { SoundEffectSummary } from "@/lib/sfx-api";
import { computeBarPosition } from "@/lib/timeline/bar-position";
import { classifyZone, clampSeconds } from "@/lib/timeline/drag-zone";
import {
  initSfxEditorState,
  sfxReducer,
} from "@/lib/timeline/sfx-timeline-reducer";
import { Playhead } from "@/lib/timeline/Playhead";
import { formatTimecode } from "@/lib/timeline/time-format";

// ── Constants ─────────────────────────────────────────────────────────────────

const HANDLE_PX = 10; // edge-handle hit zone in px
const MIN_TRIM_S = 0.1; // minimum effective SFX duration after trim

const ALLOWED_AUDIO_MIME_TYPES = [
  "audio/mpeg",
  "audio/mp3",
  "audio/mp4",
  "audio/wav",
  "audio/x-wav",
  "audio/aac",
  "audio/ogg",
  "audio/webm",
];
const MAX_SFX_FILE_BYTES = 20 * 1024 * 1024;

// ── Types ─────────────────────────────────────────────────────────────────────

interface UploadFile {
  file: File;
  filename: string;
  content_type: string;
  file_size_bytes: number;
}

export interface UnifiedTimelineProps {
  /** Total variant duration in seconds. */
  totalDurationS: number;
  /** Current video playhead in seconds (lifted from hero player). */
  currentTimeS: number;
  // SFX -----------------------------------------------------------------------
  sfxPlacements: SoundEffectPlacement[];
  sfxGlossaryEffects: SoundEffectSummary[];
  sfxGlossaryLoading: boolean;
  /** True while a render or upload is in progress — disables SFX edits. */
  sfxRendering: boolean;
  sfxUploading: boolean;
  /** Emitted after any SFX mutation (same contract as handleSfxChange in page.tsx). */
  onSfxChange: (placements: SoundEffectPlacement[]) => void;
  /** Called when user drops / selects audio files to upload (parent handles signed URL). */
  onSfxUploadRequest: (files: UploadFile[]) => Promise<void>;
  // Read-only lanes -----------------------------------------------------------
  /** Media overlay cards from the parent's overlayCards state. */
  overlayCards: MediaOverlay[];
  /** Whether the active variant has intro text (shows Text lane). */
  hasText: boolean;
  // Click-through handlers ----------------------------------------------------
  /**
   * Opens the legacy editor for the given lane.
   * Parent (FocusedVariantControls) sets activeTab to show the right panel.
   */
  onOpenTab: (tab: "overlays" | "text" | "clips") => void;
}

/**
 * Drag state during a pointer interaction on an SFX bar.
 * Both "video-time" edges (atS / endS) are tracked so each handle type
 * can compute the correct delta without repeated algebra in every handler.
 */
interface DragState {
  id: string;
  handle: "body" | "left" | "right";
  startClientX: number;
  // Capture values at drag start
  startAtS: number;
  startEndS: number; // video time of bar's right edge = at_s + effectiveDur
  startTrimStartS: number; // trim_start_s ?? 0
  startTrimEndS: number; // trim_end_s ?? duration_s
  durationS: number | null;
  // Live preview (updated on pointer move; committed to reducer on pointer up)
  previewAtS: number;
  previewEndS: number;
}

// ── Ruler helper ──────────────────────────────────────────────────────────────

function tickIntervalFor(totalS: number): number {
  if (totalS <= 10) return 1;
  if (totalS <= 30) return 2;
  if (totalS <= 60) return 5;
  if (totalS <= 120) return 10;
  return 15;
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function UnifiedTimeline({
  totalDurationS,
  currentTimeS,
  sfxPlacements,
  sfxGlossaryEffects,
  sfxGlossaryLoading,
  sfxRendering,
  sfxUploading,
  onSfxChange,
  onSfxUploadRequest,
  overlayCards,
  hasText,
  onOpenTab,
}: UnifiedTimelineProps) {
  // ── SFX reducer (undo/redo) ─────────────────────────────────────────────────
  //
  // Pattern: reducer owns the working copy; parent's sfxPlacements is the
  // "truth from the last save". We emit onChange after each mutation; the
  // debounced parent save echoes the same array reference back as the prop,
  // which the sync effect recognises and ignores (ref equality guard).
  const lastEmitted = useRef<SoundEffectPlacement[]>(sfxPlacements);

  const [editorState, dispatch] = useReducer(
    sfxReducer,
    undefined,
    () => initSfxEditorState(sfxPlacements),
  );

  // Emit to parent when reducer state changes
  useEffect(() => {
    if (editorState.placements === lastEmitted.current) return;
    lastEmitted.current = editorState.placements;
    onSfxChange(editorState.placements);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editorState.placements]);

  // Reset reducer when parent updates externally (e.g. upload adds a placement)
  useEffect(() => {
    if (sfxPlacements === lastEmitted.current) return;
    dispatch({ type: "RESET", placements: sfxPlacements });
    lastEmitted.current = sfxPlacements;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sfxPlacements]);

  const placements = editorState.placements;
  const canUndo = editorState.past.length > 0;
  const canRedo = editorState.future.length > 0;

  // ── Drag ────────────────────────────────────────────────────────────────────

  const [drag, setDrag] = useState<DragState | null>(null);
  const laneContentRef = useRef<HTMLDivElement | null>(null);

  function pxToS(deltaX: number): number {
    const rect = laneContentRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0 || totalDurationS <= 0) return 0;
    return (deltaX / rect.width) * totalDurationS;
  }

  function handleBarPointerDown(
    e: React.PointerEvent<HTMLDivElement>,
    p: SoundEffectPlacement,
  ) {
    if (sfxRendering) return;
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);

    const zone = classifyZone(e.clientX, e.currentTarget.getBoundingClientRect(), HANDLE_PX);
    const durationS = p.duration_s ?? null;
    const trimStartS = p.trim_start_s ?? 0;
    const trimEndS = p.trim_end_s ?? (durationS ?? 0);
    const effectiveDur = durationS ? trimEndS - trimStartS : 0;
    const endS = p.at_s + effectiveDur;

    setDrag({
      id: p.id,
      handle: zone,
      startClientX: e.clientX,
      startAtS: p.at_s,
      startEndS: endS,
      startTrimStartS: trimStartS,
      startTrimEndS: trimEndS,
      durationS,
      previewAtS: p.at_s,
      previewEndS: endS,
    });
  }

  function handleBarPointerMove(
    e: React.PointerEvent<HTMLDivElement>,
    id: string,
  ) {
    if (!drag || drag.id !== id) return;
    const deltaS = pxToS(e.clientX - drag.startClientX);
    const effectiveDur = drag.startEndS - drag.startAtS;

    if (drag.handle === "body" || !drag.durationS) {
      // Body: shift both edges equally
      const maxAt = Math.max(0, totalDurationS - effectiveDur);
      const newAtS = clampSeconds(drag.startAtS + deltaS, maxAt);
      setDrag((d) => d ? { ...d, previewAtS: newAtS, previewEndS: newAtS + effectiveDur } : null);
    } else if (drag.handle === "right") {
      // Right edge: extend/shrink audio tail; left edge stays fixed
      const newEndS = clampSeconds(drag.startEndS + deltaS, totalDurationS);
      const minEnd = drag.startAtS + MIN_TRIM_S;
      setDrag((d) => d ? { ...d, previewEndS: Math.max(minEnd, newEndS) } : null);
    } else {
      // Left edge: keep right edge fixed; at_s + trimStartS both shift
      const newAtS = clampSeconds(drag.startAtS + deltaS, drag.startEndS - MIN_TRIM_S);
      setDrag((d) => d ? { ...d, previewAtS: Math.max(0, newAtS), previewEndS: drag.startEndS } : null);
    }
  }

  function handleBarPointerUp(
    e: React.PointerEvent<HTMLDivElement>,
    id: string,
  ) {
    if (!drag || drag.id !== id) return;
    e.currentTarget.releasePointerCapture(e.pointerId);

    const deltaS = pxToS(e.clientX - drag.startClientX);
    const effectiveDur = drag.startEndS - drag.startAtS;
    const p = placements.find((x) => x.id === id);

    if (!p) { setDrag(null); return; }

    if (drag.handle === "body" || !drag.durationS) {
      const maxAt = Math.max(0, totalDurationS - effectiveDur);
      const newAtS = clampSeconds(drag.startAtS + deltaS, maxAt);
      if (Math.abs(newAtS - p.at_s) > 0.01) {
        dispatch({ type: "MOVE", id, atS: newAtS });
      }
    } else if (drag.handle === "right") {
      // right video-time edge changed → trim_end_s = startTrimEndS + delta
      const newEndS = clampSeconds(drag.startEndS + deltaS, totalDurationS);
      const minEnd = drag.startAtS + MIN_TRIM_S;
      const clampedEndS = Math.max(minEnd, newEndS);
      const newTrimEndS = drag.startTrimEndS + (clampedEndS - drag.startEndS);
      dispatch({
        type: "TRIM",
        id,
        trimStartS: p.trim_start_s ?? null,
        trimEndS: Math.min(drag.durationS, Math.max(drag.startTrimStartS + MIN_TRIM_S, newTrimEndS)),
      });
    } else {
      // left video-time edge changed → at_s shifts, trim_start_s shifts by same delta
      const newAtS = Math.max(0, clampSeconds(drag.startAtS + deltaS, drag.startEndS - MIN_TRIM_S));
      const newTrimStartS = Math.max(0, drag.startTrimStartS + deltaS);
      const maxStart = drag.startTrimEndS - MIN_TRIM_S;
      if (Math.abs(newAtS - p.at_s) > 0.01) {
        dispatch({ type: "MOVE", id, atS: newAtS });
      }
      if (Math.abs(newTrimStartS - drag.startTrimStartS) > 0.01) {
        dispatch({
          type: "TRIM",
          id,
          trimStartS: Math.min(maxStart, newTrimStartS),
          trimEndS: p.trim_end_s ?? null,
        });
      }
    }

    setDrag(null);
  }

  // ── Glossary + upload ────────────────────────────────────────────────────────

  const [selectedGlossaryId, setSelectedGlossaryId] = useState("");
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);

  function addFromGlossary() {
    if (!selectedGlossaryId) return;
    const effect = sfxGlossaryEffects.find((e) => e.id === selectedGlossaryId);
    if (!effect) return;
    dispatch({
      type: "ADD",
      placement: {
        id: crypto.randomUUID(),
        sound_effect_id: effect.id,
        src_gcs_path: "",
        at_s: Math.min(Math.max(0, currentTimeS), Math.max(0, totalDurationS - 0.1)),
        gain: 1.0,
        label: effect.name,
        duration_s: effect.duration_s ?? null,
      },
    });
    setSelectedGlossaryId("");
  }

  async function handleFileSelect(files: FileList | File[]) {
    const valid = Array.from(files).filter(
      (f) => ALLOWED_AUDIO_MIME_TYPES.includes(f.type) && f.size <= MAX_SFX_FILE_BYTES,
    );
    if (valid.length === 0) {
      setUploadError("No valid audio files (mp3/wav/aac/ogg, max 20 MB).");
      return;
    }
    setUploadError(null);
    await onSfxUploadRequest(
      valid.map((f) => ({
        file: f,
        filename: f.name,
        content_type: f.type || "audio/mpeg",
        file_size_bytes: f.size,
      })),
    );
  }

  // ── Per-placement edit row ──────────────────────────────────────────────────

  const [openPlacementId, setOpenPlacementId] = useState<string | null>(null);
  const disabled = sfxRendering || sfxUploading;

  // ── Ruler ───────────────────────────────────────────────────────────────────

  const tickInterval = tickIntervalFor(totalDurationS);
  const ticks =
    totalDurationS > 0
      ? Array.from(
          { length: Math.floor(totalDurationS / tickInterval) + 1 },
          (_, i) => i * tickInterval,
        )
      : [0];

  // ── Bar geometry helper ──────────────────────────────────────────────────────

  function sfxBarGeometry(p: SoundEffectPlacement): { leftPct: number; widthPct: number } {
    const isInDrag = drag?.id === p.id;
    const atS = isInDrag ? drag.previewAtS : p.at_s;
    let endS: number;
    if (isInDrag && p.duration_s) {
      endS = drag.previewEndS;
    } else if (p.duration_s) {
      const trimEndS = p.trim_end_s ?? p.duration_s;
      const trimStartS = p.trim_start_s ?? 0;
      endS = p.at_s + (trimEndS - trimStartS);
    } else {
      // duration unknown — show a minimal marker (2% of total)
      endS = atS + Math.max(0.5, totalDurationS * 0.02);
    }
    return computeBarPosition(atS, endS, totalDurationS);
  }

  // ── Render ───────────────────────────────────────────────────────────────────

  return (
    <div className="select-none overflow-x-auto" data-testid="unified-timeline">
      {/* ── Ruler ── */}
      <div className="flex h-5" style={{ minWidth: "100%" }}>
        <div className="flex-shrink-0 w-14" /> {/* gutter */}
        <div className="relative flex-1 bg-zinc-900/40 border-b border-zinc-800/60">
          {totalDurationS > 0 &&
            ticks.map((t) => {
              const pct = (t / totalDurationS) * 100;
              return (
                <div
                  key={t}
                  className="absolute top-0 h-full pointer-events-none"
                  style={{ left: `${pct}%` }}
                >
                  <div className="w-px h-2 bg-zinc-700" />
                  <span className="absolute left-0.5 top-2 text-[8px] leading-none text-zinc-500 whitespace-nowrap">
                    {formatTimecode(t)}
                  </span>
                </div>
              );
            })}
        </div>
      </div>

      {/* ── Clips lane (read-only) ── */}
      <ReadOnlyLane
        label="Clips"
        totalDurationS={totalDurationS}
        currentTimeS={currentTimeS}
        onClick={() => onOpenTab("clips")}
      >
        <FullWidthBar
          label="Edit clips ↗"
          colorClass="bg-sky-700/30 border-sky-600/40 hover:bg-sky-700/50 text-sky-300/80"
          onClick={() => onOpenTab("clips")}
        />
      </ReadOnlyLane>

      {/* ── Text lane (read-only, only when variant has intro text) ── */}
      {hasText && (
        <ReadOnlyLane
          label="Text"
          totalDurationS={totalDurationS}
          currentTimeS={currentTimeS}
          onClick={() => onOpenTab("text")}
        >
          <FullWidthBar
            label="Edit text ↗"
            colorClass="bg-amber-700/30 border-amber-600/40 hover:bg-amber-700/50 text-amber-300/80"
            onClick={() => onOpenTab("text")}
          />
        </ReadOnlyLane>
      )}

      {/* ── Overlays lane (read-only bars per card) ── */}
      {overlayCards.length > 0 && (
        <ReadOnlyLane
          label="Overlays"
          totalDurationS={totalDurationS}
          currentTimeS={currentTimeS}
          onClick={() => onOpenTab("overlays")}
        >
          {overlayCards.map((card) => {
            const { leftPct, widthPct } = computeBarPosition(
              card.start_s,
              card.end_s,
              totalDurationS,
            );
            return (
              <button
                key={card.id}
                type="button"
                className="absolute inset-y-1 rounded bg-violet-700/30 border border-violet-500/40 hover:bg-violet-700/50 transition-colors flex items-center px-1 overflow-hidden"
                style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                onClick={(e) => { e.stopPropagation(); onOpenTab("overlays"); }}
                title="Click to edit overlays"
              >
                <span className="text-[9px] text-violet-300/80 truncate pointer-events-none">
                  {card.kind === "video" ? "🎬" : "🖼"}
                </span>
              </button>
            );
          })}
        </ReadOnlyLane>
      )}

      {/* ── SFX lane (interactive) ── */}
      <div className="flex h-11 mb-1">
        {/* Gutter */}
        <div className="flex-shrink-0 w-14 flex items-center justify-end pr-2">
          <span className="text-[9px] font-semibold text-zinc-400 uppercase tracking-wider">
            SFX
          </span>
        </div>
        {/* Lane content */}
        <div
          ref={laneContentRef}
          className="relative flex-1 bg-zinc-800/25 border-y border-zinc-700/40 overflow-hidden"
          data-testid="sfx-lane"
        >
          <Playhead currentTimeS={currentTimeS} totalDurationS={totalDurationS} />

          {placements.length === 0 && !sfxRendering && (
            <p className="absolute inset-0 flex items-center justify-center text-[10px] text-zinc-600 pointer-events-none">
              Add a sound effect below
            </p>
          )}

          {placements.map((p) => {
            const { leftPct, widthPct } = sfxBarGeometry(p);
            const isBeingDragged = drag?.id === p.id;
            const isOpen = openPlacementId === p.id;

            return (
              <div
                key={p.id}
                data-placement-id={p.id}
                role="button"
                aria-label={`Sound effect ${p.label ?? ""} at ${p.at_s.toFixed(1)}s`}
                aria-pressed={isOpen}
                className={[
                  "absolute inset-y-1 rounded select-none border flex items-center overflow-hidden",
                  "transition-opacity",
                  isBeingDragged ? "opacity-60 z-10 shadow-lg" : "opacity-100",
                  disabled ? "opacity-40 cursor-not-allowed" : "cursor-grab active:cursor-grabbing",
                  "bg-lime-700/40 border-lime-500/50 hover:bg-lime-700/60",
                ].join(" ")}
                style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                onPointerDown={(e) => handleBarPointerDown(e, p)}
                onPointerMove={(e) => handleBarPointerMove(e, p.id)}
                onPointerUp={(e) => handleBarPointerUp(e, p.id)}
                onClick={(e) => {
                  // Only toggle edit row on click, not after drag
                  if (drag !== null) return;
                  e.stopPropagation();
                  setOpenPlacementId(isOpen ? null : p.id);
                }}
              >
                {/* Trim-handle visual zones (only when duration is known) */}
                {p.duration_s && (
                  <>
                    <div className="absolute left-0 top-0 bottom-0 w-2.5 cursor-col-resize z-10" />
                    <div className="absolute right-0 top-0 bottom-0 w-2.5 cursor-col-resize z-10" />
                  </>
                )}
                <span className="px-1.5 text-[9px] text-lime-100 truncate pointer-events-none leading-none">
                  {p.label ?? formatTimecode(p.at_s)}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {/* ── Per-placement edit rows ── */}
      {placements.map((p) =>
        openPlacementId === p.id ? (
          <div
            key={`edit-${p.id}`}
            className="ml-14 mr-0 mb-2 bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 space-y-2"
          >
            <div className="flex items-center gap-2 flex-wrap">
              <input
                value={p.label ?? ""}
                onChange={(e) =>
                  dispatch({ type: "SET_LABEL", id: p.id, label: e.target.value })
                }
                disabled={disabled}
                placeholder="Label (optional)"
                className="flex-1 min-w-0 bg-zinc-700 border border-zinc-600 rounded px-2 py-1 text-xs text-white placeholder-zinc-500 focus:outline-none focus:border-lime-500"
              />
              <span className="text-xs text-zinc-500 shrink-0 tabular-nums">
                @{p.at_s.toFixed(1)}s
              </span>
              <button
                type="button"
                onClick={() => {
                  dispatch({ type: "REMOVE", id: p.id });
                  setOpenPlacementId(null);
                }}
                disabled={disabled}
                className="shrink-0 text-xs text-zinc-500 hover:text-red-400 disabled:opacity-40"
              >
                Remove
              </button>
            </div>
            <label className="flex items-center gap-2 text-xs text-zinc-400">
              <span className="w-5 shrink-0">Vol</span>
              <input
                type="range"
                min={0}
                max={2}
                step={0.05}
                value={p.gain}
                onChange={(e) =>
                  dispatch({ type: "SET_GAIN", id: p.id, gain: parseFloat(e.target.value) })
                }
                disabled={disabled}
                className="flex-1 accent-lime-500 disabled:opacity-50"
              />
              <span className="w-10 text-right tabular-nums shrink-0">{p.gain.toFixed(2)}×</span>
            </label>
          </div>
        ) : null,
      )}

      {/* ── Add SFX controls ── */}
      <div className="pl-14 pr-0 pt-2 space-y-2">
        {/* Glossary row */}
        <div className="flex gap-2">
          <select
            value={selectedGlossaryId}
            onChange={(e) => setSelectedGlossaryId(e.target.value)}
            disabled={disabled || sfxGlossaryLoading}
            className="flex-1 bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-xs text-white disabled:opacity-50"
          >
            <option value="">
              {sfxGlossaryLoading ? "Loading effects…" : "Pick a sound effect (placed at playhead)…"}
            </option>
            {sfxGlossaryEffects.map((e) => (
              <option key={e.id} value={e.id}>
                {e.name}
                {e.duration_s != null ? ` · ${e.duration_s.toFixed(1)}s` : ""}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={addFromGlossary}
            disabled={disabled || !selectedGlossaryId}
            className="shrink-0 px-3 py-1 bg-lime-800/60 hover:bg-lime-700 text-lime-100 text-xs rounded disabled:opacity-40 transition-colors"
          >
            + Add
          </button>
        </div>

        {/* Upload + undo/redo row */}
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={disabled}
            className="px-3 py-1 bg-zinc-700 hover:bg-zinc-600 text-zinc-300 text-xs rounded disabled:opacity-40 transition-colors"
          >
            Upload audio…
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept={ALLOWED_AUDIO_MIME_TYPES.join(",")}
            multiple
            className="hidden"
            onChange={(e) => {
              if (e.target.files) handleFileSelect(e.target.files);
              e.target.value = "";
            }}
          />

          <div className="flex-1" />

          {/* Undo / redo */}
          <button
            type="button"
            onClick={() => dispatch({ type: "UNDO" })}
            disabled={!canUndo || disabled}
            title="Undo"
            className="px-2 py-1 text-sm text-zinc-400 hover:text-white disabled:opacity-25 transition-colors"
          >
            ↩
          </button>
          <button
            type="button"
            onClick={() => dispatch({ type: "REDO" })}
            disabled={!canRedo || disabled}
            title="Redo"
            className="px-2 py-1 text-sm text-zinc-400 hover:text-white disabled:opacity-25 transition-colors"
          >
            ↪
          </button>
        </div>

        {uploadError && <p className="text-xs text-red-400">{uploadError}</p>}
      </div>

      {/* Bottom hint */}
      <p className="pl-14 pt-1.5 text-[9px] text-zinc-600">
        Clips · Text · Overlays lanes — click to open editor
      </p>
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

interface ReadOnlyLaneProps {
  label: string;
  totalDurationS: number;
  currentTimeS: number;
  onClick: () => void;
  children: React.ReactNode;
}

function ReadOnlyLane({
  label,
  totalDurationS,
  currentTimeS,
  onClick,
  children,
}: ReadOnlyLaneProps) {
  return (
    <div
      role="button"
      tabIndex={0}
      className="flex h-10 group cursor-pointer"
      onClick={onClick}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(); } }}
    >
      {/* Label gutter */}
      <div className="flex-shrink-0 w-14 flex items-center justify-end pr-2">
        <span className="text-[9px] font-semibold text-zinc-400 uppercase tracking-wider truncate">
          {label}
        </span>
      </div>
      {/* Content */}
      <div className="relative flex-1 bg-zinc-800/15 border-y border-zinc-700/30 overflow-hidden group-hover:bg-zinc-800/30 transition-colors">
        <Playhead currentTimeS={currentTimeS} totalDurationS={totalDurationS} />
        {children}
      </div>
    </div>
  );
}

interface FullWidthBarProps {
  label: string;
  colorClass: string;
  onClick: () => void;
}

function FullWidthBar({ label, colorClass, onClick }: FullWidthBarProps) {
  return (
    <button
      type="button"
      className={[
        "absolute inset-1 rounded flex items-center px-2 border transition-colors",
        colorClass,
      ].join(" ")}
      onClick={(e) => { e.stopPropagation(); onClick(); }}
    >
      <span className="text-[10px] truncate pointer-events-none">{label}</span>
    </button>
  );
}
