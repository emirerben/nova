"use client";

/**
 * Full-screen clip-timeline editor sheet (light editorial).
 *
 * Filmstrip of slot cards over the variant's beat grid: reorder (drag handle or
 * arrow buttons), trim the in-point, nudge duration by whole beats (or 0.5s in
 * no-grid mode), swap/remove/restore/add clips — then commit ONE re-render.
 * All draft state is in-memory (timeline-reducer) with bounded undo/redo;
 * invalid drafts are clamped client-side and never submitted.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type Dispatch,
} from "react";
import {
  editTimeline,
  getTimeline,
  resetTimeline,
  TimelineApiError,
  type TimelineClip,
  type TimelineEditSlotPayload,
  type TimelineResponse,
} from "@/lib/generative-api";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { useFocusTrap } from "@/components/ui/useFocusTrap";
import {
  countEdits,
  formatInPoint,
  formatSeconds,
  formatTimecode,
  remainingBeats,
  SECONDS_FLOOR,
  slotWindows,
  totalDurationS,
  MAX_TOTAL_SECONDS,
  type DraftSlot,
  type SlotWindow,
} from "./timeline-math";
import {
  initEditorState,
  timelineReducer,
  type EditorAction,
} from "./timeline-reducer";

// ── Copy maps ─────────────────────────────────────────────────────────────────

const UNEDITABLE_COPY: Record<string, string> = {
  disabled: "Clip editing is turned off right now.",
  lyrics_sync: "This cut is synced to the song's lyrics — its clips stay where they are.",
  no_slot_timeline: "This edit was made before clip editing — your newer edits are editable.",
  no_timeline: "This edit was made before clip editing — your newer edits are editable.",
  voiceover_bed_fit: "This cut is fitted to your voiceover — its clips stay where they are.",
  sources_expired: "These clips have expired from storage. New uploads stay editable.",
};

const SUBMIT_ERROR_COPY: Record<string, string> = {
  TIMELINE_OUT_OF_BOUNDS: "This trim runs past the end of the clip.",
  TIMELINE_TOO_SHORT: "The cut is too short — give it a little more time.",
  TIMELINE_EMPTY: "The cut needs at least one clip.",
  TIMELINE_UNKNOWN_CLIP: "This clip isn't part of the edit anymore.",
  TIMELINE_BEATS_EXHAUSTED: "The song ran out of beats for this cut.",
  TIMELINE_TOO_LONG: "The cut runs past 60 seconds — trim it down.",
  sources_expired: "Some clips have expired from storage, so this cut can't re-render.",
  disabled: "Clip editing is turned off right now.",
};

// ── Poster-frame capture (offscreen <video> + <canvas>) ───────────────────────

const POSTER_CONCURRENCY = 2;

function captureFrame(url: string, seekS: number): Promise<string> {
  return new Promise((resolve, reject) => {
    const video = document.createElement("video");
    video.crossOrigin = "anonymous";
    video.muted = true;
    video.preload = "auto";
    video.playsInline = true;
    let settled = false;
    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      video.removeAttribute("src");
      video.load();
      fn();
    };
    const timer = setTimeout(() => finish(() => reject(new Error("poster timeout"))), 8000);
    video.onerror = () => {
      clearTimeout(timer);
      finish(() => reject(new Error("poster decode failed")));
    };
    video.onloadedmetadata = () => {
      const dur = Number.isFinite(video.duration) ? video.duration : seekS;
      video.currentTime = Math.min(Math.max(0, seekS), Math.max(0, dur - 0.05));
    };
    video.onseeked = () => {
      try {
        const canvas = document.createElement("canvas");
        canvas.width = 180;
        canvas.height = 320;
        const ctx = canvas.getContext("2d");
        if (!ctx || !video.videoWidth) throw new Error("no frame");
        // cover-fit 9:16
        const scale = Math.max(180 / video.videoWidth, 320 / video.videoHeight);
        const w = video.videoWidth * scale;
        const h = video.videoHeight * scale;
        ctx.drawImage(video, (180 - w) / 2, (320 - h) / 2, w, h);
        const dataUrl = canvas.toDataURL("image/jpeg", 0.72);
        clearTimeout(timer);
        finish(() => resolve(dataUrl));
      } catch {
        clearTimeout(timer);
        finish(() => reject(new Error("poster draw failed")));
      }
    };
    video.src = url;
  });
}

/** Captures one poster per clip (at its first-used in-point), cap-bounded
 * concurrency, cached data URLs. "failed" → callers show the fallback card. */
function usePosterFrames(
  clips: TimelineClip[],
  slots: DraftSlot[],
): Record<number, string | "failed" | undefined> {
  const [posters, setPosters] = useState<Record<number, string | "failed">>({});
  const startedRef = useRef<Set<number>>(new Set());
  const activeRef = useRef(0);
  const queueRef = useRef<Array<{ clipIndex: number; url: string; seekS: number }>>([]);

  const pump = useCallback(() => {
    while (activeRef.current < POSTER_CONCURRENCY && queueRef.current.length > 0) {
      const job = queueRef.current.shift()!;
      activeRef.current += 1;
      captureFrame(job.url, job.seekS)
        .then((dataUrl) =>
          setPosters((p) => ({ ...p, [job.clipIndex]: dataUrl })),
        )
        .catch(() =>
          setPosters((p) => ({ ...p, [job.clipIndex]: "failed" as const })),
        )
        .finally(() => {
          activeRef.current -= 1;
          pump();
        });
    }
  }, []);

  useEffect(() => {
    for (const clip of clips) {
      if (startedRef.current.has(clip.clip_index)) continue;
      startedRef.current.add(clip.clip_index);
      const firstSlot = slots.find((s) => s.clipIndex === clip.clip_index);
      queueRef.current.push({
        clipIndex: clip.clip_index,
        url: clip.signed_url,
        seekS: firstSlot?.inS ?? 0,
      });
    }
    pump();
  }, [clips, slots, pump]);

  return posters;
}

// ── Sheet shell (fetch + states) ──────────────────────────────────────────────

export interface TimelineEditorProps {
  jobId: string;
  variantId: string;
  onClose: () => void;
  /** Successful POST/DELETE — the session takes over the re-render wait. */
  onRenderEnqueued: () => void;
}

export function TimelineEditor({
  jobId,
  variantId,
  onClose,
  onRenderEnqueued,
}: TimelineEditorProps) {
  const [timeline, setTimeline] = useState<TimelineResponse | null>(null);
  const [fetchError, setFetchError] = useState(false);
  const [loadNonce, setLoadNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setTimeline(null);
    setFetchError(false);
    getTimeline(jobId, variantId)
      .then((t) => {
        if (!cancelled) setTimeline(t);
      })
      .catch(() => {
        if (!cancelled) setFetchError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [jobId, variantId, loadNonce]);

  // Lock page scroll while the sheet is open.
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  const reload = useCallback(() => setLoadNonce((n) => n + 1), []);

  return (
    <div className="fixed inset-0 z-50 flex items-stretch justify-center bg-black/30 sm:items-center sm:px-6 sm:py-8">
      {timeline == null && !fetchError && (
        <SheetFrame onRequestClose={onClose} dirty={false}>
          <SheetHeader onClose={onClose} />
          <div className="flex-1 space-y-3 overflow-y-auto px-5 pb-5">
            {[0, 1, 2, 3].map((i) => (
              <div
                key={i}
                className="h-[120px] rounded-2xl border border-zinc-200 bg-[length:200%_100%] bg-gradient-to-r from-zinc-100 via-zinc-200 to-zinc-100 motion-safe:animate-shimmer"
                aria-hidden="true"
              />
            ))}
            <p className="sr-only" role="status">
              Loading your cut…
            </p>
          </div>
        </SheetFrame>
      )}

      {fetchError && (
        <SheetFrame onRequestClose={onClose} dirty={false}>
          <SheetHeader onClose={onClose} />
          <div className="flex flex-1 flex-col items-start gap-4 px-5 pb-8 pt-2">
            <p className="font-display text-lg text-[#3f3f46]">
              We couldn&apos;t load this cut.
            </p>
            <button
              onClick={reload}
              className="rounded-full border border-zinc-200 px-4 py-2 text-sm text-[#3f3f46] hover:border-zinc-400"
            >
              Retry
            </button>
          </div>
        </SheetFrame>
      )}

      {timeline != null && !timeline.editable && (
        <SheetFrame onRequestClose={onClose} dirty={false}>
          <SheetHeader onClose={onClose} />
          <div className="flex flex-1 flex-col items-start gap-5 px-5 pb-8 pt-2">
            <p className="font-display text-lg leading-relaxed text-[#3f3f46]">
              {UNEDITABLE_COPY[timeline.reason ?? ""] ??
                "This cut can't be edited."}
            </p>
            <button
              onClick={onClose}
              className="rounded-full bg-[#0c0c0e] px-6 py-2.5 text-sm font-semibold text-white hover:opacity-80"
            >
              Back to your edit
            </button>
          </div>
        </SheetFrame>
      )}

      {timeline != null && timeline.editable && (
        <TimelineEditorBody
          key={loadNonce}
          jobId={jobId}
          variantId={variantId}
          timeline={timeline}
          onClose={onClose}
          onRenderEnqueued={onRenderEnqueued}
          reload={reload}
        />
      )}
    </div>
  );
}

// ── Frame + header ────────────────────────────────────────────────────────────

function SheetFrame({
  children,
  onRequestClose,
  dirty,
}: {
  children: React.ReactNode;
  onRequestClose: () => void;
  dirty: boolean;
}) {
  const frameRef = useRef<HTMLDivElement>(null);
  useFocusTrap(frameRef, true);

  // dirty is consumed by the body's own Escape handler; here only the
  // clean-sheet states route Escape straight to close.
  useEffect(() => {
    if (dirty) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onRequestClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [dirty, onRequestClose]);

  return (
    <div
      ref={frameRef}
      role="dialog"
      aria-modal="true"
      aria-label="Your cut"
      className="relative flex h-full w-full flex-col overflow-hidden bg-[#fafaf8] sm:h-auto sm:max-h-[90vh] sm:w-full sm:max-w-[520px] sm:rounded-2xl sm:border sm:border-zinc-200 sm:shadow-[0_12px_30px_rgba(0,0,0,0.18)]"
    >
      {children}
    </div>
  );
}

function SheetHeader({
  onClose,
  onUndo,
  onRedo,
  canUndo = false,
  canRedo = false,
}: {
  onClose: () => void;
  onUndo?: () => void;
  onRedo?: () => void;
  canUndo?: boolean;
  canRedo?: boolean;
}) {
  return (
    <div className="flex items-center justify-between px-5 pb-3 pt-5">
      <h2 className="font-display text-2xl text-[#0c0c0e]">Your cut</h2>
      <div className="flex items-center gap-1">
        {onUndo && (
          <button
            onClick={onUndo}
            disabled={!canUndo}
            aria-label="Undo"
            className="flex h-11 w-11 items-center justify-center rounded-full text-[#3f3f46] hover:bg-zinc-100 disabled:opacity-30"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M9 14L4 9l5-5" />
              <path d="M4 9h10a6 6 0 0 1 0 12h-3" />
            </svg>
          </button>
        )}
        {onRedo && (
          <button
            onClick={onRedo}
            disabled={!canRedo}
            aria-label="Redo"
            className="flex h-11 w-11 items-center justify-center rounded-full text-[#3f3f46] hover:bg-zinc-100 disabled:opacity-30"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M15 14l5-5-5-5" />
              <path d="M20 9H10a6 6 0 0 0 0 12h3" />
            </svg>
          </button>
        )}
        <button
          onClick={onClose}
          aria-label="Close editor"
          className="flex h-11 w-11 items-center justify-center rounded-full text-[#3f3f46] hover:bg-zinc-100"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>
      </div>
    </div>
  );
}

// ── Editor body ───────────────────────────────────────────────────────────────

interface DragState {
  index: number;
  dy: number;
  targetIndex: number;
  lifted: boolean;
}

function TimelineEditorBody({
  jobId,
  variantId,
  timeline,
  onClose,
  onRenderEnqueued,
  reload,
}: {
  jobId: string;
  variantId: string;
  timeline: TimelineResponse;
  onClose: () => void;
  onRenderEnqueued: () => void;
  reload: () => void;
}) {
  const [state, dispatch] = useReducer(
    timelineReducer,
    timeline,
    initEditorState,
  );
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<{
    code: string;
    slotKey: string | null;
  } | null>(null);
  const [stale, setStale] = useState(false);
  const [busyNotice, setBusyNotice] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const [drag, setDrag] = useState<DragState | null>(null);

  const isNoGrid = state.grid.length === 0;
  const windows = useMemo(
    () => slotWindows(state.slots, state.grid),
    [state.slots, state.grid],
  );
  const edits = useMemo(
    () => countEdits(state.baseline, state.slots),
    [state.baseline, state.slots],
  );
  const dirty = edits > 0;
  const liveSlots = state.slots.filter((s) => !s.removed);

  const posters = usePosterFrames(timeline.clips, state.slots);
  const clipByIndex = useMemo(() => {
    const m = new Map<number, TimelineClip>();
    for (const c of timeline.clips) m.set(c.clip_index, c);
    return m;
  }, [timeline.clips]);

  // Initial focus → first slot card.
  const firstCardRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    firstCardRef.current?.focus();
  }, []);

  // Escape routed through the unsaved-changes confirm.
  const requestClose = useCallback(() => {
    if (dirty || state.past.length > 0) setConfirmClose(true);
    else onClose();
  }, [dirty, state.past.length, onClose]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !confirmClose && !stale) requestClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [requestClose, confirmClose, stale]);

  // ── Reorder (buttons + announcement) ──
  const moveSlot = useCallback(
    (from: number, to: number) => {
      if (to < 0 || to >= state.slots.length) return;
      dispatch({ type: "REORDER", from, to });
      setAnnouncement(`Slot ${from + 1} moved to position ${to + 1}`);
    },
    [state.slots.length],
  );

  const handleUndo = useCallback(() => {
    dispatch({ type: "UNDO" });
    setAnnouncement("Undid last change");
  }, []);
  const handleRedo = useCallback(() => {
    dispatch({ type: "REDO" });
    setAnnouncement("Redid last change");
  }, []);

  // ── Drag from the handle ONLY ──
  const listRef = useRef<HTMLDivElement>(null);
  const cardRefs = useRef<Array<HTMLDivElement | null>>([]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{
    index: number;
    startY: number;
    pointerId: number;
    lifted: boolean;
    liftTimer: ReturnType<typeof setTimeout> | null;
    autoScroll: number;
  } | null>(null);

  const computeTarget = useCallback((clientY: number, fromIndex: number) => {
    const mids: number[] = [];
    cardRefs.current.forEach((el, i) => {
      if (!el || i === fromIndex) return;
      const r = el.getBoundingClientRect();
      mids.push(r.top + r.height / 2);
    });
    let target = 0;
    for (const mid of mids) if (clientY > mid) target += 1;
    return target;
  }, []);

  const endDrag = useCallback(() => {
    const d = dragRef.current;
    if (d?.liftTimer) clearTimeout(d.liftTimer);
    if (d?.autoScroll) cancelAnimationFrame(d.autoScroll);
    dragRef.current = null;
    setDrag(null);
  }, []);

  const startDrag = useCallback(
    (e: React.PointerEvent, index: number) => {
      e.preventDefault();
      const isTouch = e.pointerType === "touch";
      const startY = e.clientY;
      const d = {
        index,
        startY,
        pointerId: e.pointerId,
        lifted: !isTouch,
        liftTimer: null as ReturnType<typeof setTimeout> | null,
        autoScroll: 0,
      };
      dragRef.current = d;
      if (isTouch) {
        d.liftTimer = setTimeout(() => {
          if (dragRef.current === d) {
            d.lifted = true;
            setDrag({ index, dy: 0, targetIndex: index, lifted: true });
          }
        }, 200);
      } else {
        setDrag({ index, dy: 0, targetIndex: index, lifted: true });
      }

      const onMove = (ev: PointerEvent) => {
        if (dragRef.current !== d || ev.pointerId !== d.pointerId) return;
        if (!d.lifted) return;
        ev.preventDefault(); // scroll locked while dragging
        const dy = ev.clientY - d.startY;
        const target = computeTarget(ev.clientY, d.index);
        setDrag({ index: d.index, dy, targetIndex: target, lifted: true });
        // Edge auto-scroll inside the filmstrip scroller.
        const sc = scrollRef.current;
        if (sc) {
          const r = sc.getBoundingClientRect();
          if (ev.clientY < r.top + 56) sc.scrollTop -= 8;
          else if (ev.clientY > r.bottom - 56) sc.scrollTop += 8;
        }
      };
      const onUp = (ev: PointerEvent) => {
        if (ev.pointerId !== d.pointerId) return;
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        window.removeEventListener("pointercancel", onUp);
        if (dragRef.current === d && d.lifted) {
          const target = computeTarget(ev.clientY, d.index);
          if (target !== d.index) {
            dispatch({ type: "REORDER", from: d.index, to: target });
            setAnnouncement(`Slot ${d.index + 1} moved to position ${target + 1}`);
          }
        }
        endDrag();
      };
      window.addEventListener("pointermove", onMove, { passive: false });
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onUp);
    },
    [computeTarget, endDrag],
  );

  // ── Submit / reset ──
  const buildPayload = useCallback((): TimelineEditSlotPayload[] => {
    return state.slots.map((s) => ({
      slot_id: s.slotId,
      clip_index: s.clipIndex,
      in_s: Math.round(s.inS * 1000) / 1000,
      duration_beats: isNoGrid ? null : s.durationBeats,
      duration_s: isNoGrid ? s.durationS : null,
      removed: s.removed,
    }));
  }, [state.slots, isNoGrid]);

  const findOffendingSlot = useCallback(
    (code: string): string | null => {
      if (code === "TIMELINE_OUT_OF_BOUNDS") {
        const idx = state.slots.findIndex((s, i) => {
          if (s.removed) return false;
          const src =
            clipByIndex.get(s.clipIndex)?.duration_s ??
            state.clipDurations[s.clipIndex];
          return src != null && s.inS + windows[i].durationS > src + 1e-3;
        });
        return idx >= 0 ? state.slots[idx].key : null;
      }
      if (code === "TIMELINE_UNKNOWN_CLIP") {
        const bad = state.slots.find((s) => !clipByIndex.has(s.clipIndex));
        return bad?.key ?? null;
      }
      return null;
    },
    [state.slots, state.clipDurations, clipByIndex, windows],
  );

  const handleSubmit = useCallback(async () => {
    setSubmitting(true);
    setSubmitError(null);
    setBusyNotice(false);
    try {
      await editTimeline(jobId, variantId, buildPayload());
      onRenderEnqueued();
    } catch (e) {
      if (e instanceof TimelineApiError) {
        if (e.code === "TIMELINE_STALE") {
          setStale(true); // BLOCKING — never auto-refetch over a draft
        } else if (e.code === "JOB_BUSY" || e.status === 409) {
          setBusyNotice(true);
        } else if (e.code) {
          setSubmitError({ code: e.code, slotKey: findOffendingSlot(e.code) });
        } else {
          setSubmitError({ code: "unknown", slotKey: null });
        }
      } else {
        setSubmitError({ code: "unknown", slotKey: null });
      }
    } finally {
      setSubmitting(false);
    }
  }, [jobId, variantId, buildPayload, onRenderEnqueued, findOffendingSlot]);

  const handleReset = useCallback(async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      await resetTimeline(jobId, variantId);
      onRenderEnqueued();
    } catch (e) {
      if (e instanceof TimelineApiError && e.code === "JOB_BUSY") setBusyNotice(true);
      else setSubmitError({ code: "unknown", slotKey: null });
    } finally {
      setSubmitting(false);
    }
  }, [jobId, variantId, onRenderEnqueued]);

  // CTA label
  const ctaLabel = submitting
    ? "Re-rendering…"
    : edits > 0
      ? `Re-render ${edits} ${edits === 1 ? "edit" : "edits"} · ~2 min`
      : "Re-render · ~2 min";
  const showReset = timeline.has_user_edits || dirty;

  const totalS = totalDurationS(state.slots, state.grid);

  return (
    <SheetFrame onRequestClose={requestClose} dirty>
      <SheetHeader
        onClose={requestClose}
        onUndo={handleUndo}
        onRedo={handleRedo}
        canUndo={state.past.length > 0}
        canRedo={state.future.length > 0}
      />

      {/* SR announcements for reorder / undo */}
      <div aria-live="polite" className="sr-only">
        {announcement}
      </div>

      {/* Blocking stale card */}
      {stale && (
        <div className="absolute inset-0 z-20 flex items-center justify-center bg-[#fafaf8]/95 px-6">
          <div className="w-full max-w-[380px] rounded-2xl border border-zinc-200 bg-white p-6 shadow-sm">
            <p className="text-sm leading-relaxed text-[#3f3f46]">
              This cut changed since you opened the editor. Your edits no longer
              apply.
            </p>
            <button
              onClick={reload}
              className="mt-5 rounded-full bg-[#0c0c0e] px-6 py-2.5 text-sm font-semibold text-white hover:opacity-80"
            >
              Reload editor
            </button>
          </div>
        </div>
      )}

      {/* Filmstrip */}
      <div ref={scrollRef} className="relative flex-1 overflow-y-auto px-5 pb-6">
        {busyNotice && (
          <p className="mb-3 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm text-[#3f3f46]">
            Another variant is rendering — try again in a minute.
          </p>
        )}
        {submitError && submitError.slotKey == null && (
          <p className="mb-3 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm text-[#3f3f46]">
            {SUBMIT_ERROR_COPY[submitError.code] ??
              "That didn't save — try again."}
          </p>
        )}

        <div ref={listRef} role="list" className="space-y-3">
          {state.slots.map((slot, i) => {
            const isDraggedCard = drag?.lifted && drag.index === i;
            if (slot.removed) {
              return (
                <div key={slot.key} role="listitem">
                  <RemovedSlotCard
                    cardRef={(el) => {
                      cardRefs.current[i] = el;
                      if (i === 0) {
                        (firstCardRef as React.MutableRefObject<HTMLDivElement | null>).current = el;
                      }
                    }}
                    slot={slot}
                    index={i}
                    onRestore={() => dispatch({ type: "RESTORE", key: slot.key })}
                  />
                </div>
              );
            }
            return (
              <div key={slot.key} role="listitem">
                {drag?.lifted && drag.targetIndex === i && drag.index > i && (
                  <div className="mb-1 h-[2px] w-full bg-lime-600" aria-hidden="true" />
                )}
                <SlotCard
                  cardRef={(el) => {
                    cardRefs.current[i] = el;
                    if (i === 0) {
                      (firstCardRef as React.MutableRefObject<HTMLDivElement | null>).current = el;
                    }
                  }}
                  slot={slot}
                  index={i}
                  count={state.slots.length}
                  window={windows[i]}
                  grid={state.grid}
                  isNoGrid={isNoGrid}
                  clip={clipByIndex.get(slot.clipIndex)}
                  poster={posters[slot.clipIndex]}
                  selected={selectedKey === slot.key}
                  onSelect={() =>
                    setSelectedKey((k) => (k === slot.key ? null : slot.key))
                  }
                  dispatch={dispatch}
                  clampNonce={state.clampNonce}
                  clampedKey={state.clampedKey}
                  inlineError={
                    submitError?.slotKey === slot.key
                      ? (SUBMIT_ERROR_COPY[submitError.code] ?? "This slot didn't save.")
                      : null
                  }
                  onMoveUp={() => moveSlot(i, i - 1)}
                  onMoveDown={() => moveSlot(i, i + 1)}
                  onDragStart={(e) => startDrag(e, i)}
                  dragStyle={
                    isDraggedCard
                      ? { transform: `translateY(${drag.dy}px) scale(1.02)` }
                      : undefined
                  }
                  isDragged={!!isDraggedCard}
                  clips={timeline.clips}
                  posters={posters}
                  canAddBeats={
                    isNoGrid
                      ? totalS + 0.5 <= MAX_TOTAL_SECONDS
                      : remainingBeats(state.slots, state.grid) >= 1
                  }
                />
                {drag?.lifted && drag.targetIndex === i && drag.index < i && (
                  <div className="mt-1 h-[2px] w-full bg-lime-600" aria-hidden="true" />
                )}
              </div>
            );
          })}

          {/* Add clip */}
          <AddClipCard
            clips={timeline.clips}
            posters={posters}
            disabled={
              isNoGrid
                ? totalS + SECONDS_FLOOR > MAX_TOTAL_SECONDS
                : remainingBeats(state.slots, state.grid) < 1
            }
            onAdd={(clipIndex) => {
              dispatch({ type: "ADD", clipIndex });
              setAnnouncement("Clip added to the end of the cut");
            }}
          />
        </div>
      </div>

      {/* Sticky bottom bar */}
      <div className="sticky bottom-0 border-t border-zinc-200 bg-[#fafaf8]/95 px-5 py-4 backdrop-blur-sm">
        <div className="flex items-center justify-between gap-4">
          {showReset ? (
            <button
              onClick={handleReset}
              disabled={submitting}
              className="text-sm text-[#71717a] hover:underline underline-offset-4 disabled:opacity-40"
            >
              Reset to AI cut
            </button>
          ) : (
            <span className="text-xs text-[#a1a1aa] tabular-nums">
              {formatTimecode(totalS)} total
            </span>
          )}
          <button
            onClick={handleSubmit}
            disabled={submitting || !dirty || liveSlots.length === 0}
            className="rounded-full bg-[#0c0c0e] px-6 py-3 text-sm font-semibold text-white hover:opacity-80 disabled:bg-zinc-700 disabled:opacity-100 disabled:hover:opacity-100"
          >
            {ctaLabel}
          </button>
        </div>
        {liveSlots.length === 0 && (
          <p className="mt-2 text-xs text-[#71717a]">
            The cut needs at least one clip — restore one to re-render.
          </p>
        )}
      </div>

      <ConfirmDialog
        open={confirmClose}
        question="Discard your clip edits?"
        detail="You haven't re-rendered this cut — closing the editor loses these changes."
        confirmLabel="Discard edits"
        cancelLabel="Keep editing"
        onConfirm={() => {
          setConfirmClose(false);
          onClose();
        }}
        onCancel={() => setConfirmClose(false)}
      />
    </SheetFrame>
  );
}

// ── Slot card ─────────────────────────────────────────────────────────────────

function SlotCard({
  cardRef,
  slot,
  index,
  count,
  window: win,
  grid,
  isNoGrid,
  clip,
  poster,
  selected,
  onSelect,
  dispatch,
  clampNonce,
  clampedKey,
  inlineError,
  onMoveUp,
  onMoveDown,
  onDragStart,
  dragStyle,
  isDragged,
  clips,
  posters,
  canAddBeats,
}: {
  cardRef: (el: HTMLDivElement | null) => void;
  slot: DraftSlot;
  index: number;
  count: number;
  window: SlotWindow;
  grid: number[];
  isNoGrid: boolean;
  clip: TimelineClip | undefined;
  poster: string | "failed" | undefined;
  selected: boolean;
  onSelect: () => void;
  dispatch: Dispatch<EditorAction>;
  clampNonce: number;
  clampedKey: string | null;
  inlineError: string | null;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onDragStart: (e: React.PointerEvent) => void;
  dragStyle?: React.CSSProperties;
  isDragged: boolean;
  clips: TimelineClip[];
  posters: Record<number, string | "failed" | undefined>;
  canAddBeats: boolean;
}) {
  const [whyOpen, setWhyOpen] = useState(false);
  const [swapOpen, setSwapOpen] = useState(false);
  const [chipFlash, setChipFlash] = useState(false);
  const lastClampNonce = useRef(clampNonce);

  // Chip flash when a clamp hit THIS slot.
  useEffect(() => {
    if (clampNonce !== lastClampNonce.current) {
      lastClampNonce.current = clampNonce;
      if (clampedKey === slot.key) {
        setChipFlash(true);
        const t = setTimeout(() => setChipFlash(false), 600);
        return () => clearTimeout(t);
      }
    }
  }, [clampNonce, clampedKey, slot.key]);

  const sourceDur = clip?.duration_s ?? null;
  const startS = win.startS ?? 0;
  const endS = startS + win.durationS;

  const eyebrowParts = [
    `Slot ${index + 1}`,
    `${formatTimecode(startS)}–${formatTimecode(endS)}`,
  ];
  if (!isNoGrid) eyebrowParts.push(`${slot.durationBeats ?? 0} beats`);

  const chipText = isNoGrid
    ? formatSeconds(slot.durationS ?? 0)
    : `${slot.durationBeats ?? 0} ${slot.durationBeats === 1 ? "beat" : "beats"} · ${formatSeconds(win.durationS)}`;

  return (
    <div
      ref={cardRef}
      tabIndex={0}
      style={dragStyle}
      onKeyDown={(e) => {
        if ((e.key === "Enter" || e.key === " ") && e.target === e.currentTarget) {
          e.preventDefault();
          onSelect();
        }
      }}
      className={[
        "rounded-2xl border bg-white shadow-sm motion-safe:transition-shadow",
        selected ? "border-zinc-200 outline outline-2 outline-lime-500" : "border-zinc-200",
        isDragged ? "relative z-10 shadow-[0_12px_30px_rgba(0,0,0,0.18)]" : "",
      ].join(" ")}
    >
      {/* Collapsed row */}
      <div className="flex items-center gap-3 p-3">
        {/* Poster */}
        <button
          onClick={onSelect}
          aria-label={`Slot ${index + 1}: ${selected ? "collapse" : "expand"}`}
          className="shrink-0"
        >
          {poster && poster !== "failed" ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={poster}
              alt=""
              className="aspect-[9/16] w-12 rounded-[10px] object-cover"
            />
          ) : (
            <div className="flex aspect-[9/16] w-12 items-center justify-center rounded-[10px] border border-dashed border-zinc-300 bg-zinc-50">
              <span className="text-[10px] text-zinc-400">{index + 1}</span>
            </div>
          )}
        </button>

        {/* Eyebrow + chip */}
        <button onClick={onSelect} className="min-w-0 flex-1 text-left">
          <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-lime-700">
            {eyebrowParts.join(" · ")}
          </p>
          <span
            className={[
              "mt-1.5 inline-block rounded-full border border-lime-200 bg-lime-50 px-2 py-0.5 text-[11px] text-lime-800",
              chipFlash ? "motion-safe:animate-pulse border-lime-500" : "",
            ].join(" ")}
          >
            {chipText}
          </span>
          {slot.momentDescription && !selected && (
            <p className="mt-1 truncate text-xs text-[#a1a1aa]">
              {slot.momentDescription}
            </p>
          )}
        </button>

        {/* Move buttons (keyboard/SR path) + drag handle */}
        <div className="flex shrink-0 items-center">
          <button
            onClick={onMoveUp}
            disabled={index === 0}
            aria-label={`Move slot ${index + 1} up`}
            className="flex h-11 w-8 items-center justify-center text-[#71717a] hover:text-[#0c0c0e] disabled:opacity-25"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <path d="M12 19V5M5 12l7-7 7 7" />
            </svg>
          </button>
          <button
            onClick={onMoveDown}
            disabled={index === count - 1}
            aria-label={`Move slot ${index + 1} down`}
            className="flex h-11 w-8 items-center justify-center text-[#71717a] hover:text-[#0c0c0e] disabled:opacity-25"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <path d="M12 5v14M19 12l-7 7-7-7" />
            </svg>
          </button>
          <button
            onPointerDown={onDragStart}
            aria-label={`Drag to reorder slot ${index + 1}`}
            className="flex h-11 w-11 cursor-grab touch-none items-center justify-center text-[#a1a1aa] hover:text-[#3f3f46] active:cursor-grabbing"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <circle cx="9" cy="6" r="1.5" /><circle cx="15" cy="6" r="1.5" />
              <circle cx="9" cy="12" r="1.5" /><circle cx="15" cy="12" r="1.5" />
              <circle cx="9" cy="18" r="1.5" /><circle cx="15" cy="18" r="1.5" />
            </svg>
          </button>
        </div>
      </div>

      {inlineError && (
        <p className="px-3 pb-2 text-xs text-[#3f3f46]">{inlineError}</p>
      )}

      {/* Expanded controls */}
      {selected && (
        <div className="border-t border-zinc-100 p-3">
          <SlotPreview
            clip={clip}
            inS={slot.inS}
            windowS={win.durationS}
            slot={slot}
            startS={startS}
            endS={endS}
          />

          <InPointScrubber
            inS={slot.inS}
            windowS={win.durationS}
            sourceDurationS={sourceDur}
            grid={grid}
            offsetBeats={win.offsetBeats}
            durationBeats={slot.durationBeats}
            onChange={(inS, record) =>
              dispatch({ type: "SET_IN", key: slot.key, inS, record })
            }
          />

          {/* Duration steppers */}
          <div className="mt-3 flex items-center justify-between">
            <div className="flex items-center overflow-hidden rounded-full border border-zinc-200">
              <button
                onClick={() => dispatch({ type: "NUDGE", key: slot.key, delta: -1 })}
                disabled={
                  isNoGrid
                    ? (slot.durationS ?? 0) - 0.5 < SECONDS_FLOOR
                    : (slot.durationBeats ?? 0) <= 1
                }
                aria-label="Shorter"
                className="flex h-11 w-11 items-center justify-center text-[#3f3f46] hover:bg-zinc-50 disabled:opacity-25"
              >
                −
              </button>
              <span className="min-w-[72px] select-none border-x border-zinc-200 px-2 text-center text-xs tabular-nums text-[#3f3f46]">
                {isNoGrid
                  ? formatSeconds(slot.durationS ?? 0)
                  : `${slot.durationBeats ?? 0} beats`}
              </span>
              <button
                onClick={() => dispatch({ type: "NUDGE", key: slot.key, delta: 1 })}
                disabled={!canAddBeats}
                aria-label="Longer"
                className="flex h-11 w-11 items-center justify-center text-[#3f3f46] hover:bg-zinc-50 disabled:opacity-25"
              >
                +
              </button>
            </div>

            <div className="flex items-center gap-3">
              {slot.momentDescription && (
                <div className="relative">
                  <button
                    onClick={() => setWhyOpen((o) => !o)}
                    aria-expanded={whyOpen}
                    className="py-2 text-xs text-[#71717a] underline-offset-4 hover:underline"
                  >
                    Why this moment
                  </button>
                  {whyOpen && (
                    <div className="absolute bottom-full right-0 z-10 mb-2 w-60 rounded-lg border border-zinc-200 bg-white p-3 text-sm text-zinc-600 shadow-sm">
                      {slot.momentDescription}
                    </div>
                  )}
                </div>
              )}
              <button
                onClick={() => setSwapOpen((o) => !o)}
                aria-expanded={swapOpen}
                className="py-2 text-xs text-[#3f3f46] underline-offset-4 hover:underline"
              >
                Swap clip
              </button>
              <button
                onClick={() => dispatch({ type: "REMOVE", key: slot.key })}
                className="py-2 text-xs text-[#71717a] underline-offset-4 hover:underline"
              >
                Remove
              </button>
            </div>
          </div>

          {isNoGrid && (
            <p className="mt-2 text-[11px] text-zinc-500">
              This video uses the clips&apos; own audio — trims cut the audio too.
            </p>
          )}

          {swapOpen && (
            <ClipPicker
              clips={clips}
              posters={posters}
              currentClipIndex={slot.clipIndex}
              onPick={(clipIndex) => {
                dispatch({ type: "SWAP", key: slot.key, clipIndex });
                setSwapOpen(false);
              }}
            />
          )}
        </div>
      )}
    </div>
  );
}

// ── Removed-slot card (separate so the main card stays readable) ──────────────

function RemovedSlotCard({
  cardRef,
  slot,
  index,
  onRestore,
}: {
  cardRef: (el: HTMLDivElement | null) => void;
  slot: DraftSlot;
  index: number;
  onRestore: () => void;
}) {
  return (
    <div
      ref={cardRef}
      tabIndex={0}
      className="flex items-center justify-between gap-3 rounded-2xl border border-dashed border-zinc-300 px-4 py-3 text-zinc-600"
    >
      <p className="min-w-0 truncate text-sm">
        Slot {index + 1} removed
        {slot.momentDescription ? ` — ${slot.momentDescription}` : ""}
      </p>
      <button
        onClick={onRestore}
        className="shrink-0 py-2 text-sm text-[#3f3f46] underline underline-offset-4"
      >
        Restore
      </button>
    </div>
  );
}

// ── Expanded-card video preview (plays only the slot window, looped) ──────────

function SlotPreview({
  clip,
  inS,
  windowS,
  slot,
  startS,
  endS,
}: {
  clip: TimelineClip | undefined;
  inS: number;
  windowS: number;
  slot: DraftSlot;
  startS: number;
  endS: number;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [decodeFailed, setDecodeFailed] = useState(false);
  const windowRef = useRef({ inS, windowS });
  windowRef.current = { inS, windowS };

  // Live-seek when the in-point moves.
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    if (Math.abs(v.currentTime - inS) > 0.25) v.currentTime = inS;
  }, [inS]);

  if (!clip || decodeFailed) {
    // HDR/HEVC no-decode fallback (or missing clip): dashed no-frame card.
    return (
      <div className="mb-3 flex aspect-video w-full flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-zinc-300 bg-zinc-50 px-4 text-center">
        <p className="text-sm text-zinc-600">
          {slot.momentDescription ?? "This clip can't preview here"}
        </p>
        <p className="text-xs tabular-nums text-[#a1a1aa]">
          {formatTimecode(startS)}–{formatTimecode(endS)}
        </p>
      </div>
    );
  }

  return (
    <video
      ref={videoRef}
      src={clip.signed_url}
      muted
      autoPlay
      playsInline
      onError={() => setDecodeFailed(true)}
      onLoadedMetadata={() => {
        const v = videoRef.current;
        if (v) v.currentTime = windowRef.current.inS;
      }}
      onTimeUpdate={() => {
        const v = videoRef.current;
        if (!v) return;
        const { inS: start, windowS: dur } = windowRef.current;
        if (v.currentTime > start + dur || v.currentTime < start - 0.5) {
          v.currentTime = start; // loop the selected window only
        }
      }}
      className="mb-3 aspect-video w-full rounded-lg bg-zinc-100 object-contain"
    />
  );
}

// ── In-point scrubber ─────────────────────────────────────────────────────────

function InPointScrubber({
  inS,
  windowS,
  sourceDurationS,
  grid,
  offsetBeats,
  durationBeats,
  onChange,
}: {
  inS: number;
  windowS: number;
  sourceDurationS: number | null;
  grid: number[];
  offsetBeats: number | null;
  durationBeats: number | null;
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
      // Drag positions the WINDOW START across the whole source strip.
      return Math.min(maxIn, frac * src);
    },
    [inS, maxIn, src],
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
    [onChange, posToIn],
  );

  // Beat ticks: beats that fall INSIDE the current window, in source time.
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
        className="relative h-11 w-full cursor-ew-resize touch-none rounded-lg bg-zinc-100"
      >
        {/* Window fill */}
        <div
          className="absolute inset-y-1 rounded-md bg-lime-600/90"
          style={{
            left: `${(inS / src) * 100}%`,
            width: `${Math.min(100, (windowS / src) * 100)}%`,
          }}
        >
          {/* Handles */}
          <div className="absolute inset-y-0 left-0 w-1 rounded-l-md bg-lime-700" aria-hidden="true" />
          <div className="absolute inset-y-0 right-0 w-1 rounded-r-md bg-lime-700" aria-hidden="true" />
        </div>
        {/* Beat ticks */}
        {ticks.map((t, i) => (
          <div
            key={i}
            className="absolute inset-y-2 w-[2px] bg-lime-600"
            style={{ left: `${(t / src) * 100}%` }}
            aria-hidden="true"
          />
        ))}
      </div>
      <p className="mt-1 text-xs tabular-nums text-[#71717a]">
        Starts at {formatInPoint(inS)}
      </p>
    </div>
  );
}

// ── Clip picker (swap + add) ──────────────────────────────────────────────────

function ClipPicker({
  clips,
  posters,
  currentClipIndex,
  unusedOnly = false,
  onPick,
}: {
  clips: TimelineClip[];
  posters: Record<number, string | "failed" | undefined>;
  currentClipIndex?: number;
  unusedOnly?: boolean;
  onPick: (clipIndex: number) => void;
}) {
  const visible = unusedOnly ? clips.filter((c) => !c.used) : clips;
  if (visible.length === 0) {
    return (
      <p className="mt-3 text-sm text-zinc-600">
        Every uploaded clip is already in this cut.
      </p>
    );
  }
  return (
    <div className="mt-3 grid grid-cols-4 gap-2 sm:grid-cols-5">
      {visible.map((c) => {
        const poster = posters[c.clip_index];
        const isCurrent = c.clip_index === currentClipIndex;
        return (
          <button
            key={c.clip_index}
            onClick={() => onPick(c.clip_index)}
            disabled={isCurrent}
            aria-label={`Clip ${c.clip_index + 1}${c.used ? " (used)" : " (unused)"}`}
            className={[
              "relative overflow-hidden rounded-[10px] border",
              isCurrent
                ? "border-lime-500 opacity-60"
                : "border-zinc-200 hover:border-zinc-400",
            ].join(" ")}
          >
            {poster && poster !== "failed" ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={poster} alt="" className="aspect-[9/16] w-full object-cover" />
            ) : (
              <div className="flex aspect-[9/16] w-full items-center justify-center bg-zinc-50 text-[10px] text-zinc-400">
                {c.clip_index + 1}
              </div>
            )}
            <span
              className={[
                "absolute bottom-1 left-1 rounded-full px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide",
                c.used
                  ? "bg-lime-50 text-lime-800"
                  : "bg-white/90 text-[#71717a]",
              ].join(" ")}
            >
              {c.used ? "Used" : "Unused"}
            </span>
          </button>
        );
      })}
    </div>
  );
}

// ── Add-clip affordance ───────────────────────────────────────────────────────

function AddClipCard({
  clips,
  posters,
  disabled,
  onAdd,
}: {
  clips: TimelineClip[];
  posters: Record<number, string | "failed" | undefined>;
  disabled: boolean;
  onAdd: (clipIndex: number) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-2xl border border-dashed border-zinc-300 p-3">
      <button
        onClick={() => setOpen((o) => !o)}
        disabled={disabled}
        aria-expanded={open}
        className="w-full py-2 text-left text-sm text-[#3f3f46] underline-offset-4 hover:underline disabled:no-underline disabled:opacity-40"
      >
        + Add clip
      </button>
      {disabled && (
        <p className="text-xs text-[#a1a1aa]">
          No room left in this cut — shorten a slot first.
        </p>
      )}
      {open && !disabled && (
        <ClipPicker
          clips={clips}
          posters={posters}
          unusedOnly
          onPick={(clipIndex) => {
            onAdd(clipIndex);
            setOpen(false);
          }}
        />
      )}
    </div>
  );
}
