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
  beatsForWindowSeconds,
  countEdits,
  formatInPoint,
  formatSeconds,
  maxGridBeats,
  SECONDS_FLOOR,
  slotWindows,
  totalDurationS,
  type DraftSlot,
} from "../../generative/timeline-math";
import {
  initEditorState,
  timelineReducer,
  type EditorAction,
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
  pointerId: number;
  pointerType: string;
  startX: number;
  startY: number;
  startInS: number;
  startDurS: number;
  startBeats: number | null;
  startOffsetBeats: number | null;
  scaleS: number;      // total seconds represented by containerW pixels
  containerW: number;
  startTarget: HTMLElement;
  hasIntent: boolean;
  recorded: boolean;
  lastInS: number;
  lastDurS: number;
  lastBeats: number | null;
}

interface DragView {
  key: string;
  kind: DragKind;
  hasIntent: boolean;
  inS: number;
  durationS: number;
}

interface OutputBarLayout {
  slot: DraftSlot;
  slotIndex: number;
  window: ReturnType<typeof slotWindows>[number];
  leftPct: number;
  widthPct: number;
  leftPx: number;
  widthPx: number;
  rightPx: number;
}

interface HandleHitLayout {
  left: { offsetPx: number; widthPx: number };
  right: { offsetPx: number; widthPx: number };
}

// ─── Helpers ────────────────────────────────────────────────────────────────────

function TimeRuler({ totalS }: { totalS: number }) {
  const step = totalS <= 8 ? 1 : totalS <= 20 ? 2 : 5;
  const ticks: number[] = [];
  for (let t = 0; t <= totalS + 1e-6; t += step) ticks.push(t);
  return (
    <div className="relative h-5 select-none">
      {ticks.map((t, i) => {
        // First label left-aligns, last right-aligns, middle center — so no
        // label ever extends past the rail edges (375px page-overflow guard).
        const isLast = i === ticks.length - 1 && i > 0;
        return (
          <div
            key={t}
            className="absolute top-0"
            style={{ left: `${(t / (totalS || 1)) * 100}%` }}
          >
            <div className="h-2 w-px bg-zinc-300" />
            <span
              className={`absolute top-2 whitespace-nowrap text-[11px] leading-none tabular-nums text-[#71717a] ${
                i === 0 ? "left-0" : isLast ? "right-0" : "left-0 -translate-x-1/2"
              }`}
            >
              {formatSeconds(t)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function DragTimeChip({ inS, durationS }: { inS: number; durationS: number }) {
  return (
    <div className="pointer-events-none whitespace-nowrap rounded-full border border-zinc-200 bg-white px-2.5 py-1 text-[11px] font-medium text-[#3f3f46] shadow-sm">
      <span className="tabular-nums">In {formatInPoint(inS)}</span>
      <span className="mx-1.5 text-zinc-300">|</span>
      <span className="tabular-nums">Out {formatInPoint(inS + durationS)}</span>
      <span className="mx-1.5 text-zinc-300">|</span>
      <span className="tabular-nums">Dur {formatSeconds(durationS)}</span>
    </div>
  );
}

function sameTrimValue(a: DraftSlot | undefined, b: DraftSlot | undefined) {
  if (!a || !b) return false;
  return (
    Math.abs(a.inS - b.inS) < 1e-6 &&
    Math.abs((a.durationS ?? 0) - (b.durationS ?? 0)) < 1e-6 &&
    a.durationBeats === b.durationBeats
  );
}

function clampPx(value: number, min: number, max: number) {
  if (max < min) return min;
  return Math.min(Math.max(value, min), max);
}

function computeHandleHitLayout(
  bar: OutputBarLayout,
  containerW: number,
  prevBar: OutputBarLayout | null,
  nextBar: OutputBarLayout | null,
): HandleHitLayout {
  const prevRight = prevBar?.rightPx ?? 0;
  const nextLeft = nextBar?.leftPx ?? containerW;
  const leftGap = Math.max(0, bar.leftPx - prevRight);
  const rightGap = Math.max(0, nextLeft - bar.rightPx);
  const leftOut = Math.min(22, leftGap / 2, Math.max(0, bar.leftPx));
  const rightOut = Math.min(22, rightGap / 2, Math.max(0, containerW - bar.rightPx));
  let leftIn = 44 - leftOut;
  let rightIn = 44 - rightOut;

  if (leftIn + rightIn > bar.widthPx) {
    const midpoint = Math.max(0, bar.widthPx / 2);
    leftIn = Math.min(leftIn, midpoint);
    rightIn = Math.min(rightIn, Math.max(0, bar.widthPx - leftIn));
  }

  return {
    left: { offsetPx: -leftOut, widthPx: leftOut + leftIn },
    right: { offsetPx: -rightOut, widthPx: rightOut + rightIn },
  };
}

// ─── Instant client-side preview player ─────────────────────────────────────────
// One <video> per active slot, stacked. Pre-seeks to inS on every render
// (while paused) so any drag is reflected immediately without a server call.

function PreviewPlayer({
  activeSlots,
  windows,
  clips,
  onTimeChange,
}: {
  activeSlots: DraftSlot[];
  windows: ReturnType<typeof slotWindows>;
  clips: TimelineClip[];
  onTimeChange?: (timeS: number) => void;
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
      onTimeChange?.(0);
      return;
    }
    const slot = activeSlots[idx];
    const v = videoRefs.current[idx];
    if (!v) { playFrom(idx + 1); return; }
    v.currentTime = slot.inS;
    onTimeChange?.(windows[idx]?.startS ?? 0);
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
    onTimeChange?.((win.startS ?? 0) + Math.max(0, v.currentTime - slot.inS));
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
      <div
        className="flex w-24 flex-shrink-0 items-center justify-center rounded-[10px] border border-zinc-200 bg-white text-[11px] text-[#71717a] sm:w-16 sm:rounded-lg"
        style={{ aspectRatio: "9/16" }}
      >
        No preview
      </div>
    );
  }

  return (
    <div
      className="relative w-24 flex-shrink-0 overflow-hidden rounded-[10px] bg-black shadow-[0_12px_30px_rgba(0,0,0,0.18)] sm:w-16 sm:rounded-lg"
      style={{ aspectRatio: "9/16" }}
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
  onNudgeIn,
  onNudgeOut,
  canNudgeInEarlier,
  canNudgeInLater,
  canNudgeOutEarlier,
  canNudgeOutLater,
  activeDrag,
}: {
  slot: DraftSlot;
  clip: TimelineClip | undefined;
  windowS: number;
  onSrcLeftDown: (e: React.PointerEvent<HTMLElement>, cw: number, srcDurS: number) => void;
  onSrcRightDown: (e: React.PointerEvent<HTMLElement>, cw: number, srcDurS: number) => void;
  onNudgeIn: (delta: number) => void;
  onNudgeOut: (delta: number) => void;
  canNudgeInEarlier: boolean;
  canNudgeInLater: boolean;
  canNudgeOutEarlier: boolean;
  canNudgeOutLater: boolean;
  activeDrag: DragView | null;
}) {
  const srcBarRef = useRef<HTMLDivElement>(null);
  const srcDur = clip?.duration_s ?? null;
  const effectiveSrc = srcDur ?? Math.max(slot.inS + windowS + 0.5, 1);
  const inFrac = Math.min(1, slot.inS / effectiveSrc);
  const outFrac = Math.min(1, (slot.inS + windowS) / effectiveSrc);
  const isLeftActive = activeDrag?.key === slot.key && activeDrag.kind === "src-left";
  const isRightActive = activeDrag?.key === slot.key && activeDrag.kind === "src-right";
  const isLeftPressed = isLeftActive && activeDrag?.hasIntent;
  const isRightPressed = isRightActive && activeDrag?.hasIntent;

  return (
    <div className="mt-2 rounded-lg border border-zinc-200 bg-white p-2.5">
      <p className="mb-2 text-[11px] font-medium text-[#71717a]">
        Clip {slot.clipIndex} source{srcDur ? ` — ${formatSeconds(srcDur)} total` : ""}
      </p>
      <div
        ref={srcBarRef}
        className="relative h-14 select-none rounded-lg bg-[#fafaf8] [touch-action:pan-y]"
      >
        {/* Unused region (dimmed) */}
        <div className="absolute inset-x-0 top-4 h-6 rounded bg-zinc-100" />
        {/* Used window */}
        <div
          className="absolute top-4 h-6 rounded bg-lime-600/70"
          style={{ left: `${inFrac * 100}%`, right: `${(1 - outFrac) * 100}%` }}
        />
        {activeDrag?.key === slot.key && activeDrag.hasIntent && activeDrag.kind.startsWith("src-") && (
          <div
            className={`absolute -top-2 ${
              activeDrag.kind === "src-left" ? "translate-x-3" : "-translate-x-[calc(100%+12px)]"
            }`}
            style={{ left: `${activeDrag.kind === "src-left" ? inFrac * 100 : outFrac * 100}%` }}
          >
            <DragTimeChip inS={activeDrag.inS} durationS={activeDrag.durationS} />
          </div>
        )}
        {/* In-point handle */}
        <button
          type="button"
          aria-label="Drag in-point"
          className={`absolute top-1 flex h-12 w-11 cursor-ew-resize items-center justify-end rounded-l-lg pr-1 [touch-action:none] ${
            isLeftActive ? "z-20" : "z-10"
          }`}
          style={{ left: `${inFrac * 100}%`, transform: "translateX(-100%)" }}
          onPointerDown={(e) => {
            const cw = srcBarRef.current?.getBoundingClientRect().width ?? 200;
            onSrcLeftDown(e, cw, effectiveSrc);
          }}
        >
          <span className="mr-1 text-[11px] font-semibold text-lime-700">In</span>
          <span
            className={`h-9 w-3 rounded-full border border-zinc-200 transition-transform ${
              isLeftPressed ? "scale-110 bg-lime-600" : "bg-white"
            }`}
          />
        </button>
        {/* Out-point handle */}
        <button
          type="button"
          aria-label="Drag out-point"
          className={`absolute top-1 flex h-12 w-11 cursor-ew-resize items-center justify-start rounded-r-lg pl-1 [touch-action:none] ${
            isRightActive ? "z-20" : "z-10"
          }`}
          style={{ left: `${outFrac * 100}%` }}
          onPointerDown={(e) => {
            const cw = srcBarRef.current?.getBoundingClientRect().width ?? 200;
            onSrcRightDown(e, cw, effectiveSrc);
          }}
        >
          <span
            className={`h-9 w-3 rounded-full border border-zinc-200 transition-transform ${
              isRightPressed ? "scale-110 bg-lime-600" : "bg-white"
            }`}
          />
          <span className="ml-1 text-[11px] font-semibold text-lime-700">Out</span>
        </button>
      </div>
      <div className="mt-2 flex justify-between text-[11px] tabular-nums text-[#71717a]">
        <span>In: {formatSeconds(slot.inS)}</span>
        <span>{formatSeconds(windowS)} used</span>
        <span>Out: {formatSeconds(slot.inS + windowS)}</span>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2">
        <div className="grid grid-cols-[44px_1fr_44px] items-center gap-1">
          <button
            type="button"
            aria-label="Nudge in-point earlier"
            className="min-h-11 rounded border border-zinc-200 bg-[#fafaf8] text-sm font-semibold text-[#0c0c0e] disabled:opacity-40"
            disabled={!canNudgeInEarlier}
            onClick={() => onNudgeIn(-0.1)}
          >
            -
          </button>
          <span className="text-center text-[11px] font-medium text-[#71717a]">In</span>
          <button
            type="button"
            aria-label="Nudge in-point later"
            className="min-h-11 rounded border border-zinc-200 bg-[#fafaf8] text-sm font-semibold text-[#0c0c0e] disabled:opacity-40"
            disabled={!canNudgeInLater}
            onClick={() => onNudgeIn(0.1)}
          >
            +
          </button>
        </div>
        <div className="grid grid-cols-[44px_1fr_44px] items-center gap-1">
          <button
            type="button"
            aria-label="Nudge out-point earlier"
            className="min-h-11 rounded border border-zinc-200 bg-[#fafaf8] text-sm font-semibold text-[#0c0c0e] disabled:opacity-40"
            disabled={!canNudgeOutEarlier}
            onClick={() => onNudgeOut(-0.1)}
          >
            -
          </button>
          <span className="text-center text-[11px] font-medium text-[#71717a]">Out</span>
          <button
            type="button"
            aria-label="Nudge out-point later"
            className="min-h-11 rounded border border-zinc-200 bg-[#fafaf8] text-sm font-semibold text-[#0c0c0e] disabled:opacity-40"
            disabled={!canNudgeOutLater}
            onClick={() => onNudgeOut(0.1)}
          >
            +
          </button>
        </div>
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
  const [previewTimeS, setPreviewTimeS] = useState(0);
  const [activeDrag, setActiveDrag] = useState<DragView | null>(null);
  const [outputBarWidth, setOutputBarWidth] = useState(300);
  const submitErrorRef = useRef<HTMLDivElement>(null);

  // Plan C: when the parent emits a focusedKey (because the user clicked a lane
  // bar), auto-select that clip so its SourcePanel opens without a second click.
  useEffect(() => {
    if (focusedKey !== undefined && focusedKey !== null) {
      setSelectedKey(focusedKey);
    }
  }, [focusedKey]);

  const dragRef = useRef<ActiveDrag | null>(null);
  const outputBarRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef(state);
  const dispatchRef = useRef(dispatch);
  const suppressClickTargetRef = useRef<HTMLElement | null>(null);

  useEffect(() => { stateRef.current = state; }, [state]);
  useEffect(() => { dispatchRef.current = dispatch; }, [dispatch]);
  useEffect(() => {
    const node = outputBarRef.current;
    if (!node) return;
    const measure = () => {
      const width = node.getBoundingClientRect().width;
      if (width > 0) setOutputBarWidth(width);
    };
    measure();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [loadState]);
  useEffect(() => {
    if (submitError) {
      submitErrorRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [submitError]);

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

  function nudgeInPoint(slot: DraftSlot, delta: number) {
    const action: EditorAction = {
      type: "SET_IN",
      key: slot.key,
      inS: Math.max(0, slot.inS + delta),
      record: true,
    };
    dispatchIfTrimChanged(action, slot.key);
  }

  function nudgeOutPoint(slot: DraftSlot, windowS: number, delta: number) {
    const idx = state.slots.indexOf(slot);
    const offsetBeats = windows[idx]?.offsetBeats ?? null;
    if (slot.durationBeats != null && offsetBeats != null) {
      dispatchIfTrimChanged({
        type: "NUDGE",
        key: slot.key,
        delta: delta > 0 ? 1 : -1,
      }, slot.key);
      return;
    }
    const durationS = Math.max(SECONDS_FLOOR, windowS + delta);
    dispatchIfTrimChanged({
      type: "SET_WINDOW",
      key: slot.key,
      inS: slot.inS,
      durationS,
      record: true,
    }, slot.key);
  }

  function canActionChangeTrim(action: EditorAction, key: string) {
    const currentState = stateRef.current;
    const nextState = timelineReducer(currentState, action);
    const before = currentState.slots.find((s) => s.key === key);
    const after = nextState.slots.find((s) => s.key === key);
    return !sameTrimValue(before, after);
  }

  function dispatchIfTrimChanged(action: EditorAction, key: string) {
    if (!canActionChangeTrim(action, key)) return false;
    dispatchRef.current(action);
    return true;
  }

  function beginDrag(
    e: React.PointerEvent<HTMLElement>,
    draft: Omit<
      ActiveDrag,
      "pointerId" | "pointerType" | "startY" | "startTarget" | "hasIntent" | "recorded" | "lastInS" | "lastDurS" | "lastBeats"
    >,
  ) {
    if (dragRef.current) return;
    if (e.isPrimary === false) return;
    e.preventDefault();
    suppressClickTargetRef.current = null;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    const next: ActiveDrag = {
      ...draft,
      pointerId: e.pointerId,
      pointerType: e.pointerType || "mouse",
      startY: e.clientY,
      startTarget: e.currentTarget,
      hasIntent: false,
      recorded: false,
      lastInS: draft.startInS,
      lastDurS: draft.startDurS,
      lastBeats: draft.startBeats,
    };
    dragRef.current = next;
    setActiveDrag({
      key: next.key,
      kind: next.kind,
      hasIntent: false,
      inS: next.startInS,
      durationS: next.startDurS,
    });
  }

  function dispatchDragValue(d: ActiveDrag, clientX: number) {
    const delta = ((clientX - d.startX) / d.containerW) * d.scaleS;
    const currentState = stateRef.current;
    let action: EditorAction;

    if (d.kind === "bar-left" || d.kind === "src-left") {
      const inS = Math.max(0, d.startInS + delta);
      action = { type: "SET_IN", key: d.key, inS, record: !d.recorded };
    } else {
      const durationS = Math.max(SECONDS_FLOOR, d.startDurS + delta);
      const slot = currentState.slots.find((s) => s.key === d.key);
      if (slot?.durationBeats != null && d.startOffsetBeats != null) {
        const beats = beatsForWindowSeconds(
          currentState.grid,
          d.startOffsetBeats,
          durationS,
          maxGridBeats(currentState.grid),
        );
        action = {
          type: "SET_DURATION_BEATS",
          key: d.key,
          beats,
          record: !d.recorded,
        };
      } else {
        action = {
          type: "SET_WINDOW",
          key: d.key,
          inS: d.startInS,
          durationS,
          record: !d.recorded,
        };
      }
    }

    const previewAction =
      "record" in action ? { ...action, record: false } : action;
    const nextState = timelineReducer(currentState, previewAction);
    const before = currentState.slots.find((s) => s.key === d.key);
    const after = nextState.slots.find((s) => s.key === d.key);
    if (sameTrimValue(before, after)) return;

    dispatchRef.current(action);
    d.recorded = true;

    const idx = nextState.slots.findIndex((s) => s.key === d.key);
    const nextSlot = nextState.slots[idx];
    const nextWindow = idx >= 0 ? slotWindows(nextState.slots, nextState.grid)[idx] : null;
    if (nextSlot) {
      d.lastInS = nextSlot.inS;
      d.lastDurS = nextWindow?.durationS ?? nextSlot.durationS ?? d.lastDurS;
      d.lastBeats = nextSlot.durationBeats;
      setActiveDrag({
        key: d.key,
        kind: d.kind,
        hasIntent: true,
        inS: d.lastInS,
        durationS: d.lastDurS,
      });
    }
  }

  function finishDrag(suppressClick: boolean) {
    const d = dragRef.current;
    if (!d) return;
    if (suppressClick && d.hasIntent) {
      suppressClickTargetRef.current = d.startTarget;
    }
    dragRef.current = null;
    setActiveDrag(null);
  }

  // ── Global drag ─────────────────────────────────────────────────────────────
  useEffect(() => {
    function onMove(e: PointerEvent) {
      const d = dragRef.current;
      if (!d) return;
      if (e.pointerId !== d.pointerId) return;
      const dx = e.clientX - d.startX;
      const dy = e.clientY - d.startY;
      if (!d.hasIntent) {
        const slop = d.pointerType === "touch" ? 8 : 3;
        const travel = Math.hypot(dx, dy);
        if (travel < slop) return;
        d.hasIntent = true;
        setActiveDrag((prev) => (prev ? { ...prev, hasIntent: true } : prev));
      }
      dispatchDragValue(d, e.clientX);
    }

    function onUp(e: PointerEvent) {
      const d = dragRef.current;
      if (!d) return;
      if (e.pointerId !== d.pointerId) return;
      if (d.hasIntent) dispatchDragValue(d, e.clientX);
      finishDrag(true);
    }

    function onCancel(e: PointerEvent) {
      const d = dragRef.current;
      if (!d) return;
      if (e.pointerId !== d.pointerId) return;
      if (d.hasIntent) dispatchDragValue(d, e.clientX);
      finishDrag(false);
    }

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onCancel);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onCancel);
    };
  }, []);

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
    return (
      <div className="space-y-2 py-4" aria-label="Loading clips">
        <div className="h-5 rounded bg-zinc-100 motion-safe:animate-pulse" />
        <div className="h-14 rounded-lg bg-zinc-100 motion-safe:animate-pulse" />
      </div>
    );
  }
  if (loadState === "error") {
    return (
      <div className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-center text-xs text-[#3f3f46]">
        <p>Failed to load clips</p>
        <button className="mt-1 text-xs font-medium text-lime-700 underline" onClick={load}>
          Retry
        </button>
      </div>
    );
  }

  const selectedSlot = selectedKey
    ? state.slots.find((s) => s.key === selectedKey && !s.removed) ?? null
    : null;
  const outputBars: OutputBarLayout[] = state.slots.flatMap((slot, slotIndex) => {
    if (slot.removed) return [];
    const window = windows[slotIndex];
    if (!window || window.durationS <= 0) return [];
    const leftPct = ((window.startS ?? 0) / (totalS || 1)) * 100;
    const widthPct = Math.min(
      Math.max(
        (window.durationS / (totalS || 1)) * 100,
        MIN_BAR_PCT,
      ),
      Math.max(0, 100 - leftPct),
    );
    const leftPx = (leftPct / 100) * outputBarWidth;
    const widthPx = (widthPct / 100) * outputBarWidth;
    return [{
      slot,
      slotIndex,
      window,
      leftPct,
      widthPct,
      leftPx,
      widthPx,
      rightPx: leftPx + widthPx,
    }];
  });

  // ── Render ───────────────────────────────────────────────────────────────────
  return (
    <div
      className="space-y-3 text-sm text-[#0c0c0e]"
      onPointerDownCapture={() => {
        suppressClickTargetRef.current = null;
      }}
      onClickCapture={(e) => {
        const suppressTarget = suppressClickTargetRef.current;
        if (!suppressTarget) return;
        suppressClickTargetRef.current = null;
        const clickTarget = e.target;
        if (clickTarget instanceof Node && suppressTarget.contains(clickTarget)) {
          e.preventDefault();
          e.stopPropagation();
        }
      }}
    >

      {/* ── Preview + timeline ──────────────────────────────────────────────── */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start">

        {/* Instant preview player */}
        <div className="flex items-center gap-3 sm:block">
          <PreviewPlayer
            activeSlots={activeSlots}
            windows={activeWindows}
            clips={clips}
            onTimeChange={setPreviewTimeS}
          />
          <div className="sm:hidden">
            <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-lime-700">
              Preview
            </p>
            <p className="mt-1 text-sm tabular-nums text-[#3f3f46]">
              {formatInPoint(previewTimeS)}
              <span className="mx-1 text-zinc-300">/</span>
              {formatInPoint(totalS || 0)}
            </p>
          </div>
        </div>

        {/* Output timeline */}
        <div className="min-w-0 flex-1">
          <TimeRuler totalS={totalS || 1} />
          <div
            ref={outputBarRef}
            className="relative mt-1 min-h-14 select-none rounded-lg bg-[#fafaf8] [touch-action:pan-y]"
          >
            {outputBars.map((bar, barIndex) => {
              const { slot, slotIndex: i, window: win, leftPct, widthPct } = bar;
              const sel = selectedKey === slot.key;
              const leftActive = activeDrag?.key === slot.key && activeDrag.kind === "bar-left";
              const rightActive = activeDrag?.key === slot.key && activeDrag.kind === "bar-right";
              const leftPressed = leftActive && activeDrag?.hasIntent;
              const rightPressed = rightActive && activeDrag?.hasIntent;
              const activeOutputDrag = activeDrag?.key === slot.key && activeDrag.hasIntent && activeDrag.kind.startsWith("bar-");
              const hitLayout = computeHandleHitLayout(
                bar,
                outputBarWidth,
                outputBars[barIndex - 1] ?? null,
                outputBars[barIndex + 1] ?? null,
              );
              const chipEdgePx = activeDrag?.kind === "bar-left" ? bar.leftPx : bar.rightPx;
              const chipLeftPx = clampPx(
                chipEdgePx + (activeDrag?.kind === "bar-left" ? 12 : -184),
                4,
                outputBarWidth - 184,
              ) - bar.leftPx;

              return (
                <div
                  key={slot.key}
                  className="absolute inset-y-2"
                  style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                >
                  {activeOutputDrag && (
                    <div
                      className="absolute -top-9 z-30"
                      style={{ left: `${chipLeftPx}px` }}
                    >
                      <DragTimeChip inS={activeDrag.inS} durationS={activeDrag.durationS} />
                    </div>
                  )}
                  {/* Left handle — trim in-point */}
                  <button
                    type="button"
                    aria-label={`Trim clip ${slot.clipIndex} in-point`}
                    data-inline-trim-handle={`left-${slot.key}`}
                    className={`absolute top-0 flex h-11 cursor-ew-resize items-center justify-end rounded-l-lg pr-1 [touch-action:none] ${
                      leftActive ? "z-30" : "z-10"
                    }`}
                    style={{
                      left: `${hitLayout.left.offsetPx}px`,
                      width: `${hitLayout.left.widthPx}px`,
                    }}
                    onPointerDown={(e) => {
                      const cw =
                        outputBarRef.current?.getBoundingClientRect().width ?? 300;
                      beginDrag(e, {
                        key: slot.key,
                        kind: "bar-left",
                        startX: e.clientX,
                        startInS: slot.inS,
                        startDurS: slot.durationS ?? win.durationS,
                        startBeats: slot.durationBeats,
                        startOffsetBeats: win.offsetBeats,
                        containerW: cw,
                        scaleS: totalS || 1,
                      });
                    }}
                  >
                    <span className="mr-1 hidden text-[11px] font-semibold text-lime-700 min-[420px]:inline">
                      In
                    </span>
                    <span
                      className={`h-9 w-3 rounded-full border border-zinc-200 transition-transform ${
                        leftPressed ? "scale-110 bg-lime-600" : "bg-white"
                      }`}
                    />
                  </button>

                  {/* Bar body */}
                  <button
                    type="button"
                    className={`absolute inset-y-0 left-0 right-0 flex min-w-0 items-center justify-center gap-1 truncate rounded border text-[11px] font-medium transition-colors [touch-action:pan-y] ${
                      sel
                        ? "border-lime-600 bg-lime-600 text-white"
                        : "border-zinc-200 bg-white text-[#3f3f46] hover:border-lime-200"
                    }`}
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
                  <button
                    type="button"
                    aria-label={`Trim clip ${slot.clipIndex} out-point`}
                    data-inline-trim-handle={`right-${slot.key}`}
                    className={`absolute top-0 flex h-11 cursor-ew-resize items-center justify-start rounded-r-lg pl-1 [touch-action:none] ${
                      rightActive ? "z-30" : "z-10"
                    }`}
                    style={{
                      right: `${hitLayout.right.offsetPx}px`,
                      width: `${hitLayout.right.widthPx}px`,
                    }}
                    onPointerDown={(e) => {
                      const cw =
                        outputBarRef.current?.getBoundingClientRect().width ?? 300;
                      beginDrag(e, {
                        key: slot.key,
                        kind: "bar-right",
                        startX: e.clientX,
                        startInS: slot.inS,
                        startDurS: slot.durationS ?? win.durationS,
                        startBeats: slot.durationBeats,
                        startOffsetBeats: win.offsetBeats,
                        containerW: cw,
                        scaleS: totalS || 1,
                      });
                    }}
                  >
                    <span
                      className={`h-9 w-3 rounded-full border border-zinc-200 transition-transform ${
                        rightPressed ? "scale-110 bg-lime-600" : "bg-white"
                      }`}
                    />
                    <span className="ml-1 hidden text-[11px] font-semibold text-lime-700 min-[420px]:inline">
                      Out
                    </span>
                  </button>
                </div>
              );
            })}
          </div>
          <p className="mt-1 text-[11px] text-[#71717a]">
            <span className="text-lime-700">In</span> trim start
            {"  "}
            <span className="text-lime-700">Out</span> trim end
            {"  ·  "}click bar for source trim
          </p>
        </div>
      </div>

      {/* ── Source clip trim panel (when bar selected) ───────────────────────── */}
      {selectedSlot && (() => {
        const idx = state.slots.indexOf(selectedSlot);
        const win = windows[idx];
        const windowS = win?.durationS ?? 0;
        const gridOutAction = (delta: number): EditorAction => ({
          type: "NUDGE",
          key: selectedSlot.key,
          delta: delta > 0 ? 1 : -1,
        });
        const secondsOutAction = (delta: number): EditorAction => ({
          type: "SET_WINDOW",
          key: selectedSlot.key,
          inS: selectedSlot.inS,
          durationS: Math.max(SECONDS_FLOOR, windowS + delta),
          record: true,
        });
        const outAction = (delta: number) =>
          selectedSlot.durationBeats != null && win?.offsetBeats != null
            ? gridOutAction(delta)
            : secondsOutAction(delta);
        const inAction = (delta: number): EditorAction => ({
          type: "SET_IN",
          key: selectedSlot.key,
          inS: Math.max(0, selectedSlot.inS + delta),
          record: true,
        });
        return (
          <SourcePanel
            slot={selectedSlot}
            clip={clipFor(selectedSlot)}
            windowS={windowS}
            onSrcLeftDown={(e, cw, srcDurS) => {
              beginDrag(e, {
                key: selectedSlot.key,
                kind: "src-left",
                startX: e.clientX,
                startInS: selectedSlot.inS,
                startDurS: selectedSlot.durationS ?? (win?.durationS ?? 1),
                startBeats: selectedSlot.durationBeats,
                startOffsetBeats: win?.offsetBeats ?? null,
                containerW: cw,
                scaleS: srcDurS,
              });
            }}
            onSrcRightDown={(e, cw, srcDurS) => {
              beginDrag(e, {
                key: selectedSlot.key,
                kind: "src-right",
                startX: e.clientX,
                startInS: selectedSlot.inS,
                startDurS: selectedSlot.durationS ?? (win?.durationS ?? 1),
                startBeats: selectedSlot.durationBeats,
                startOffsetBeats: win?.offsetBeats ?? null,
                containerW: cw,
                scaleS: srcDurS,
              });
            }}
            onNudgeIn={(delta) => nudgeInPoint(selectedSlot, delta)}
            onNudgeOut={(delta) => nudgeOutPoint(selectedSlot, windowS, delta)}
            canNudgeInEarlier={canActionChangeTrim(inAction(-0.1), selectedSlot.key)}
            canNudgeInLater={canActionChangeTrim(inAction(0.1), selectedSlot.key)}
            canNudgeOutEarlier={canActionChangeTrim(outAction(-0.1), selectedSlot.key)}
            canNudgeOutLater={canActionChangeTrim(outAction(0.1), selectedSlot.key)}
            activeDrag={activeDrag}
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
              className={`flex items-center gap-2 rounded border px-2 py-1.5 text-xs transition-colors ${
                sel ? "border-lime-200 bg-lime-50" : "border-zinc-200 bg-white hover:border-lime-200"
              }`}
            >
              <button
                type="button"
                className="min-h-8 flex-1 truncate text-left text-[#3f3f46]"
                onClick={() => setSelectedKey(sel ? null : slot.key)}
              >
                Clip {slot.clipIndex}
              </button>
              <span className="shrink-0 tabular-nums text-[#71717a]">
                {formatSeconds(windows[allIdx]?.durationS ?? 0)}
              </span>
              <button
                type="button"
                aria-label="Move up"
                disabled={allIdx === 0}
                className="min-h-8 min-w-8 shrink-0 rounded text-[#71717a] hover:bg-[#fafaf8] hover:text-[#0c0c0e] disabled:opacity-30"
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
                className="min-h-8 min-w-8 shrink-0 rounded text-[#71717a] hover:bg-[#fafaf8] hover:text-[#0c0c0e] disabled:opacity-30"
                onClick={() =>
                  dispatch({ type: "REORDER", from: allIdx, to: allIdx + 1 })
                }
              >
                ↓
              </button>
              <button
                type="button"
                aria-label="Remove"
                className="min-h-8 min-w-8 shrink-0 rounded text-[#71717a] hover:bg-[#fafaf8] hover:text-[#0c0c0e]"
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
      <div className="flex flex-wrap items-center gap-2 border-t border-zinc-200 pt-2">
        <button
          type="button"
          disabled={state.past.length === 0}
          className="min-h-9 rounded border border-zinc-200 bg-white px-2 text-xs text-[#3f3f46] hover:bg-[#fafaf8] disabled:opacity-30"
          onClick={() => dispatch({ type: "UNDO" })}
        >
          ↩ Undo
        </button>
        <button
          type="button"
          disabled={state.future.length === 0}
          className="min-h-9 rounded border border-zinc-200 bg-white px-2 text-xs text-[#3f3f46] hover:bg-[#fafaf8] disabled:opacity-30"
          onClick={() => dispatch({ type: "REDO" })}
        >
          ↪ Redo
        </button>
        <button
          type="button"
          disabled={submitting}
          className="min-h-9 rounded border border-zinc-200 bg-white px-2 text-xs text-[#3f3f46] hover:bg-[#fafaf8] disabled:opacity-50"
          onClick={handleReset}
        >
          Reset
        </button>
        <div className="flex-1" />
        <span className="text-[11px] tabular-nums text-[#71717a]">
          {totalS > 0 && formatSeconds(totalS)}
          {edits > 0 && ` · ${edits} edit${edits > 1 ? "s" : ""}`}
        </span>
        {submitError && (
          <div
            ref={submitErrorRef}
            className="order-last w-full rounded border border-zinc-200 bg-white px-3 py-2 text-xs text-[#3f3f46] sm:order-none sm:w-auto"
          >
            {submitError}
          </div>
        )}
        <button
          type="button"
          disabled={submitting || edits === 0}
          className="min-h-9 rounded-lg bg-lime-600 px-3 text-xs font-medium text-white transition-colors hover:bg-lime-700 disabled:opacity-50"
          onClick={handleApply}
        >
          {submitting ? "Saving…" : "Apply & Re-render"}
        </button>
      </div>
    </div>
  );
}
