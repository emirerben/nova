"use client";

/**
 * Clip-timeline session state for one generative variant.
 *
 * Lifted into VariantTile (same rationale as useVariantEditSession): committing
 * a timeline edit flips the variant to "rendering" on the next poll, and the
 * re-render wait UI must survive that flip — so the state lives in the tile
 * that stays mounted for the variant's whole lifetime.
 *
 * Responsibilities:
 * - Lazy GET of the timeline on first tile render (gives the "Edit clips · N"
 *   count); 404 (old backend) or a non-transient editable:false reason hides
 *   the entry point entirely.
 * - Re-render wait after a commit/reset: ETA-band phase ("rendering"), then a
 *   receipt ("✓ Ready in m:ss") or a quiet failed tile with "Try again".
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  getTimeline,
  TimelineApiError,
  type GenerativeVariant,
  type TimelineResponse,
} from "@/lib/generative-api";

/** ~2 min baseline for a single-variant re-render (drives the ETA ladder). */
export const RERENDER_BASELINE_MS = 120_000;

export type TimelineWaitPhase = "idle" | "rendering" | "receipt" | "failed";

export interface TimelineSession {
  /** Cached GET result (count + has_user_edits for the card chrome). */
  timeline: TimelineResponse | null;
  /** True when the entry button should exist at all. */
  entryVisible: boolean;
  /** Non-removed slot count for the "Edit clips · N" label (null until loaded). */
  slotCount: number | null;
  hasUserEdits: boolean;
  isEditorOpen: boolean;
  openEditor: () => void;
  closeEditor: () => void;
  /** Called by the editor after a successful POST/DELETE. */
  onRenderEnqueued: () => void;
  /** Called by the editor when a commit changed has_user_edits server-side. */
  refetchTimeline: () => void;
  /** Re-render wait state (drives the VariantCard video well). */
  wait: {
    phase: TimelineWaitPhase;
    startedAtMs: number | null;
    elapsedMs: number | null;
  };
  /** True while a timeline-initiated render is unresolved — the tile hides the
   * generic render card and polls the job status. */
  isWaiting: boolean;
  dismissReceipt: () => void;
}

/** Reasons where the sheet itself explains the situation (entry stays visible).
 * Everything else (disabled / lyrics_sync / no_slot_timeline / voiceover_bed_fit
 * / no_timeline) is permanent for the variant → entry hidden. */
const TRANSIENT_REASONS = new Set(["sources_expired"]);

export function useTimelineSession(
  jobId: string | null,
  variant: GenerativeVariant,
  refresh: () => void,
): TimelineSession {
  const [timeline, setTimeline] = useState<TimelineResponse | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [isEditorOpen, setIsEditorOpen] = useState(false);
  const [phase, setPhase] = useState<TimelineWaitPhase>("idle");
  const [startedAtMs, setStartedAtMs] = useState<number | null>(null);
  const [elapsedMs, setElapsedMs] = useState<number | null>(null);
  const fetchedRef = useRef(false);
  const sawRenderingRef = useRef(false);

  const variantId = variant.variant_id;

  const fetchTimeline = useCallback(() => {
    if (!jobId) return;
    getTimeline(jobId, variantId)
      .then((t) => {
        setTimeline(t);
        setNotFound(false);
      })
      .catch((e) => {
        if (e instanceof TimelineApiError && e.status === 404) setNotFound(true);
        // Other failures: keep timeline null — entry hidden until a refetch.
      });
  }, [jobId, variantId]);

  // Lazy fetch once per tile lifetime (the count needs the GET; cache it).
  useEffect(() => {
    if (fetchedRef.current || !jobId) return;
    fetchedRef.current = true;
    fetchTimeline();
  }, [jobId, fetchTimeline]);

  // Refetch whenever the variant lands on "ready": the first lazy GET can race
  // the variant's initial render (timeline not written yet → editable:false),
  // and song swaps / retext renders rebuild the cut server-side. Cheap GET.
  const prevStatusRef = useRef<string | null>(variant.render_status);
  useEffect(() => {
    const prev = prevStatusRef.current;
    prevStatusRef.current = variant.render_status;
    if (variant.render_status === "ready" && prev !== "ready" && fetchedRef.current) {
      fetchTimeline();
    }
  }, [variant.render_status, fetchTimeline]);

  const onRenderEnqueued = useCallback(() => {
    setIsEditorOpen(false);
    sawRenderingRef.current = false;
    setPhase("rendering");
    setStartedAtMs(Date.now());
    setElapsedMs(null);
    refresh();
  }, [refresh]);

  // Resolve the wait by watching the polled variant status.
  const renderStatus = variant.render_status;
  useEffect(() => {
    if (phase !== "rendering") return;
    if (renderStatus === "rendering") {
      sawRenderingRef.current = true;
      return;
    }
    if (!sawRenderingRef.current) return; // stale-terminal race — keep waiting
    if (renderStatus === "ready") {
      setElapsedMs(startedAtMs != null ? Date.now() - startedAtMs : null);
      setPhase("receipt");
      fetchTimeline(); // pick up has_user_edits for the "Edited cut" pill
    } else if (renderStatus === "failed") {
      setPhase("failed");
    }
  }, [phase, renderStatus, startedAtMs, fetchTimeline]);

  const openEditor = useCallback(() => {
    if (phase === "failed") setPhase("idle"); // "Try again" path
    setIsEditorOpen(true);
  }, [phase]);

  const closeEditor = useCallback(() => setIsEditorOpen(false), []);
  const dismissReceipt = useCallback(() => setPhase("idle"), []);

  const slotCount = timeline
    ? timeline.slots.filter((s) => !s.removed).length
    : null;

  const entryVisible =
    !notFound &&
    timeline != null &&
    (timeline.editable ||
      (timeline.reason != null && TRANSIENT_REASONS.has(timeline.reason)));

  return {
    timeline,
    entryVisible,
    slotCount,
    hasUserEdits: timeline?.has_user_edits ?? false,
    isEditorOpen,
    openEditor,
    closeEditor,
    onRenderEnqueued,
    refetchTimeline: fetchTimeline,
    wait: { phase, startedAtMs, elapsedMs },
    isWaiting: phase === "rendering" || phase === "failed",
    dismissReceipt,
  };
}
