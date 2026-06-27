"use client";

/**
 * SoundEffectEditor — lets users add, configure, and remove timed sound-effect
 * placements layered over the audio of a plan-item variant.
 *
 * Presentation-only component: all mutations flow through callbacks so the parent
 * (plan-item page) handles optimistic state and API calls.
 *
 * Two sources:
 *   1. Admin glossary (published SoundEffect rows from GET /sound-effects).
 *   2. User uploads (audio files the user drags in directly).
 */

import { useEffect, useRef, useState } from "react";
import type { SoundEffectPlacement } from "@/lib/plan-api";
import type { SoundEffectSummary } from "@/lib/sfx-api";

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

const MAX_SFX_FILE_BYTES = 20 * 1024 * 1024; // 20 MB

interface Props {
  /** Current placement list for this variant. */
  placements: SoundEffectPlacement[];
  /** Variant total duration in seconds — used to clamp at_s. */
  variantDurationS: number;
  /** Current video playhead position in seconds (lifted from parent). */
  currentTimeS: number;
  /** Whether a render is in progress — disables edits. */
  rendering: boolean;
  /** Published glossary effects to show in the picker. */
  glossaryEffects: SoundEffectSummary[];
  /** True while the parent is loading glossary effects. */
  glossaryLoading: boolean;
  /**
   * Called when the user wants to upload audio files.
   * Parent handles the signed-URL upload + returns the GCS paths.
   */
  onUploadRequest: (
    files: { file: File; filename: string; content_type: string; file_size_bytes: number }[],
  ) => Promise<void>;
  /** Called when the placement list changes (add/remove/edit). Auto-saves via debounce. */
  onChange: (placements: SoundEffectPlacement[]) => void;
}

export default function SoundEffectEditor({
  placements,
  variantDurationS,
  currentTimeS,
  rendering,
  glossaryEffects,
  glossaryLoading,
  onUploadRequest,
  onChange,
}: Props) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [selectedGlossaryId, setSelectedGlossaryId] = useState<string>("");

  // ── Glossary picker ────────────────────────────────────────────────────────

  function addFromGlossary() {
    if (!selectedGlossaryId) return;
    const effect = glossaryEffects.find((e) => e.id === selectedGlossaryId);
    if (!effect) return;
    const newPlacement: SoundEffectPlacement = {
      id: crypto.randomUUID(),
      sound_effect_id: effect.id,
      src_gcs_path: "",   // server resolves this from sound_effect_id
      at_s: Math.min(Math.max(0, currentTimeS), Math.max(0, variantDurationS - 0.1)),
      gain: 1.0,
      label: effect.name,
      duration_s: effect.duration_s ?? null,
    };
    onChange([...placements, newPlacement]);
    setSelectedGlossaryId("");
  }

  // ── User-upload ────────────────────────────────────────────────────────────

  async function processFiles(rawFiles: FileList | File[]) {
    const files = Array.from(rawFiles);
    const valid = files.filter((f) => {
      if (!ALLOWED_AUDIO_MIME_TYPES.includes(f.type)) return false;
      if (f.size > MAX_SFX_FILE_BYTES) return false;
      return true;
    });
    if (valid.length === 0) {
      setUploadError("No valid audio files (mp3/wav/aac/ogg, max 20 MB).");
      return;
    }
    setUploadError(null);
    await onUploadRequest(
      valid.map((f) => ({
        file: f,
        filename: f.name,
        content_type: f.type || "audio/mpeg",
        file_size_bytes: f.size,
      })),
    );
  }

  function handleFileInput(e: React.ChangeEvent<HTMLInputElement>) {
    if (e.target.files) processFiles(e.target.files);
    e.target.value = "";
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer.files) processFiles(e.dataTransfer.files);
  }

  // ── Placement mutations ────────────────────────────────────────────────────

  function updatePlacement(idx: number, patch: Partial<SoundEffectPlacement>) {
    const updated = placements.map((p, i) => (i === idx ? { ...p, ...patch } : p));
    onChange(updated);
  }

  function removePlacement(idx: number) {
    onChange(placements.filter((_, i) => i !== idx));
  }

  // ── Render ─────────────────────────────────────────────────────────────────

  const disabled = rendering;
  const maxAt = Math.max(0, variantDurationS - 0.05);

  return (
    <div className="space-y-4">
      {/* ── Glossary picker ── */}
      <div className="space-y-2">
        <p className="text-xs font-medium text-zinc-400 uppercase tracking-wide">
          Add from glossary
        </p>
        <div className="flex gap-2">
          <select
            value={selectedGlossaryId}
            onChange={(e) => setSelectedGlossaryId(e.target.value)}
            disabled={disabled || glossaryLoading}
            className="flex-1 bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-sm text-white disabled:opacity-50"
          >
            <option value="">
              {glossaryLoading ? "Loading…" : "Pick a sound effect…"}
            </option>
            {glossaryEffects.map((e) => (
              <option key={e.id} value={e.id}>
                {e.name}
                {e.duration_s != null ? ` (${e.duration_s.toFixed(1)}s)` : ""}
              </option>
            ))}
          </select>
          <button
            onClick={addFromGlossary}
            disabled={disabled || !selectedGlossaryId}
            className="px-3 py-1.5 bg-zinc-700 hover:bg-zinc-600 text-sm rounded disabled:opacity-40"
          >
            + Add
          </button>
        </div>
        <p className="text-xs text-zinc-500">
          &ldquo;Add&rdquo; places the effect at the current playhead position ({currentTimeS.toFixed(1)}s).
        </p>
      </div>

      {/* ── Upload your own ── */}
      <div className="space-y-2">
        <p className="text-xs font-medium text-zinc-400 uppercase tracking-wide">
          Upload your own
        </p>
        <div
          className={`border-2 border-dashed rounded-lg p-4 text-center transition-colors ${
            dragOver
              ? "border-lime-500 bg-lime-950/20"
              : "border-zinc-700 hover:border-zinc-500"
          } ${disabled ? "opacity-50 pointer-events-none" : "cursor-pointer"}`}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
          onClick={() => fileInputRef.current?.click()}
        >
          <p className="text-sm text-zinc-400">
            Drop an audio file here or <span className="text-lime-400 underline">browse</span>
          </p>
          <p className="text-xs text-zinc-600 mt-1">mp3 / wav / aac / ogg · max 20 MB</p>
        </div>
        <input
          ref={fileInputRef}
          type="file"
          accept={ALLOWED_AUDIO_MIME_TYPES.join(",")}
          multiple
          className="hidden"
          onChange={handleFileInput}
        />
        {uploadError && <p className="text-xs text-red-400">{uploadError}</p>}
      </div>

      {/* ── Placement list ── */}
      {placements.length > 0 && (
        <div className="space-y-2">
          <p className="text-xs font-medium text-zinc-400 uppercase tracking-wide">
            Placements ({placements.length})
          </p>
          {placements.map((p, idx) => (
            <div
              key={p.id}
              className="bg-zinc-800/70 border border-zinc-700 rounded-lg p-3 space-y-2"
            >
              {/* Label row */}
              <div className="flex items-center justify-between gap-2">
                <input
                  value={p.label ?? ""}
                  onChange={(e) => updatePlacement(idx, { label: e.target.value })}
                  disabled={disabled}
                  placeholder="Label (optional)"
                  className="flex-1 bg-transparent text-sm text-white placeholder-zinc-500 focus:outline-none"
                />
                <button
                  onClick={() => removePlacement(idx)}
                  disabled={disabled}
                  className="text-zinc-500 hover:text-red-400 text-xs px-1 disabled:opacity-40"
                >
                  Remove
                </button>
              </div>

              {/* At + gain row */}
              <div className="flex gap-3 items-center flex-wrap">
                <label className="flex items-center gap-1.5 text-xs text-zinc-400">
                  At
                  <input
                    type="number"
                    min={0}
                    max={maxAt}
                    step={0.1}
                    value={p.at_s}
                    onChange={(e) =>
                      updatePlacement(idx, {
                        at_s: Math.min(maxAt, Math.max(0, parseFloat(e.target.value) || 0)),
                      })
                    }
                    disabled={disabled}
                    className="w-20 bg-zinc-700 border border-zinc-600 rounded px-2 py-0.5 text-white text-xs disabled:opacity-50"
                  />
                  <span>s</span>
                </label>

                <label className="flex items-center gap-1.5 text-xs text-zinc-400">
                  Vol
                  <input
                    type="range"
                    min={0}
                    max={2}
                    step={0.05}
                    value={p.gain}
                    onChange={(e) => updatePlacement(idx, { gain: parseFloat(e.target.value) })}
                    disabled={disabled}
                    className="w-24 accent-lime-500 disabled:opacity-50"
                  />
                  <span className="w-8 text-right">{p.gain.toFixed(2)}×</span>
                </label>
              </div>

              {/* Trim row (optional, collapsed by default) */}
              {p.duration_s != null && p.duration_s > 0 && (
                <div className="flex gap-3 items-center flex-wrap text-xs text-zinc-500">
                  <span>Trim:</span>
                  <label className="flex items-center gap-1">
                    from
                    <input
                      type="number"
                      min={0}
                      max={p.duration_s}
                      step={0.1}
                      value={p.trim_start_s ?? 0}
                      onChange={(e) =>
                        updatePlacement(idx, {
                          trim_start_s: Math.max(0, parseFloat(e.target.value) || 0),
                        })
                      }
                      disabled={disabled}
                      className="w-16 bg-zinc-700 border border-zinc-600 rounded px-1 py-0.5 text-white disabled:opacity-50"
                    />
                    s
                  </label>
                  <label className="flex items-center gap-1">
                    to
                    <input
                      type="number"
                      min={0}
                      max={p.duration_s}
                      step={0.1}
                      value={p.trim_end_s ?? p.duration_s}
                      onChange={(e) =>
                        updatePlacement(idx, {
                          trim_end_s: Math.min(
                            p.duration_s!,
                            Math.max(0, parseFloat(e.target.value) || 0),
                          ),
                        })
                      }
                      disabled={disabled}
                      className="w-16 bg-zinc-700 border border-zinc-600 rounded px-1 py-0.5 text-white disabled:opacity-50"
                    />
                    s
                  </label>
                </div>
              )}

              {/* Source path (informational) */}
              {p.src_gcs_path && (
                <p className="text-xs text-zinc-600 truncate font-mono">{p.src_gcs_path}</p>
              )}
            </div>
          ))}
        </div>
      )}

    </div>
  );
}
