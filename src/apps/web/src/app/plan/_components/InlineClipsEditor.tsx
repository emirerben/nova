"use client";

/**
 * Inline clips editor — timeline bar view + instant client-side preview.
 *
 * Preview is instant: every drag fires a reducer update which triggers a
 * useEffect that seeks each <video> element to its current inS.  No server
 * round-trip is needed to see the result of a trim or duration change.
 *
 * The stacked-video player (PreviewPlayer) renders one <video> per active
 * slot, all pre-loaded.  When playing, it sequences through them by watching
 * timeupdate; when paused the visible video shows the first frame of its
 * in-point so the "current state" is always on screen.
 *
 * "Apply" still calls editTimeline (which enqueues a server re-render) but
 * only once the user is satisfied — they can iterate indefinitely in the
 * instant-preview loop first.
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
} from "@/lib/generative-api";
import {
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

// ─── Constants ─────────────────────────────────────────────────────────────────

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

const MIN_BAR_PCT = 1.5;

// ─── Drag state ─────────────────────────────────────────────────────────────────

type DragKind = "bar-left" | "bar-right" | "src-left" | "src-right";

interface ActiveDrag {
  key: string;
  kind: DragKind;
  startX: number;
  startInS: number;
  startDurS: number;
  scaleS: number;      // total seconds represented by containerW pixels
  containerW: number;
}

// ─── Helpers ────────────────────────────────────────────────────────────────────

function TimeRuler({ totalS }: { totalS: number }) {
  const step = totalS <= 8 ? 1 : totalS <= 20 ? 2 : 5;
  const ticks: number[] = [];
  for (let t = 0; t <= totalS + 1e-6; t += step) ticks.push(t);
  return (
    <div className="relative h-5 select-none">
      {ticks.map((t) => (
        <div
          key={t}
          className="absolute top-0 flex flex-col items-center"
          style={{ left: `${(t / (totalS || 1)) * 100}%` }}
        >
          <div className="w-px h-2 bg-zinc-600" />
          <span className="text-[9px] text-zinc-500 tabular-nums -translate-x-1/2 leading-none">
            {formatSeconds(t)}
          </span>
        </div>
      ))}
    </div>
  );
}

// ─── Instant client-side preview player ─────────────────────────────────────────
// One <video> per active slot, stacked. Pre-seeks to inS on every render
// (while paused) so any drag is reflected immediately without a server call.

function PreviewPlayer({
  activeSlots,
  windows,
  clips,
}: {
  activeSlots: DraftSlot[];
  windows: ReturnType<typeof slotWindows>;
  clips: TimelineClip[];
}) {
  const videoRefs = useRef<(HTMLVideoElement | null)[]>([]);
  const [currentIdx, setCurrentIdx] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  // Keep refs for use inside event handlers (avoid stale closure)
  const playingRef = useRef(false);
  const idxRef = useRef(0);

  useEffect(() => { playingRef.current = isPlaying; }, [isPlaying]);
  useEffect(() => { idxRef.current = currentIdx; }, [currentIdx]);

  // ── Instant preview: seek each video to its in-point whenever the draft
  // changes (trim drag, duration drag, reorder).  Skipped while playing so
  // we don't interrupt the sequence.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (playingRef.current) return;
    activeSlots.forEach((slot, i) => {
      const v = videoRefs.current[i];
      if (v && Math.abs(v.currentTime - slot.inS) > 0.08) {
        v.currentTime = slot.inS;
      }
    });
  });

  function playFrom(idx: number) {
    if (idx >= activeSlots.length) {
      // Sequence finished — reset to start
      playingRef.current = false;
      setIsPlaying(false);
      setCurrentIdx(0);
      idxRef.current = 0;
      activeSlots.forEach((s, i) => {
        const v = videoRefs.current[i];
        if (v) { v.pause(); v.currentTime = s.inS; }
      });
      return;
    }
    const slot = activeSlots[idx];
    const v = videoRefs.current[idx];
    if (!v) { playFrom(idx + 1); return; }
    v.currentTime = slot.inS;
    setCurrentIdx(idx);
    idxRef.current = idx;
    v.play().catch(() => {});
  }

  function handleTimeUpdate(idx: number) {
    if (!playingRef.current || idxRef.current !== idx) return;
    const slot = activeSlots[idx];
    const win = windows[idx];
    const v = videoRefs.current[idx];
    if (!v || !win) return;
    // Switch to next clip a little before the hard cut to reduce gap
    if (v.currentTime >= slot.inS + win.durationS - 0.04) {
      v.pause();
      playFrom(idx + 1);
    }
  }

  function handlePlayPause() {
    if (isPlaying) {
      videoRefs.current[idxRef.current]?.pause();
      setIsPlaying(false);
      playingRef.current = false;
    } else {
      setIsPlaying(true);
      playingRef.current = true;
      playFrom(idxRef.current);
    }
  }

  const hasUrls = activeSlots.some(
    (s) => !!clips.find((c) => c.clip_index === s.clipIndex)?.signed_url,
  );

  if (!hasUrls) {
    return (
      <div className="flex-shrink-0 flex items-center justify-center rounded-lg bg-zinc-800 text-zinc-600 text-[10px]"
        style={{ width: 64, aspectRatio: "9/16" }}>
        No preview
      </div>
    );
  }

  return (
    <div
      className="relative flex-shrink-0 rounded-lg overflow-hidden bg-black"
      style={{ width: 64, aspectRatio: "9/16" }}
    >
      {activeSlots.map((slot, i) => {
        const clip = clips.find((c) => c.clip_index === slot.clipIndex);
        return (
          <video
            key={slot.key}
            ref={(el) => { videoRefs.current[i] = el; }}
            src={clip?.signed_url ?? undefined}
            className="absolute inset-0 w-full h-full object-cover"
            style={{ opacity: i === currentIdx ? 1 : 0 }}
            muted
            preload="auto"
            playsInline
            onTimeUpdate={() => handleTimeUpdate(i)}
          />
        );
      })}
      {/* Play / pause overlay */}
      <button
        type="button"
        aria-label={isPlaying ? "Pause" : "Play preview"}
        className="absolute inset-0 flex items-center justify-center transition-colors hover:bg-black/20"
        onClick={handlePlayPause}
      >
        {!isPlaying && (
          <span className="rounded-full bg-black/70 px-2 py-1 text-white text-[10px]">
            ▶
          </span>
        )}
      </button>
    </div>
  );
}

// ─── Source-clip trim panel ──────────────────────────────────────────────────────

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
  onSrcLeftDown: (e: React.MouseEvent, cw: number, srcDurS: number) => void;
  onSrcRightDown: (e: React.MouseEvent, cw: number, srcDurS: number) => void;
}) {
  const srcBarRef = useRef<HTMLDivElement>(null);
  const srcDur = clip?.duration_s ?? null;
  const effectiveSrc = srcDur ?? Math.max(slot.inS + windowS + 0.5, 1);
  const inFrac = Math.min(1, slot.inS / effectiveSrc);
  const outFrac = Math.min(1, (slot.inS + windowS) / effectiveSrc);

  return (
    <div className="mt-2 rounded-lg border border-zinc-700/40 bg-zinc-900/60 p-2">
      <p className="text-[10px] text-zinc-500 mb-1.5">
        Clip {slot.clipIndex} source{srcDur ? ` — ${formatSeconds(srcDur)} total` : ""}
      </p>
      <div ref={srcBarRef} className="relative h-6 rounded bg-zinc-800 select-none">
        {/* Unused region (dimmed) */}
        <div className="absolute inset-y-0 left-0 right-0 rounded bg-zinc-700/30" />
        {/* Used window */}
        <div
          className="absolute inset-y-0 bg-lime-600/50 rounded"
          style={{ left: `${inFrac * 100}%`, right: `${(1 - outFrac) * 100}%` }}
        />
        {/* In-point handle (orange) */}
        <div
          title="Drag to set in-point"
          className="absolute inset-y-0 w-3 cursor-ew-resize flex items-center justify-center rounded-l bg-orange-500 hover:bg-orange-400 transition-colors"
          style={{ left: `${inFrac * 100}%`, transform: "translateX(-50%)" }}
          onMouseDown={(e) => {
            const cw = srcBarRef.current?.getBoundingClientRect().width ?? 200;
            onSrcLeftDown(e, cw, effectiveSrc);
          }}
        >
          <div className="w-px h-3 bg-white/70" />
        </div>
        {/* Out-point handle (blue) */}
        <div
          title="Drag to set out-point"
          className="absolute inset-y-0 w-3 cursor-ew-resize flex items-center justify-center rounded-r bg-blue-500 hover:bg-blue-400 transition-colors"
          style={{ left: `${outFrac * 100}%`, transform: "translateX(-50%)" }}
          onMouseDown={(e) => {
            const cw = srcBarRef.current?.getBoundingClientRect().width ?? 200;
            onSrcRightDown(e, cw, effectiveSrc);
          }}
        >
          <div className="w-px h-3 bg-white/70" />
        </div>
      </div>
      <div className="mt-1 flex justify-between text-[10px] tabular-nums text-zinc-500">
        <span>In: {formatSeconds(slot.inS)}</span>
        <span>{formatSeconds(windowS)} used</span>
        <span>Out: {formatSeconds(slot.inS + windowS)}</span>
      </div>
    </div>
  );
}

// ─── Main ───────────────────────────────────────────────────────────────────────

export function InlineClipsEditor({
  ownerId,
  variantId,
  base,
  onRenderEnqueued,
  // Optional controlled mode — when provided, skip internal fetch + state.
  // Use this when a parent (e.g. ClipsLane via useClipTimeline) already owns
  // the data so the header bars and expanded panel share one draft.
  externalState,
  externalDispatch,
  externalClips,
  onReload,
  focusedKey,
}: {
  ownerId: string;
  variantId: string;
  base: TimelineBase;
  onRenderEnqueued: () => void;
  /** Controlled mode: external EditorState. Skip internal useReducer + fetch. */
  externalState?: import("../../generative/timeline-reducer").EditorState;
  /** Controlled mode: external dispatch. */
  externalDispatch?: import("react").Dispatch<import("../../generative/timeline-reducer").EditorAction>;
  /** Controlled mode: external clips list. */
  externalClips?: TimelineClip[];
  /** Controlled mode: called after Apply/Reset so the parent can re-sync. */
  onReload?: () => void;
  /**
   * Plan C fix: when set, auto-selects this slot key so the user sees that
   * clip's SourcePanel immediately on first click (no second click needed).
   * Synced via useEffect so clicking different clips updates the selection.
   */
  focusedKey?: string | null;
}) {
  const isControlled = externalState !== undefined;

  // ── Uncontrolled state (only used when externalState is not provided) ────────
  const [uncontrolledLoadState, setUncontrolledLoadState] = useState<
    "loading" | "error" | "ready"
  >(isControlled ? "ready" : "loading");
  const [uncontrolledClips, setUncontrolledClips] = useState<TimelineClip[]>([]);
  const [uncontrolledState, uncontrolledDispatch] = useReducer(
    timelineReducer,
    EMPTY_EDITOR_STATE,
  );

  // ── Resolve state (controlled or uncontrolled) ────────────────────────────
  const loadState = isControlled ? "ready" : uncontrolledLoadState;
  const clips = isControlled ? (externalClips ?? []) : uncontrolledClips;
  // When isControlled is true, externalState/externalDispatch are guaranteed
  // to be defined (TypeScript can't infer this through the boolean, so we cast).
  const state = (isControlled ? externalState : uncontrolledState) as import("../../generative/timeline-reducer").EditorState;
  const dispatch = (isControlled ? externalDispatch : uncontrolledDispatch) as import("react").Dispatch<import("../../generative/timeline-reducer").EditorAction>;

  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Plan C: when the parent emits a focusedKey (because the user clicked a lane
  // bar), auto-select that clip so its SourcePanel opens without a second click.
  useEffect(() => {
    if (focusedKey !== undefined && focusedKey !== null) {
      setSelectedKey(focusedKey);
    }
  }, [focusedKey]);

  const dragRef = useRef<ActiveDrag | null>(null);
  const outputBarRef = useRef<HTMLDivElement>(null);

  // ── Load (uncontrolled mode only) ────────────────────────────────────────────
  const load = useCallback(async () => {
    if (isControlled) {
      onReload?.();
      return;
    }
    setUncontrolledLoadState("loading");
    try {
      const data = await getTimeline(ownerId, variantId, base);
      setUncontrolledClips(data.clips);
      uncontrolledDispatch({ type: "RESET_DRAFT", timeline: data });
      setUncontrolledLoadState("ready");
    } catch {
      setUncontrolledLoadState("error");
    }
  }, [isControlled, ownerId, variantId, base, onReload]);

  useEffect(() => { if (!isControlled) void load(); }, [load, isControlled]);

  // ── Derived ─────────────────────────────────────────────────────────────────
  const windows = useMemo(
    () => slotWindows(state.slots, state.grid),
    [state.slots, state.grid],
  );
  const totalS = useMemo(
    () => totalDurationS(state.slots, state.grid),
    [state.slots, state.grid],
  );
  const activeSlots = useMemo(
    () => state.slots.filter((s) => !s.removed),
    [state.slots],
  );
  const activeWindows = useMemo(
    () => activeSlots.map((s) => windows[state.slots.indexOf(s)]),
    [activeSlots, state.slots, windows],
  );
  const edits = useMemo(
    () => countEdits(state.baseline, state.slots),
    [state.baseline, state.slots],
  );

  function clipFor(slot: DraftSlot) {
    return clips.find((c) => c.clip_index === slot.clipIndex);
  }

  // ── Global drag ─────────────────────────────────────────────────────────────
  useEffect(() => {
    function onMove(e: MouseEvent) {
      const d = dragRef.current;
      if (!d) return;
      const delta = ((e.clientX - d.startX) / d.containerW) * d.scaleS;

      if (d.kind === "bar-left" || d.kind === "src-left") {
        dispatch({
          type: "SET_IN",
          key: d.key,
          inS: Math.max(0, d.startInS + delta),
          record: false,
        });
      } else {
        // bar-right / src-right → change duration
        dispatch({
          type: "SET_WINDOW",
          key: d.key,
          inS: d.startInS,
          durationS: Math.max(0.3, d.startDurS + delta),
          record: false,
        });
      }
    }

    function onUp() {
      const d = dragRef.current;
      if (!d) return;
      const slot = state.slots.find((s) => s.key === d.key);
      if (slot) {
        if (d.kind === "bar-left" || d.kind === "src-left") {
          dispatch({ type: "SET_IN", key: d.key, inS: slot.inS, record: true });
        } else {
          dispatch({
            type: "SET_WINDOW",
            key: d.key,
            inS: slot.inS,
            durationS: slot.durationS ?? d.startDurS,
            record: true,
          });
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

  // ── Apply / Reset ────────────────────────────────────────────────────────────
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
      // In controlled mode, signal parent to re-fetch so header bars also update.
      onReload?.();
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
      onReload?.();
    } catch (err) {
      setSubmitError(
        err instanceof TimelineApiError ? err.message : "Reset failed",
      );
    } finally {
      setSubmitting(false);
    }
  }

  // ── States ───────────────────────────────────────────────────────────────────
  if (loadState === "loading") {
    return <div className="py-4 text-center text-xs text-zinc-500">Loading clips…</div>;
  }
  if (loadState === "error") {
    return (
      <div className="py-3 text-center">
        <p className="text-xs text-red-400">Failed to load clips</p>
        <button className="mt-1 text-xs text-zinc-400 underline" onClick={load}>
          Retry
        </button>
      </div>
    );
  }

  const selectedSlot = selectedKey
    ? state.slots.find((s) => s.key === selectedKey && !s.removed) ?? null
    : null;

  // ── Render ───────────────────────────────────────────────────────────────────
  return (
    <div className="space-y-3 text-sm">

      {/* ── Preview + timeline side-by-side ─────────────────────────────────── */}
      <div className="flex gap-3 items-start">

        {/* Instant preview player */}
        <PreviewPlayer
          activeSlots={activeSlots}
          windows={activeWindows}
          clips={clips}
        />

        {/* Output timeline */}
        <div className="flex-1 min-w-0">
          <TimeRuler totalS={totalS || 1} />
          <div
            ref={outputBarRef}
            className="relative mt-1 h-10 rounded bg-zinc-800/60 select-none"
          >
            {state.slots.map((slot, i) => {
              if (slot.removed) return null;
              const win = windows[i];
              if (!win || win.durationS <= 0) return null;

              const leftPct = ((win.startS ?? 0) / (totalS || 1)) * 100;
              const widthPct = Math.max(
                (win.durationS / (totalS || 1)) * 100,
                MIN_BAR_PCT,
              );
              const sel = selectedKey === slot.key;

              return (
                <div
                  key={slot.key}
                  className="absolute inset-y-1 flex"
                  style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                >
                  {/* Left handle — trim in-point */}
                  <div
                    title="Drag: set in-point (preview updates instantly)"
                    className="flex-shrink-0 w-2.5 cursor-ew-resize rounded-l flex items-center justify-center bg-orange-500/70 hover:bg-orange-400 transition-colors"
                    onMouseDown={(e) => {
                      e.preventDefault();
                      const cw =
                        outputBarRef.current?.getBoundingClientRect().width ?? 300;
                      dragRef.current = {
                        key: slot.key,
                        kind: "bar-left",
                        startX: e.clientX,
                        startInS: slot.inS,
                        startDurS: slot.durationS ?? win.durationS,
                        containerW: cw,
                        scaleS: totalS || 1,
                      };
                    }}
                  >
                    <div className="w-px h-3.5 bg-white/70" />
                  </div>

                  {/* Bar body */}
                  <button
                    type="button"
                    className={`flex-1 min-w-0 flex items-center justify-center gap-1 text-[10px] font-medium truncate transition-colors ${
                      sel
                        ? "bg-lime-500/80 text-black"
                        : "bg-blue-600/70 text-white hover:bg-blue-500/80"
                    }`}
                    title={`Clip ${slot.clipIndex} · ${formatSeconds(win.durationS)}`}
                    onClick={() =>
                      setSelectedKey(sel ? null : slot.key)
                    }
                  >
                    <span className="truncate">C{slot.clipIndex}</span>
                    <span className="opacity-70 shrink-0">
                      {formatSeconds(win.durationS)}
                    </span>
                  </button>

                  {/* Right handle — extend / shrink */}
                  <div
                    title="Drag: extend or shorten clip"
                    className="flex-shrink-0 w-2.5 cursor-ew-resize rounded-r flex items-center justify-center bg-blue-500/70 hover:bg-blue-400 transition-colors"
                    onMouseDown={(e) => {
                      e.preventDefault();
                      const cw =
                        outputBarRef.current?.getBoundingClientRect().width ?? 300;
                      dragRef.current = {
                        key: slot.key,
                        kind: "bar-right",
                        startX: e.clientX,
                        startInS: slot.inS,
                        startDurS: slot.durationS ?? win.durationS,
                        containerW: cw,
                        scaleS: totalS || 1,
                      };
                    }}
                  >
                    <div className="w-px h-3.5 bg-white/70" />
                  </div>
                </div>
              );
            })}
          </div>
          <p className="mt-1 text-[10px] text-zinc-600">
            <span className="text-orange-400/80">▌</span> trim in
            {"  "}
            <span className="text-blue-400/80">▐</span> extend/shrink
            {"  ·  "}click bar for source trim
          </p>
        </div>
      </div>

      {/* ── Source clip trim panel (when bar selected) ───────────────────────── */}
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
              };
            }}
          />
        );
      })()}

      {/* ── Slot list (reorder + remove) ─────────────────────────────────────── */}
      <div className="space-y-0.5">
        {activeSlots.map((slot) => {
          const allIdx = state.slots.indexOf(slot);
          const sel = selectedKey === slot.key;
          return (
            <div
              key={slot.key}
              className={`flex items-center gap-2 rounded px-2 py-1 text-xs transition-colors ${
                sel ? "bg-lime-900/30" : "bg-zinc-800/40 hover:bg-zinc-800/60"
              }`}
            >
              <button
                type="button"
                className="flex-1 text-left text-zinc-300 truncate"
                onClick={() => setSelectedKey(sel ? null : slot.key)}
              >
                Clip {slot.clipIndex}
              </button>
              <span className="tabular-nums text-zinc-500 shrink-0">
                {formatSeconds(windows[allIdx]?.durationS ?? 0)}
              </span>
              <button
                type="button"
                aria-label="Move up"
                disabled={allIdx === 0}
                className="text-zinc-500 hover:text-zinc-200 disabled:opacity-30 shrink-0"
                onClick={() =>
                  dispatch({ type: "REORDER", from: allIdx, to: allIdx - 1 })
                }
              >
                ↑
              </button>
              <button
                type="button"
                aria-label="Move down"
                disabled={allIdx === state.slots.length - 1}
                className="text-zinc-500 hover:text-zinc-200 disabled:opacity-30 shrink-0"
                onClick={() =>
                  dispatch({ type: "REORDER", from: allIdx, to: allIdx + 1 })
                }
              >
                ↓
              </button>
              <button
                type="button"
                aria-label="Remove"
                className="text-zinc-600 hover:text-red-400 shrink-0"
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
      {submitError && <p className="text-xs text-red-400">{submitError}</p>}
      <div className="flex flex-wrap items-center gap-2 border-t border-zinc-700/40 pt-2">
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
          Reset
        </button>
        <div className="flex-1" />
        <span className="text-[10px] text-zinc-600 tabular-nums">
          {totalS > 0 && formatSeconds(totalS)}
          {edits > 0 && ` · ${edits} edit${edits > 1 ? "s" : ""}`}
        </span>
        <button
          type="button"
          disabled={submitting || edits === 0}
          className="rounded-lg bg-lime-600 px-3 py-1.5 text-xs font-medium text-black hover:bg-lime-500 disabled:opacity-50 transition-colors"
          onClick={handleApply}
        >
          {submitting ? "Saving…" : "Apply & Re-render"}
        </button>
      </div>
    </div>
  );
}
