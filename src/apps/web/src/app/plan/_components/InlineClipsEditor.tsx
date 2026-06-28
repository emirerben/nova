"use client";

/**
 * Inline clips editor for the UnifiedTimeline Clips lane.
 *
 * Self-contained: fetches the timeline, manages the reducer draft, and submits
 * via editTimeline / resetTimeline. Designed to render inside the Clips lane's
 * expandable panel (same pattern as the Text lane's inline textPanel).
 *
 * Covers the same core edit operations as the retired full-screen TimelineEditor
 * sheet: reorder, in-point, duration nudge, reset, undo/redo, and apply.
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
  type TimelineResponse,
} from "@/lib/generative-api";
import {
  clampInPoint,
  countEdits,
  formatInPoint,
  formatSeconds,
  formatTimecode,
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

const HANDLE_PX = 12;

// ── Inline in-point scrubber (ported from TimelineEditor) ────────────────────

function InPointScrubber({
  inS,
  windowS,
  sourceDurationS,
  grid,
  offsetBeats,
  durationBeats,
  isSeconds,
  onChange,
}: {
  inS: number;
  windowS: number;
  sourceDurationS: number | null;
  grid: number[];
  offsetBeats: number | null;
  durationBeats: number | null;
  isSeconds: boolean;
  onChange: (inS: number, record: boolean) => void;
}) {
  const stripRef = useRef<HTMLDivElement>(null);
  const recordedRef = useRef(false);
  const src = sourceDurationS ?? Math.max(inS + windowS, 1);
  const maxIn = Math.max(0, src - windowS);

  const posToIn = useCallback(
    (clientX: number) => {
      const el = stripRef.current;
      if (!el) return inS;
      const r = el.getBoundingClientRect();
      const frac = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
      return Math.min(maxIn, frac * src);
    },
    [inS, src, maxIn],
  );

  const handlePointer = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault();
      const record = !recordedRef.current;
      recordedRef.current = true;
      onChange(posToIn(e.clientX), record);
      const onMove = (ev: PointerEvent) => onChange(posToIn(ev.clientX), false);
      const onUp = () => {
        recordedRef.current = false;
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },
    [posToIn, onChange],
  );

  const ticks: number[] = [];
  if (grid.length > 0 && offsetBeats != null && durationBeats != null) {
    const base = grid[Math.min(offsetBeats, grid.length - 1)];
    for (let k = 1; k < durationBeats; k++) {
      const t = grid[Math.min(offsetBeats + k, grid.length - 1)] - base;
      ticks.push(inS + t);
    }
  }

  return (
    <div>
      <div
        ref={stripRef}
        role="slider"
        tabIndex={0}
        aria-label="Clip in-point"
        aria-valuemin={0}
        aria-valuemax={Math.round(maxIn * 10) / 10}
        aria-valuenow={Math.round(inS * 10) / 10}
        aria-valuetext={`Starts at ${formatInPoint(inS)}`}
        onPointerDown={handlePointer}
        onKeyDown={(e) => {
          if (e.key === "ArrowLeft") onChange(Math.max(0, inS - 0.1), true);
          if (e.key === "ArrowRight") onChange(Math.min(maxIn, inS + 0.1), true);
        }}
        className="relative h-9 w-full cursor-ew-resize touch-none rounded-md bg-zinc-700/40"
      >
        <div
          className="absolute inset-y-1 rounded"
          style={{
            left: `${(inS / src) * 100}%`,
            width: `${Math.min(100, (windowS / src) * 100)}%`,
            background: "rgba(163,230,53,0.7)",
          }}
        >
          <div
            className="absolute inset-y-0 left-0 rounded-l"
            style={{ width: HANDLE_PX, background: "rgba(132,204,22,0.9)" }}
            aria-hidden="true"
          />
          <div
            className="absolute inset-y-0 right-0 rounded-r"
            style={{ width: HANDLE_PX, background: "rgba(132,204,22,0.9)" }}
            aria-hidden="true"
          />
        </div>
        {ticks.map((t, i) => (
          <div
            key={i}
            className="absolute inset-y-2 w-px bg-lime-400/60"
            style={{ left: `${(t / src) * 100}%` }}
            aria-hidden="true"
          />
        ))}
      </div>
      <p className="mt-0.5 text-[10px] tabular-nums text-zinc-500">
        Starts at {formatInPoint(inS)}
      </p>
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────

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
  const [timelineData, setTimelineData] = useState<TimelineResponse | null>(null);
  const [state, dispatch] = useReducer(timelineReducer, EMPTY_EDITOR_STATE);
  const [initialized, setInitialized] = useState(false);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadState("loading");
    try {
      const data = await getTimeline(ownerId, variantId, base);
      setTimelineData(data);
      dispatch({ type: "RESET_DRAFT", timeline: data });
      setInitialized(true);
      setLoadState("ready");
    } catch {
      setLoadState("error");
    }
  }, [ownerId, variantId, base]);

  useEffect(() => { void load(); }, [load]);

  const windows = useMemo(
    () => (initialized ? slotWindows(state.slots, state.grid) : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [initialized, state?.slots, state?.grid],
  );

  const edits = useMemo(
    () => (initialized ? countEdits(state.baseline, state.slots) : 0),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [initialized, state?.baseline, state?.slots],
  );

  const totalS = useMemo(
    () => (initialized ? totalDurationS(state.slots, state.grid) : 0),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [initialized, state?.slots, state?.grid],
  );

  const dirty = edits > 0;
  const liveSlots = initialized ? state.slots.filter((s: DraftSlot) => !s.removed) : [];

  async function handleApply() {
    if (!initialized || !dirty || liveSlots.length === 0) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const payload = state.slots.map((s: DraftSlot) => ({
        slot_id: s.slotId,
        clip_index: s.clipIndex,
        in_s: s.inS,
        duration_beats: s.durationBeats,
        duration_s: s.durationS,
        removed: s.removed,
      }));
      await editTimeline(ownerId, variantId, payload, base);
      onRenderEnqueued();
      const fresh = await getTimeline(ownerId, variantId, base);
      setTimelineData(fresh);
      dispatch({ type: "RESET_DRAFT", timeline: fresh });
    } catch (err) {
      if (err instanceof TimelineApiError) {
        setSubmitError(`Submit failed (${err.status})`);
      } else {
        setSubmitError("Submit failed — try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  async function handleReset() {
    if (!initialized) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      await resetTimeline(ownerId, variantId, base);
      onRenderEnqueued();
      const fresh = await getTimeline(ownerId, variantId, base);
      setTimelineData(fresh);
      dispatch({ type: "RESET_DRAFT", timeline: fresh });
    } catch {
      setSubmitError("Reset failed — try again.");
    } finally {
      setSubmitting(false);
    }
  }

  // ── Loading / error / uneditable states ────────────────────────────────────

  if (loadState === "loading") {
    return (
      <div className="space-y-2 py-2">
        {[0, 1, 2].map((i) => (
          <div key={i} className="h-10 rounded-lg bg-zinc-700/20 animate-pulse" />
        ))}
      </div>
    );
  }

  if (loadState === "error") {
    return (
      <div className="flex flex-col items-center gap-2 py-4 text-center">
        <p className="text-xs text-zinc-400">Couldn&apos;t load the cut.</p>
        <button
          type="button"
          onClick={() => void load()}
          className="text-xs text-zinc-300 underline underline-offset-2"
        >
          Retry
        </button>
      </div>
    );
  }

  if (!initialized || !timelineData) return null;

  if (!timelineData.editable) {
    const reasonCopy: Record<string, string> = {
      lyrics_sync: "Clips are beat-synced to lyrics and can't be reordered.",
      voiceover_bed_fit: "Clips are locked to the voiceover timing.",
      sources_expired: "Source clips have expired — re-upload to edit.",
    };
    return (
      <p className="py-3 text-center text-xs text-zinc-400">
        {reasonCopy[timelineData.reason ?? ""] ?? "This variant’s clips can’t be edited."}
      </p>
    );
  }

  // ── Editable slot list ──────────────────────────────────────────────────────

  const canUndo = state.past.length > 0;
  const canRedo = state.future.length > 0;
  const isNoGrid = state.grid.length === 0;
  const ctaLabel = submitting
    ? "Applying…"
    : dirty
    ? `Apply ${edits} edit${edits !== 1 ? "s" : ""} · re-renders ~2 min`
    : `${formatTimecode(totalS)} total`;

  return (
    <div className="space-y-2">
      {/* Slot list */}
      <div className="space-y-1">
        {state.slots.map((slot: DraftSlot, idx: number) => {
          const win = windows[idx];
          const durText = win ? formatSeconds(win.durationS) : "—";
          const isSelected = selectedKey === slot.key;
          const srcDur = state.clipDurations[slot.clipIndex] ?? null;

          return (
            <div
              key={slot.key}
              className={`rounded-lg border transition-colors ${
                slot.removed
                  ? "border-zinc-700/30 bg-zinc-800/20 opacity-50"
                  : isSelected
                  ? "border-lime-600/50 bg-zinc-800/60"
                  : "border-zinc-700/30 bg-zinc-800/30"
              }`}
            >
              {/* Slot header row */}
              <div className="flex items-center gap-2 px-3 py-2">
                {/* Clip label */}
                <button
                  type="button"
                  className="flex-1 text-left"
                  onClick={() => setSelectedKey(isSelected ? null : slot.key)}
                  disabled={slot.removed}
                >
                  <span className="text-[10px] font-semibold text-zinc-400 uppercase tracking-wider">
                    Clip {slot.clipIndex + 1}
                  </span>
                  {!slot.removed && (
                    <span className="ml-2 text-[10px] text-zinc-500 tabular-nums">{durText}</span>
                  )}
                </button>

                {/* Reorder buttons */}
                {!slot.removed && (
                  <div className="flex gap-0.5">
                    <button
                      type="button"
                      aria-label="Move clip up"
                      disabled={idx === 0 || slot.removed}
                      onClick={() => dispatch({ type: "REORDER", from: idx, to: idx - 1 })}
                      className="flex h-6 w-6 items-center justify-center rounded text-zinc-400 hover:bg-zinc-700/50 hover:text-white disabled:opacity-25"
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      aria-label="Move clip down"
                      disabled={idx === state.slots.length - 1 || slot.removed}
                      onClick={() => dispatch({ type: "REORDER", from: idx, to: idx + 1 })}
                      className="flex h-6 w-6 items-center justify-center rounded text-zinc-400 hover:bg-zinc-700/50 hover:text-white disabled:opacity-25"
                    >
                      ↓
                    </button>
                  </div>
                )}

                {/* Duration nudge */}
                {!slot.removed && (
                  <div className="flex gap-0.5">
                    <button
                      type="button"
                      aria-label="Shorten clip"
                      onClick={() => dispatch({ type: "NUDGE", key: slot.key, delta: -1 })}
                      className="flex h-6 w-6 items-center justify-center rounded text-zinc-400 hover:bg-zinc-700/50 hover:text-white"
                    >
                      −
                    </button>
                    <button
                      type="button"
                      aria-label="Lengthen clip"
                      onClick={() => dispatch({ type: "NUDGE", key: slot.key, delta: 1 })}
                      className="flex h-6 w-6 items-center justify-center rounded text-zinc-400 hover:bg-zinc-700/50 hover:text-white"
                    >
                      +
                    </button>
                  </div>
                )}

                {/* Remove / Restore */}
                {slot.removed ? (
                  <button
                    type="button"
                    onClick={() => dispatch({ type: "RESTORE", key: slot.key })}
                    className="text-[10px] text-zinc-400 hover:text-white"
                  >
                    Restore
                  </button>
                ) : (
                  <button
                    type="button"
                    aria-label="Remove clip"
                    onClick={() => dispatch({ type: "REMOVE", key: slot.key })}
                    className="text-[10px] text-zinc-500 hover:text-zinc-300"
                  >
                    ✕
                  </button>
                )}
              </div>

              {/* In-point scrubber (expanded) */}
              {isSelected && !slot.removed && win && (
                <div className="px-3 pb-3">
                  <InPointScrubber
                    inS={slot.inS}
                    windowS={win.durationS}
                    sourceDurationS={srcDur}
                    grid={state.grid}
                    offsetBeats={win.offsetBeats}
                    durationBeats={slot.durationBeats}
                    isSeconds={isNoGrid || slot.durationBeats == null}
                    onChange={(inS, record) =>
                      dispatch({ type: "SET_IN", key: slot.key, inS, record })
                    }
                  />
                </div>
              )}
            </div>
          );
        })}
      </div>

      {liveSlots.length === 0 && (
        <p className="text-center text-xs text-zinc-500">
          Restore at least one clip to re-render.
        </p>
      )}

      {submitError && (
        <p className="text-center text-xs text-red-400">{submitError}</p>
      )}

      {/* Bottom bar: undo/redo + reset + apply */}
      <div className="flex items-center justify-between gap-2 pt-1">
        <div className="flex items-center gap-1">
          <button
            type="button"
            aria-label="Undo"
            disabled={!canUndo || submitting}
            onClick={() => dispatch({ type: "UNDO" })}
            className="flex h-7 w-7 items-center justify-center rounded text-zinc-400 hover:bg-zinc-700/50 hover:text-white disabled:opacity-25"
          >
            ↩
          </button>
          <button
            type="button"
            aria-label="Redo"
            disabled={!canRedo || submitting}
            onClick={() => dispatch({ type: "REDO" })}
            className="flex h-7 w-7 items-center justify-center rounded text-zinc-400 hover:bg-zinc-700/50 hover:text-white disabled:opacity-25"
          >
            ↪
          </button>
          {timelineData.has_user_edits && (
            <button
              type="button"
              disabled={submitting}
              onClick={() => void handleReset()}
              className="ml-1 text-[10px] text-zinc-500 hover:text-zinc-300 disabled:opacity-40"
            >
              Reset to AI cut
            </button>
          )}
        </div>
        <button
          type="button"
          disabled={submitting || !dirty || liveSlots.length === 0}
          onClick={() => void handleApply()}
          className="rounded-full bg-white px-4 py-1.5 text-xs font-semibold text-black hover:opacity-80 disabled:bg-zinc-700 disabled:text-zinc-400 disabled:opacity-100"
        >
          {ctaLabel}
        </button>
      </div>
    </div>
  );
}
