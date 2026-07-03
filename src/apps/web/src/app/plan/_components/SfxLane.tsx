"use client";

/**
 * SfxLane — interactive SFX lane for UnifiedTimeline.
 *
 * Owns: SFX reducer (undo/redo), drag-move, edge-trim, per-placement
 * edit row, glossary picker, and file upload UI.
 *
 * Extracted from UnifiedTimeline.tsx (T0 refactor). No logic changed.
 */

import { useEffect, useReducer, useRef, useState } from "react";
import type { SoundEffectPlacement } from "@/lib/plan-api";
import type { SoundEffectSummary } from "@/lib/sfx-api";
import { computeBarPosition } from "@/lib/timeline/bar-position";
import { classifyZone, clampSeconds } from "@/lib/timeline/drag-zone";
import {
  initSfxEditorState,
  sfxReducer,
} from "@/lib/timeline/sfx-timeline-reducer";
import { Playhead } from "@/lib/timeline/Playhead";
import { formatTimecode } from "@/lib/timeline/time-format";
import type { UploadFile, SfxDragState } from "./UnifiedTimelineTypes";

// ── Constants ─────────────────────────────────────────────────────────────────

const HANDLE_PX = 10;
const MIN_TRIM_S = 0.1;

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

// ── Props ─────────────────────────────────────────────────────────────────────

export interface SfxLaneProps {
  totalDurationS: number;
  currentTimeS: number;
  sfxPlacements: SoundEffectPlacement[];
  sfxGlossaryEffects: SoundEffectSummary[];
  sfxGlossaryLoading: boolean;
  /** True while a render is in flight — disables lane editing. Derived from the
      variant's shared render_status, so it also fires during overlay/text/clip
      renders. */
  sfxRendering: boolean;
  sfxUploading: boolean;
  onSfxChange: (placements: SoundEffectPlacement[]) => void;
  onSfxUploadRequest: (files: UploadFile[]) => Promise<void>;
  /** Child SFX of pending AI overlay suggestions (006 T3) — rendered as
      read-only dashed-lime ✦ diamonds. Removal stays in the rail ("× sound"). */
  suggestionSfx?: { id: string; sfx: SoundEffectPlacement; staged: boolean }[];
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function SfxLane({
  totalDurationS,
  currentTimeS,
  sfxPlacements,
  sfxGlossaryEffects,
  sfxGlossaryLoading,
  sfxRendering,
  sfxUploading,
  onSfxChange,
  onSfxUploadRequest,
  suggestionSfx,
}: SfxLaneProps) {
  // ── SFX reducer (undo/redo) ─────────────────────────────────────────────────

  const lastEmitted = useRef<SoundEffectPlacement[]>(sfxPlacements);

  const [editorState, dispatch] = useReducer(
    sfxReducer,
    undefined,
    () => initSfxEditorState(sfxPlacements),
  );

  useEffect(() => {
    if (editorState.placements === lastEmitted.current) return;
    lastEmitted.current = editorState.placements;
    onSfxChange(editorState.placements);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editorState.placements]);

  useEffect(() => {
    if (sfxPlacements === lastEmitted.current) return;
    dispatch({ type: "RESET", placements: sfxPlacements });
    lastEmitted.current = sfxPlacements;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sfxPlacements]);

  const placements = editorState.placements;
  const canUndo = editorState.past.length > 0;
  const canRedo = editorState.future.length > 0;

  // ── SFX drag ────────────────────────────────────────────────────────────────

  const [sfxDrag, setSfxDrag] = useState<SfxDragState | null>(null);
  const laneContentRef = useRef<HTMLDivElement | null>(null);

  function pxToS(deltaX: number, ref: React.RefObject<HTMLDivElement | null>): number {
    const rect = ref.current?.getBoundingClientRect();
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

    setSfxDrag({
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

  function handleBarPointerMove(e: React.PointerEvent<HTMLDivElement>, id: string) {
    if (!sfxDrag || sfxDrag.id !== id) return;
    const deltaS = pxToS(e.clientX - sfxDrag.startClientX, laneContentRef);
    const effectiveDur = sfxDrag.startEndS - sfxDrag.startAtS;

    if (sfxDrag.handle === "body" || !sfxDrag.durationS) {
      const maxAt = Math.max(0, totalDurationS - effectiveDur);
      const newAtS = clampSeconds(sfxDrag.startAtS + deltaS, maxAt);
      setSfxDrag((d) => d ? { ...d, previewAtS: newAtS, previewEndS: newAtS + effectiveDur } : null);
    } else if (sfxDrag.handle === "right") {
      const newEndS = clampSeconds(sfxDrag.startEndS + deltaS, totalDurationS);
      const minEnd = sfxDrag.startAtS + MIN_TRIM_S;
      setSfxDrag((d) => d ? { ...d, previewEndS: Math.max(minEnd, newEndS) } : null);
    } else {
      const newAtS = clampSeconds(sfxDrag.startAtS + deltaS, sfxDrag.startEndS - MIN_TRIM_S);
      setSfxDrag((d) => d ? { ...d, previewAtS: Math.max(0, newAtS), previewEndS: sfxDrag.startEndS } : null);
    }
  }

  function handleBarPointerUp(e: React.PointerEvent<HTMLDivElement>, id: string) {
    if (!sfxDrag || sfxDrag.id !== id) return;
    e.currentTarget.releasePointerCapture(e.pointerId);

    const deltaS = pxToS(e.clientX - sfxDrag.startClientX, laneContentRef);
    const effectiveDur = sfxDrag.startEndS - sfxDrag.startAtS;
    const p = placements.find((x) => x.id === id);
    if (!p) { setSfxDrag(null); return; }

    if (sfxDrag.handle === "body" || !sfxDrag.durationS) {
      const maxAt = Math.max(0, totalDurationS - effectiveDur);
      const newAtS = clampSeconds(sfxDrag.startAtS + deltaS, maxAt);
      if (Math.abs(newAtS - p.at_s) > 0.01) dispatch({ type: "MOVE", id, atS: newAtS });
    } else if (sfxDrag.handle === "right") {
      const newEndS = clampSeconds(sfxDrag.startEndS + deltaS, totalDurationS);
      const minEnd = sfxDrag.startAtS + MIN_TRIM_S;
      const clampedEndS = Math.max(minEnd, newEndS);
      const newTrimEndS = sfxDrag.startTrimEndS + (clampedEndS - sfxDrag.startEndS);
      dispatch({
        type: "TRIM", id,
        trimStartS: p.trim_start_s ?? null,
        trimEndS: Math.min(sfxDrag.durationS, Math.max(sfxDrag.startTrimStartS + MIN_TRIM_S, newTrimEndS)),
      });
    } else {
      const newAtS = Math.max(0, clampSeconds(sfxDrag.startAtS + deltaS, sfxDrag.startEndS - MIN_TRIM_S));
      const newTrimStartS = Math.max(0, sfxDrag.startTrimStartS + deltaS);
      const maxStart = sfxDrag.startTrimEndS - MIN_TRIM_S;
      if (Math.abs(newAtS - p.at_s) > 0.01) dispatch({ type: "MOVE", id, atS: newAtS });
      if (Math.abs(newTrimStartS - sfxDrag.startTrimStartS) > 0.01) {
        dispatch({
          type: "TRIM", id,
          trimStartS: Math.min(maxStart, newTrimStartS),
          trimEndS: p.trim_end_s ?? null,
        });
      }
    }
    setSfxDrag(null);
  }

  // ── Glossary + SFX upload ─────────────────────────────────────────────────────

  const [selectedGlossaryId, setSelectedGlossaryId] = useState("");
  const sfxFileInputRef = useRef<HTMLInputElement | null>(null);
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

  async function handleSfxFileSelect(files: FileList | File[]) {
    const valid = Array.from(files).filter(
      (f) => ALLOWED_AUDIO_MIME_TYPES.includes(f.type) && f.size <= MAX_SFX_FILE_BYTES,
    );
    if (valid.length === 0) {
      setUploadError("No valid audio files (mp3/wav/aac/ogg, max 20 MB).");
      return;
    }
    setUploadError(null);
    await onSfxUploadRequest(
      valid.map((f) => ({ file: f, filename: f.name, content_type: f.type || "audio/mpeg", file_size_bytes: f.size })),
    );
  }

  // ── Per-placement edit state ──────────────────────────────────────────────────

  const [openPlacementId, setOpenPlacementId] = useState<string | null>(null);
  const sfxDisabled = sfxRendering || sfxUploading;

  // ── SFX bar geometry ──────────────────────────────────────────────────────────

  function sfxBarGeometry(p: SoundEffectPlacement): { leftPct: number; widthPct: number } {
    const isInDrag = sfxDrag?.id === p.id;
    const atS = isInDrag ? sfxDrag.previewAtS : p.at_s;
    let endS: number;
    if (isInDrag && p.duration_s) {
      endS = sfxDrag.previewEndS;
    } else if (p.duration_s) {
      const trimEndS = p.trim_end_s ?? p.duration_s;
      const trimStartS = p.trim_start_s ?? 0;
      endS = p.at_s + (trimEndS - trimStartS);
    } else {
      endS = atS + Math.max(0.5, totalDurationS * 0.02);
    }
    return computeBarPosition(atS, endS, totalDurationS);
  }

  // ── Render ────────────────────────────────────────────────────────────────────

  return (
    <>
      {/* ── SFX lane track ── */}
      <div className="flex h-11 mb-1">
        <div className="flex-shrink-0 w-14 flex items-center justify-end pr-2">
          <span className="text-[9px] font-semibold text-zinc-400 uppercase tracking-wider">
            SFX
          </span>
        </div>
        <div
          ref={laneContentRef}
          className="relative flex-1 bg-zinc-800/25 border-y border-zinc-700/40 overflow-hidden"
          data-testid="sfx-lane"
        >
          <Playhead currentTimeS={currentTimeS} totalDurationS={totalDurationS} />

          {placements.length === 0 && (suggestionSfx?.length ?? 0) === 0 && !sfxRendering && (
            <p className="absolute inset-0 flex items-center justify-center text-[10px] text-zinc-600 pointer-events-none">
              Add a sound effect below
            </p>
          )}

          {/* Suggested-sfx diamonds (006 T3) — read-only provenance markers.
              Dashed lime-600 + ✦ while pending; staged → solid + ✦ fade
              (005-6A). Audio removal stays in the rail's "× sound" strip. */}
          {(suggestionSfx ?? []).map(({ id, sfx, staged }) => {
            const { leftPct, widthPct } = sfxBarGeometry(sfx);
            return (
              <div
                key={`sug-${id}`}
                data-testid={`sfx-suggestion-${id}`}
                aria-label={`Suggested sound ${sfx.label ?? formatTimecode(sfx.at_s)}`}
                className={`absolute inset-y-1 rounded border-[1.5px] border-lime-600 ${
                  staged ? "border-solid" : "border-dashed"
                } bg-lime-500/15 flex items-center overflow-hidden pointer-events-none select-none`}
                style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
              >
                <span className="px-1 text-[9px] text-lime-200 truncate leading-none">
                  <span
                    aria-hidden
                    className={`motion-safe:transition-opacity motion-safe:duration-300 ${
                      staged ? "opacity-0" : "opacity-100"
                    }`}
                  >
                    ✦{" "}
                  </span>
                  {sfx.label ?? formatTimecode(sfx.at_s)}
                </span>
              </div>
            );
          })}

          {placements.map((p) => {
            const { leftPct, widthPct } = sfxBarGeometry(p);
            const isBeingDragged = sfxDrag?.id === p.id;
            const isOpen = openPlacementId === p.id;

            return (
              <div
                key={p.id}
                data-placement-id={p.id}
                role="button"
                aria-label={`Sound effect ${p.label ?? ""} at ${(p.at_s ?? 0).toFixed(1)}s`}
                aria-pressed={isOpen}
                className={[
                  "absolute inset-y-1 rounded select-none border flex items-center overflow-hidden",
                  "transition-opacity",
                  isBeingDragged ? "opacity-60 z-10 shadow-lg" : "opacity-100",
                  sfxDisabled ? "opacity-40 cursor-not-allowed" : "cursor-grab active:cursor-grabbing",
                  "bg-lime-700/40 border-lime-500/50 hover:bg-lime-700/60",
                ].join(" ")}
                style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                onPointerDown={(e) => handleBarPointerDown(e, p)}
                onPointerMove={(e) => handleBarPointerMove(e, p.id)}
                onPointerUp={(e) => handleBarPointerUp(e, p.id)}
                onClick={(e) => {
                  if (sfxDrag !== null) return;
                  e.stopPropagation();
                  setOpenPlacementId(isOpen ? null : p.id);
                }}
              >
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

      {/* ── Per-SFX edit rows ── */}
      {placements.map((p) =>
        openPlacementId === p.id ? (
          <div
            key={`edit-${p.id}`}
            className="ml-14 mr-0 mb-2 bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 space-y-2"
          >
            <div className="flex items-center gap-2 flex-wrap">
              <input
                value={p.label ?? ""}
                onChange={(e) => dispatch({ type: "SET_LABEL", id: p.id, label: e.target.value })}
                disabled={sfxDisabled}
                placeholder="Label (optional)"
                className="flex-1 min-w-0 bg-zinc-700 border border-zinc-600 rounded px-2 py-1 text-xs text-white placeholder-zinc-500 focus:outline-none focus:border-lime-500"
              />
              <span className="text-xs text-zinc-500 shrink-0 tabular-nums">
                @{(p.at_s ?? 0).toFixed(1)}s
              </span>
              <button
                type="button"
                onClick={() => { dispatch({ type: "REMOVE", id: p.id }); setOpenPlacementId(null); }}
                disabled={sfxDisabled}
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
                value={p.gain ?? 1}
                onChange={(e) => dispatch({ type: "SET_GAIN", id: p.id, gain: parseFloat(e.target.value) })}
                disabled={sfxDisabled}
                className="flex-1 accent-lime-500 disabled:opacity-50"
              />
              <span className="w-10 text-right tabular-nums shrink-0">{(p.gain ?? 1).toFixed(2)}×</span>
            </label>
          </div>
        ) : null,
      )}

      {/* ── Add SFX controls ── */}
      <div className="pl-14 pr-0 pt-2 space-y-2">
        <div className="flex gap-2">
          <select
            value={selectedGlossaryId}
            onChange={(e) => setSelectedGlossaryId(e.target.value)}
            disabled={sfxDisabled || sfxGlossaryLoading}
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
            disabled={sfxDisabled || !selectedGlossaryId}
            className="shrink-0 px-3 py-1 bg-lime-800/60 hover:bg-lime-700 text-lime-100 text-xs rounded disabled:opacity-40 transition-colors"
          >
            + Add
          </button>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => sfxFileInputRef.current?.click()}
            disabled={sfxDisabled}
            className="px-3 py-1 bg-zinc-700 hover:bg-zinc-600 text-zinc-300 text-xs rounded disabled:opacity-40 transition-colors"
          >
            Upload audio…
          </button>
          <input
            ref={sfxFileInputRef}
            type="file"
            accept={ALLOWED_AUDIO_MIME_TYPES.join(",")}
            multiple
            className="hidden"
            onChange={(e) => { if (e.target.files) handleSfxFileSelect(e.target.files); e.target.value = ""; }}
          />
          <div className="flex-1" />
          <button
            type="button"
            onClick={() => dispatch({ type: "UNDO" })}
            disabled={!canUndo || sfxDisabled}
            title="Undo"
            className="px-2 py-1 text-sm text-zinc-400 hover:text-white disabled:opacity-25 transition-colors"
          >
            ↩
          </button>
          <button
            type="button"
            onClick={() => dispatch({ type: "REDO" })}
            disabled={!canRedo || sfxDisabled}
            title="Redo"
            className="px-2 py-1 text-sm text-zinc-400 hover:text-white disabled:opacity-25 transition-colors"
          >
            ↪
          </button>
        </div>

        {/* No "Apply" button: SFX play live in the preview (useSfxPreview) and
            are baked into the MP4 on Download (handleDownload in page.tsx). */}
        {uploadError && <p className="text-xs text-red-400">{uploadError}</p>}
      </div>
    </>
  );
}
