"use client";

/**
 * Inline clips editor — timeline bar view.
 *
 * Each clip is shown as a proportional bar on a shared output timeline.
 * • Left handle on a bar  → trim in-point (scrubs source video live)
 * • Right handle on a bar → extend / shrink clip duration in output
 * • Click bar body        → expand source-clip panel with precise in/out handles
 *                           + mini video preview that seeks to the in-frame instantly
 *
 * All edits stay local (reducer draft) until the user clicks Apply.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";
import {
  editTimeline,
  getTimeline,
  resetTimeline,
  TimelineApiError,
  type TimelineBase,
  type TimelineClip,
  type TimelineResponse,
} from "@/lib/generative-api";
import {
  clampInPoint,
  countEdits,
  formatSeconds,
  slotWindows,
  totalDurationS,
  type DraftSlot,
} from "../../generative/timeline-math";
import {
  initEditorState,
  timelineReducer,
  type EditorState,
} from "../../generative/timeline-reducer";

const EMPTY_EDITOR_STATE: EditorState = {
  grid: [],
  clipDurations: {},
  baseline: [],
  slots: [],
  past: [],
  future: [],
  clampNonce: 0,
  clampedKey: null,
};

const MIN_BAR_PCT = 1.5; // minimum rendered bar width so tiny clips are still clickable

// ─── Drag helpers ──────────────────────────────────────────────────────────────

type DragKind =
  | "bar-left"   // output bar left handle  → changes inS
  | "bar-right"  // output bar right handle → changes durationS
  | "src-left"   // source timeline left handle → inS
  | "src-right"; // source timeline right handle → out-point (inS + durationS)

interface DragState {
  key: string;
  kind: DragKind;
  startX: number;
  startInS: number;
  startDurS: number;
  containerW: number;
  scaleS: number; // seconds per pixel for this handle's container
  srcDurS: number | null;
}

// ─── Timeline ruler ────────────────────────────────────────────────────────────

function TimeRuler({ totalS }: { totalS: number }) {
  const ticks: number[] = [];
  const step = totalS <= 10 ? 1 : totalS <= 30 ? 2 : 5;
  for (let t = 0; t <= totalS; t += step) ticks.push(t);
  return (
    <div className="relative h-5 border-b border-zinc-700/50 select-none">
      {ticks.map((t) => (
        <div
          key={t}
          className="absolute top-0 flex flex-col items-center"
          style={{ left: `${(t / totalS) * 100}%` }}
        >
          <div className="w-px h-2 bg-zinc-600" />
          <span className="text-[9px] text-zinc-500 tabular-nums -translate-x-1/2">
            {formatSeconds(t)}
          </span>
        </div>
      ))}
    </div>
  );
}

// ─── Source clip preview panel ─────────────────────────────────────────────────

function SourcePanel({
  slot,
  clip,
  windowS,
  onSrcLeftDown,
  onSrcRightDown,
}: {
  slot: DraftSlot;
  clip: TimelineClip | undefined;
  windowS: number;
  onSrcLeftDown: (e: React.MouseEvent, containerW: number, srcDurS: number) => void;
  onSrcRightDown: (e: React.MouseEvent, containerW: number, srcDurS: number) => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const srcBarRef = useRef<HTMLDivElement>(null);
  const srcDur = clip?.duration_s ?? null;
  const url = clip?.signed_url ?? null;

  // Seek video to in-point whenever inS changes — instant preview, no re-render
  useEffect(() => {
    const v = videoRef.current;
    if (!v || !url) return;
    if (Math.abs(v.currentTime - slot.inS) > 0.05) {
      v.currentTime = slot.inS;
    }
  }, [slot.inS, url]);

  function handlePreviewClick(e: React.MouseEvent) {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) {
      v.currentTime = slot.inS;
      v.play();
    } else {
      v.pause();
    }
  }

  function handleTimeUpdate() {
    const v = videoRef.current;
    if (!v) return;
    if (v.currentTime >= slot.inS + windowS) {
      v.pause();
      v.currentTime = slot.inS;
    }
  }

  const inFrac = srcDur ? Math.min(1, slot.inS / srcDur) : 0;
  const outFrac = srcDur ? Math.min(1, (slot.inS + windowS) / srcDur) : 1;

  return (
    <div className="mt-2 rounded-lg border border-zinc-700/50 bg-zinc-900/60 p-3">
      <div className="flex gap-3">
        {/* Mini video preview */}
        {url ? (
          <div className="relative flex-shrink-0 cursor-pointer" onClick={handlePreviewClick}>
            <video
              ref={videoRef}
              src={url}
              className="h-24 w-16 rounded object-cover bg-black"
              muted
              preload="metadata"
              onTimeUpdate={handleTimeUpdate}
            />
            <div className="absolute inset-0 flex items-center justify-center opacity-0 hover:opacity-100 transition-opacity">
              <div className="rounded-full bg-black/60 p-1 text-white text-xs">▶</div>
            </div>
          </div>
        ) : (
          <div className="flex-shrink-0 h-24 w-16 rounded bg-zinc-800 flex items-center justify-center text-zinc-600 text-xs">
            No preview
          </div>
        )}

        {/* Source clip timeline */}
        <div className="flex-1 min-w-0">
          <p className="text-[10px] text-zinc-500 mb-1">
            Source clip{srcDur ? ` — ${formatSeconds(srcDur)} total` : ""}
          </p>

          {/* Source timeline bar */}
          <div
            ref={srcBarRef}
            className="relative h-7 rounded bg-zinc-800 select-none"
          >
            {/* Used region */}
            <div
              className="absolute inset-y-0 bg-lime-600/40 rounded"
              style={{ left: `${inFrac * 100}%`, right: `${(1 - outFrac) * 100}%` }}
            />
            {/* Left handle (in-point) */}
            <div
              className="absolute inset-y-0 w-3 cursor-ew-resize flex items-center justify-center rounded-l bg-orange-500/80 hover:bg-orange-400 transition-colors"
              style={{ left: `${inFrac * 100}%`, transform: "translateX(-50%)" }}
              title="Drag to set in-point"
              onMouseDown={(e) => {
                const w = srcBarRef.current?.getBoundingClientRect().width ?? 200;
                const s = srcDur ?? (slot.inS + windowS + 1);
                onSrcLeftDown(e, w, s);
              }}
            >
              <div className="w-px h-3 bg-white/80" />
            </div>
            {/* Right handle (out-point) */}
            <div
              className="absolute inset-y-0 w-3 cursor-ew-resize flex items-center justify-center rounded-r bg-blue-500/80 hover:bg-blue-400 transition-colors"
              style={{ left: `${outFrac * 100}%`, transform: "translateX(-50%)" }}
              title="Drag to set out-point"
              onMouseDown={(e) => {
                const w = srcBarRef.current?.getBoundingClientRect().width ?? 200;
                const s = srcDur ?? (slot.inS + windowS + 1);
                onSrcRightDown(e, w, s);
              }}
            >
              <div className="w-px h-3 bg-white/80" />
            </div>
          </div>

          {/* Labels */}
          <div className="mt-1 flex justify-between text-[10px] tabular-nums text-zinc-400">
            <span>In: {formatSeconds(slot.inS)}</span>
            <span>Duration: {formatSeconds(windowS)}</span>
            <span>Out: {formatSeconds(slot.inS + windowS)}</span>
          </div>
          {url && (
            <p className="mt-1 text-[10px] text-zinc-600">Click video to preview · handles set in/out</p>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Main component ────────────────────────────────────────────────────────────

export function InlineClipsEditor({
  ownerId,
  variantId,
  base,
  onRenderEnqueued,
}: {
  ownerId: string;
  variantId: string;
  base: TimelineBase;
  onRenderEnqueued: () => void;
}) {
  const [loadState, setLoadState] = useState<"loading" | "error" | "ready">("loading");
  const [clips, setClips] = useState<TimelineClip[]>([]);
  const [state, dispatch] = useReducer(timelineReducer, EMPTY_EDITOR_STATE);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const dragRef = useRef<DragState | null>(null);
  const outputBarRef = useRef<HTMLDivElement>(null);

  // ── Load ──────────────────────────────────────────────────────────────────────
  const load = useCallback(async () => {
    setLoadState("loading");
    try {
      const data = await getTimeline(ownerId, variantId, base);
      setClips(data.clips);
      dispatch({ type: "RESET_DRAFT", timeline: data });
      setLoadState("ready");
    } catch {
      setLoadState("error");
    }
  }, [ownerId, variantId, base]);

  useEffect(() => { load(); }, [load]);

  // ── Derived ───────────────────────────────────────────────────────────────────
  const activeSlots = useMemo(
    () => state.slots.filter((s) => !s.removed),
    [state.slots],
  );
  const windows = useMemo(
    () => slotWindows(state.slots, state.grid),
    [state.slots, state.grid],
  );
  const totalS = useMemo(
    () => totalDurationS(state.slots, state.grid),
    [state.slots, state.grid],
  );
  const edits = useMemo(
    () => countEdits(state.baseline, state.slots),
    [state.baseline, state.slots],
  );

  function clipFor(slot: DraftSlot): TimelineClip | undefined {
    return clips.find((c) => c.clip_index === slot.clipIndex);
  }

  // ── Global drag handlers ──────────────────────────────────────────────────────
  useEffect(() => {
    function onMove(e: MouseEvent) {
      const d = dragRef.current;
      if (!d) return;
      const delta = ((e.clientX - d.startX) / d.containerW) * d.scaleS;

      if (d.kind === "bar-left") {
        // Trim in-point: shift source window start, keep output duration
        const newIn = Math.max(0, d.startInS + delta);
        dispatch({ type: "SET_IN", key: d.key, inS: newIn, record: false });
      } else if (d.kind === "bar-right") {
        // Extend/shrink output duration
        const newDur = Math.max(0.3, d.startDurS + delta);
        dispatch({ type: "SET_WINDOW", key: d.key, inS: d.startInS, durationS: newDur, record: false });
      } else if (d.kind === "src-left") {
        // Source timeline in-point handle
        const newIn = Math.max(0, d.startInS + delta);
        dispatch({ type: "SET_IN", key: d.key, inS: newIn, record: false });
      } else if (d.kind === "src-right") {
        // Source timeline out-point handle (inS fixed, change durationS)
        const newDur = Math.max(0.3, d.startDurS + delta);
        dispatch({ type: "SET_WINDOW", key: d.key, inS: d.startInS, durationS: newDur, record: false });
      }
    }

    function onUp() {
      const d = dragRef.current;
      if (!d) return;
      // Commit to history on mouse-up
      const slot = state.slots.find((s) => s.key === d.key);
      if (slot) {
        if (d.kind === "bar-left" || d.kind === "src-left") {
          dispatch({ type: "SET_IN", key: d.key, inS: slot.inS, record: true });
        } else {
          dispatch({ type: "SET_WINDOW", key: d.key, inS: slot.inS, durationS: slot.durationS ?? d.startDurS, record: true });
        }
      }
      dragRef.current = null;
    }

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [state.slots]);

  // ── Apply / Reset ─────────────────────────────────────────────────────────────
  async function handleApply() {
    setSubmitting(true);
    setSubmitError(null);
    try {
      const payload = state.slots.map((s) => ({
        slot_id: s.slotId,
        clip_index: s.clipIndex,
        in_s: s.inS,
        duration_beats: s.durationBeats,
        duration_s: s.durationS,
        removed: s.removed,
      }));
      await editTimeline(ownerId, variantId, payload, base);
      onRenderEnqueued();
    } catch (err) {
      setSubmitError(
        err instanceof TimelineApiError ? err.message : "Save failed",
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function handleReset() {
    setSubmitting(true);
    setSubmitError(null);
    try {
      await resetTimeline(ownerId, variantId, base);
      onRenderEnqueued();
    } catch (err) {
      setSubmitError(
        err instanceof TimelineApiError ? err.message : "Reset failed",
      );
    } finally {
      setSubmitting(false);
    }
  }

  // ── Loading / error ───────────────────────────────────────────────────────────
  if (loadState === "loading") {
    return (
      <div className="py-4 text-center text-xs text-zinc-500">Loading clips…</div>
    );
  }
  if (loadState === "error") {
    return (
      <div className="py-3 text-center">
        <p className="text-xs text-red-400">Failed to load clips</p>
        <button
          className="mt-1 text-xs text-zinc-400 underline"
          onClick={load}
        >
          Retry
        </button>
      </div>
    );
  }

  const selectedSlot = selectedKey
    ? state.slots.find((s) => s.key === selectedKey && !s.removed) ?? null
    : null;

  // ── Render ────────────────────────────────────────────────────────────────────
  return (
    <div className="space-y-3 text-sm">

      {/* ── Output timeline ─────────────────────────────────────────────────── */}
      <div>
        <TimeRuler totalS={totalS || 1} />
        <div
          ref={outputBarRef}
          className="relative mt-1 h-10 rounded-md bg-zinc-800/60 select-none"
          aria-label="Clip output timeline"
        >
          {state.slots.map((slot, i) => {
            if (slot.removed) return null;
            const win = windows[i];
            if (!win || win.durationS <= 0) return null;

            const leftPct = ((win.startS ?? 0) / (totalS || 1)) * 100;
            const rawWidth = (win.durationS / (totalS || 1)) * 100;
            const widthPct = Math.max(rawWidth, MIN_BAR_PCT);
            const isSelected = selectedKey === slot.key;
            const clip = clipFor(slot);
            const label = `C${slot.clipIndex}`;

            return (
              <div
                key={slot.key}
                className="absolute inset-y-1 flex"
                style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
              >
                {/* Left handle — trim in-point */}
                <div
                  title="Drag to trim in-point"
                  className="flex-shrink-0 w-2.5 cursor-ew-resize rounded-l flex items-center justify-center hover:bg-orange-400/80 bg-orange-500/60 transition-colors"
                  onMouseDown={(e) => {
                    e.preventDefault();
                    const cw = outputBarRef.current?.getBoundingClientRect().width ?? 300;
                    dragRef.current = {
                      key: slot.key,
                      kind: "bar-left",
                      startX: e.clientX,
                      startInS: slot.inS,
                      startDurS: slot.durationS ?? win.durationS,
                      containerW: cw,
                      scaleS: totalS || 1,
                      srcDurS: clip?.duration_s ?? null,
                    };
                  }}
                >
                  <div className="w-px h-4 bg-white/60" />
                </div>

                {/* Bar body — click to select */}
                <button
                  type="button"
                  className={`flex-1 min-w-0 flex items-center justify-center gap-1 truncate text-[10px] font-medium transition-colors ${
                    isSelected
                      ? "bg-lime-500/80 text-black"
                      : "bg-blue-600/70 text-white hover:bg-blue-500/80"
                  }`}
                  onClick={() => setSelectedKey(isSelected ? null : slot.key)}
                  title={`Clip ${slot.clipIndex} · ${formatSeconds(win.durationS)}`}
                >
                  <span className="truncate">{label}</span>
                  <span className="opacity-70">{formatSeconds(win.durationS)}</span>
                </button>

                {/* Right handle — extend / shrink duration */}
                <div
                  title="Drag to extend or shorten"
                  className="flex-shrink-0 w-2.5 cursor-ew-resize rounded-r flex items-center justify-center hover:bg-blue-400/80 bg-blue-500/60 transition-colors"
                  onMouseDown={(e) => {
                    e.preventDefault();
                    const cw = outputBarRef.current?.getBoundingClientRect().width ?? 300;
                    dragRef.current = {
                      key: slot.key,
                      kind: "bar-right",
                      startX: e.clientX,
                      startInS: slot.inS,
                      startDurS: slot.durationS ?? win.durationS,
                      containerW: cw,
                      scaleS: totalS || 1,
                      srcDurS: clip?.duration_s ?? null,
                    };
                  }}
                >
                  <div className="w-px h-4 bg-white/60" />
                </div>
              </div>
            );
          })}
        </div>
        <p className="mt-1 text-[10px] text-zinc-600">
          Orange handle = trim in-point · Blue handle = extend/shrink · Click bar to edit
        </p>
      </div>

      {/* ── Selected clip panel ─────────────────────────────────────────────── */}
      {selectedSlot && (() => {
        const idx = state.slots.indexOf(selectedSlot);
        const win = windows[idx];
        return (
          <SourcePanel
            slot={selectedSlot}
            clip={clipFor(selectedSlot)}
            windowS={win?.durationS ?? 0}
            onSrcLeftDown={(e, cw, srcDurS) => {
              e.preventDefault();
              dragRef.current = {
                key: selectedSlot.key,
                kind: "src-left",
                startX: e.clientX,
                startInS: selectedSlot.inS,
                startDurS: selectedSlot.durationS ?? (win?.durationS ?? 1),
                containerW: cw,
                scaleS: srcDurS,
                srcDurS,
              };
            }}
            onSrcRightDown={(e, cw, srcDurS) => {
              e.preventDefault();
              dragRef.current = {
                key: selectedSlot.key,
                kind: "src-right",
                startX: e.clientX,
                startInS: selectedSlot.inS,
                startDurS: selectedSlot.durationS ?? (win?.durationS ?? 1),
                containerW: cw,
                scaleS: srcDurS,
                srcDurS,
              };
            }}
          />
        );
      })()}

      {/* ── Slot list (reorder + remove) ────────────────────────────────────── */}
      <div className="space-y-1">
        {activeSlots.map((slot) => {
          const allIdx = state.slots.indexOf(slot);
          const isSelected = selectedKey === slot.key;
          return (
            <div
              key={slot.key}
              className={`flex items-center gap-2 rounded px-2 py-1 text-xs transition-colors ${
                isSelected ? "bg-lime-900/30" : "bg-zinc-800/40 hover:bg-zinc-800/60"
              }`}
            >
              <button
                type="button"
                className="flex-1 text-left text-zinc-300"
                onClick={() => setSelectedKey(isSelected ? null : slot.key)}
              >
                Clip {slot.clipIndex}
              </button>
              <span className="tabular-nums text-zinc-500">
                {formatSeconds(windows[allIdx]?.durationS ?? 0)}
              </span>
              {/* Reorder */}
              <button
                type="button"
                aria-label="Move up"
                disabled={allIdx === 0}
                className="text-zinc-500 hover:text-zinc-200 disabled:opacity-30"
                onClick={() => dispatch({ type: "REORDER", from: allIdx, to: allIdx - 1 })}
              >
                ↑
              </button>
              <button
                type="button"
                aria-label="Move down"
                disabled={allIdx === state.slots.length - 1}
                className="text-zinc-500 hover:text-zinc-200 disabled:opacity-30"
                onClick={() => dispatch({ type: "REORDER", from: allIdx, to: allIdx + 1 })}
              >
                ↓
              </button>
              {/* Remove */}
              <button
                type="button"
                aria-label="Remove clip"
                className="text-zinc-600 hover:text-red-400"
                onClick={() => {
                  dispatch({ type: "REMOVE", key: slot.key });
                  if (selectedKey === slot.key) setSelectedKey(null);
                }}
              >
                ×
              </button>
            </div>
          );
        })}
      </div>

      {/* ── Controls ─────────────────────────────────────────────────────────── */}
      {submitError && (
        <p className="text-xs text-red-400">{submitError}</p>
      )}
      <div className="flex flex-wrap items-center gap-2 pt-1 border-t border-zinc-700/40">
        <button
          type="button"
          disabled={state.past.length === 0}
          className="rounded px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200 disabled:opacity-30"
          onClick={() => dispatch({ type: "UNDO" })}
        >
          ↩ Undo
        </button>
        <button
          type="button"
          disabled={state.future.length === 0}
          className="rounded px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200 disabled:opacity-30"
          onClick={() => dispatch({ type: "REDO" })}
        >
          ↪ Redo
        </button>
        <button
          type="button"
          disabled={submitting}
          className="rounded px-2 py-1 text-xs text-zinc-500 hover:text-red-400 disabled:opacity-50"
          onClick={handleReset}
        >
          Reset to AI cut
        </button>
        <div className="flex-1" />
        <span className="text-[10px] text-zinc-600 tabular-nums">
          {totalS > 0 ? formatSeconds(totalS) : ""}
          {edits > 0 ? ` · ${edits} edit${edits > 1 ? "s" : ""}` : ""}
        </span>
        <button
          type="button"
          disabled={submitting || edits === 0}
          className="rounded-lg bg-lime-600 px-3 py-1.5 text-xs font-medium text-black hover:bg-lime-500 disabled:opacity-50 transition-colors"
          onClick={handleApply}
        >
          {submitting ? "Saving…" : "Apply"}
        </button>
      </div>
    </div>
  );
}
