import type {
  EditorTransition,
  LookAdjustments,
  LookPreset,
  TimelineClip,
} from "@/lib/generative-api";
import { slotWindows, type DraftSlot } from "@/app/generative/timeline-math";
import { lookAdjustmentsEqual } from "@/lib/look-presets";
import { effectiveBoundaryDuration } from "@/lib/carousel-timing";

const EPSILON = 1e-6;
const roundMillis = (value: number) => Math.round(value * 1000) / 1000;

export interface VirtualTimelineEntry {
  kind: "clip";
  slotIndex: number;
  slotKey: string;
  clipIndex: number;
  startS: number;
  durationS: number;
  inS: number;
  sourceUrl: string | null;
  transitionAfter: EditorTransition;
  transitionDurationS: number | null;
  overlapBeforeS: number;
}

/**
 * A spliced Blossom-carousel moment (Lane C: staged/undoable editor block).
 * No `sourceUrl`/`slotKey`/etc — it isn't a source clip, it's a locally
 * rendered composite (mirrors the backend's synthetic
 * `__carousel_{variant_id}` clip, `_insert_carousel_moment_step` in
 * generative_build.py). The virtual-preview transport has no video deck for
 * this window; `useVirtualPreview` gates play/seek to a pause here and the
 * mounted preview component (`CarouselBlockPreview`, Lane B swaps its
 * internals) owns rendering it.
 */
export interface VirtualCarouselEntry {
  kind: "carousel";
  startS: number;
  durationS: number;
  overlapBeforeS: number;
}

export type VirtualEntry = VirtualTimelineEntry | VirtualCarouselEntry;

export interface VirtualTimeline {
  entries: VirtualEntry[];
  totalDurationS: number;
  hasMissingSource: boolean;
  /** Base/output mapping receipt for the one ripple-inserted Carousel. */
  carouselProjection: {
    baseInsertionS: number;
    downstreamShiftS: number;
  } | null;
  /** Baseline-output to draft-output mapping for trim/remove/reorder ripples. */
  segmentProjection?: Array<{
    baseStartS: number;
    baseEndS: number;
    draftStartS: number;
    draftEndS: number;
  }>;
}

type VirtualTimelineSegment = NonNullable<VirtualTimeline["segmentProjection"]>[number];

export interface VirtualTimeMapping {
  entry: VirtualEntry;
  entryIndex: number;
  virtualTimeS: number;
  localOffsetS: number;
  /** Position into the source clip's own timeline. `null` for a carousel
   * entry (no single source file to seek into) — callers must check
   * `entry.kind` before treating this as a video seek target. */
  sourceTimeS: number | null;
}

export interface ProjectedTimeRange {
  startS: number;
  endS: number;
}

export interface VirtualTransitionPreview {
  kind: Exclude<EditorTransition, "cut">;
  durationS: number;
  progress: number;
  carouselEntry?: VirtualCarouselEntry;
  carouselRole?: "incoming" | "outgoing";
}

export function virtualDeckLookPresetsAtTime(
  timeline: VirtualTimeline,
  slots: DraftSlot[],
  timeS: number,
  activeDeck: "a" | "b",
): Record<"a" | "b", LookPreset> {
  const result: Record<"a" | "b", LookPreset> = { a: "none", b: "none" };
  const mapping = mapVirtualTime(timeline, timeS);
  // A carousel block has no source clip / look-preset filter of its own —
  // the preview component owns that window's visuals entirely.
  if (!mapping || mapping.entry.kind !== "clip") return result;

  const transition = transitionPreviewAtTime(timeline, timeS);
  if (transition) {
    // mapVirtualTime is intentionally right-biased, so during an overlap it
    // resolves the incoming entry. The active video deck still owns the
    // outgoing source until the deck swap completes.
    const outgoing = timeline.entries[mapping.entryIndex - 1];
    const incoming = timeline.entries[mapping.entryIndex];
    if (outgoing?.kind === "clip") {
      result[activeDeck] = slots[outgoing.slotIndex]?.lookPreset ?? "none";
    }
    if (incoming && incoming.kind === "clip") {
      result[activeDeck === "a" ? "b" : "a"] =
        slots[incoming.slotIndex]?.lookPreset ?? "none";
    }
  } else {
    result[activeDeck] = slots[mapping.entry.slotIndex]?.lookPreset ?? "none";
  }
  return result;
}

export function virtualDeckLookAdjustmentsAtTime(
  timeline: VirtualTimeline,
  slots: DraftSlot[],
  currentTimeS: number,
  activeDeck: "a" | "b",
): Record<"a" | "b", LookAdjustments | null> {
  const result: Record<"a" | "b", LookAdjustments | null> = { a: null, b: null };
  const mapping = mapVirtualTime(timeline, currentTimeS);
  if (!mapping || mapping.entry.kind !== "clip") return result;
  const transition = transitionPreviewAtTime(timeline, currentTimeS);
  if (transition) {
    const outgoing = timeline.entries[mapping.entryIndex - 1];
    const incoming = timeline.entries[mapping.entryIndex];
    if (outgoing?.kind === "clip") {
      result[activeDeck] = slots[outgoing.slotIndex]?.lookAdjustments ?? null;
    }
    if (incoming && incoming.kind === "clip") {
      const incomingDeck = activeDeck === "a" ? "b" : "a";
      result[incomingDeck] = slots[incoming.slotIndex]?.lookAdjustments ?? null;
    }
  } else {
    result[activeDeck] = slots[mapping.entry.slotIndex]?.lookAdjustments ?? null;
  }
  return result;
}

export function mapVirtualTimeToMusicTime(
  virtualTimeS: number,
  sectionStartS: number,
): number {
  return Math.max(0, sectionStartS) + Math.max(0, virtualTimeS);
}

/** Position + duration of a staged carousel-moment block, as needed to splice
 * it into the virtual timeline. Mirrors `CarouselMoment.position` /
 * `duration_s` — the caller (EditorShell) resolves the effective staged/
 * persisted config down to this shape. */
export interface VirtualCarouselSplice {
  position: "intro" | "middle" | "outro";
  durationS: number;
  transitionIn?: "crossfade" | "none";
  transitionInDurationS?: number;
  transitionOut?: "crossfade" | "none";
  transitionOutDurationS?: number;
}

function inverseDraftTime(timeline: VirtualTimeline, draftTimeS: number): number {
  const safe = Math.max(0, draftTimeS);
  const segments = timeline.segmentProjection ?? [];
  // Prefer the segment that starts at a shared boundary. This is the same
  // right-biased convention used by mapVirtualTime and ensures a trimmed
  // segment's collapsed tail maps to the following baseline segment rather
  // than stopping at an arbitrary interior offset.
  let strictMatch: VirtualTimelineSegment | undefined;
  let inclusiveMatch: VirtualTimelineSegment | undefined;
  for (const candidate of segments) {
    if (
      safe < candidate.draftStartS - EPSILON ||
      safe > candidate.draftEndS + EPSILON
    ) {
      continue;
    }
    if (!inclusiveMatch || candidate.draftStartS > inclusiveMatch.draftStartS) {
      inclusiveMatch = candidate;
    }
    if (
      safe < candidate.draftEndS - EPSILON &&
      (!strictMatch || candidate.draftStartS > strictMatch.draftStartS)
    ) {
      strictMatch = candidate;
    }
  }
  const segment = strictMatch ?? inclusiveMatch;
  return segment
    ? segment.baseStartS +
        Math.min(
          segment.baseEndS - segment.baseStartS,
          Math.max(0, safe - segment.draftStartS),
        )
    : safe;
}

function outputToDraftTime(timeline: VirtualTimeline, outputTimeS: number): number {
  const safe = Math.max(0, outputTimeS);
  const carousel = timeline.entries.find((entry) => entry.kind === "carousel");
  const projection = timeline.carouselProjection;
  if (!carousel || !projection) {
    return safe;
  }

  const downstreamStartS = projection.baseInsertionS + projection.downstreamShiftS;
  if (safe < carousel.startS - EPSILON) {
    return safe;
  }
  if (safe < downstreamStartS - EPSILON) {
    return projection.baseInsertionS;
  }
  return Math.max(0, safe - projection.downstreamShiftS);
}

export function buildVirtualTimeline(
  slots: DraftSlot[],
  clips: Pick<TimelineClip, "clip_index" | "signed_url">[],
  grid: number[] = [],
  carousel?: VirtualCarouselSplice | null,
  baselineSlots: DraftSlot[] = slots,
): VirtualTimeline {
  const clipUrlByIndex = new Map(clips.map((clip) => [clip.clip_index, clip.signed_url]));
  const windows = slotWindows(slots, grid);
  const baselineWindows = slotWindows(baselineSlots, grid);
  const clipEntries: VirtualTimelineEntry[] = [];

  slots.forEach((slot, slotIndex) => {
    const window = windows[slotIndex];
    if (slot.removed || !window || window.startS == null || window.durationS <= 0) {
      return;
    }
    const previous = clipEntries.at(-1);
    const overlapBeforeS = previous
      ? Math.round(
          Math.max(0, previous.startS + previous.durationS - window.startS) * 1000,
        ) / 1000
      : 0;
    clipEntries.push({
      kind: "clip",
      slotIndex,
      slotKey: slot.key,
      clipIndex: slot.clipIndex,
      startS: window.startS,
      durationS: window.durationS,
      inS: Math.max(0, slot.inS),
      sourceUrl: clipUrlByIndex.get(slot.clipIndex) ?? null,
      transitionAfter: slot.transitionAfter ?? "cut",
      transitionDurationS: slot.transitionDurationS ?? null,
      overlapBeforeS,
    });
  });

  let entries: VirtualEntry[] = clipEntries;
  let carouselProjection: VirtualTimeline["carouselProjection"] = null;

  if (carousel && carousel.durationS > 0) {
    // Mirror `_insert_carousel_moment_step`'s position resolution
    // (app/tasks/generative_build.py), verbatim:
    //
    //   position = moment_cfg.get("position", "intro")
    //   new_steps = list(steps)
    //   if position == "middle":
    //       insertion_index = len(new_steps) // 2
    //       new_steps.insert(insertion_index, moment_step)
    //   elif position == "outro":
    //       insertion_index = len(new_steps)
    //       new_steps.append(moment_step)
    //   else:  # "intro" (default) and any unrecognized value
    //       insertion_index = 0
    //       new_steps.insert(0, moment_step)
    //
    // `steps` there is the per-clip AssemblyStep list BEFORE insertion — the
    // direct analog of `clipEntries` here (both are the ordered, already
    // removed-filtered list of real clips the montage assembles). So:
    //   "intro"  -> index 0 (before every clip)
    //   "outro"  -> index n (after every clip, i.e. appended)
    //   "middle" -> index floor(n / 2) — inserted BEFORE the clip currently
    //     sitting at that 0-based index. For n=4 that's before the 3rd clip
    //     (1-based): 2 clips before the block, 2 after. For n=5 it's still
    //     before the 3rd clip: 2 before, 3 after — floor division always
    //     leaves any odd remainder AFTER the block, never before it.
    const n = clipEntries.length;
    const insertionIndex =
      carousel.position === "middle"
        ? Math.floor(n / 2)
        : carousel.position === "outro"
          ? n
          : 0; // "intro" (default) and any unrecognized value
    const before = insertionIndex > 0 ? clipEntries[insertionIndex - 1] : null;
    const after = insertionIndex < n ? clipEntries[insertionIndex] : null;
    const oldBoundaryOverlapS = after?.overlapBeforeS ?? 0;
    const incomingOverlapS =
      before && carousel.transitionIn === "crossfade"
        ? effectiveBoundaryDuration(
            carousel.transitionInDurationS,
            before.durationS,
            carousel.durationS,
          )
        : 0;
    const outgoingOverlapS =
      after && carousel.transitionOut === "crossfade"
        ? effectiveBoundaryDuration(
            carousel.transitionOutDurationS,
            carousel.durationS,
            after.durationS,
          )
        : 0;
    const carouselStartS = roundMillis(
      before ? before.startS + before.durationS - incomingOverlapS : 0,
    );
    const baseInsertionS = roundMillis(
      after?.startS ?? (before ? before.startS + before.durationS : 0),
    );
    const downstreamShiftS = roundMillis(
      after
        ? carouselStartS + carousel.durationS - outgoingOverlapS - after.startS
        : carousel.durationS - incomingOverlapS,
    );
    const carouselEntry: VirtualCarouselEntry = {
      kind: "carousel",
      startS: carouselStartS,
      durationS: carousel.durationS,
      overlapBeforeS: incomingOverlapS,
    };
    // Re-home the replaced clip-to-clip overlap onto the configured Carousel
    // boundaries. Downstream clips shift by the net inserted output duration,
    // including both boundary overlaps.
    const shifted: VirtualEntry[] = clipEntries.map((entry, index) =>
      index >= insertionIndex
        ? {
            ...entry,
            startS: roundMillis(entry.startS + downstreamShiftS),
            ...(index === insertionIndex ? { overlapBeforeS: outgoingOverlapS } : {}),
          }
        : entry,
    );
    shifted.splice(insertionIndex, 0, carouselEntry);
    entries = shifted;
    carouselProjection = { baseInsertionS, downstreamShiftS };
  }

  const draftEntryByKey = new Map(
    clipEntries.map((entry) => [entry.slotKey, entry] as const),
  );
  const segmentProjection = baselineSlots.flatMap((slot, index) => {
    const base = baselineWindows[index];
    const draft = draftEntryByKey.get(slot.key);
    if (!base || base.startS == null || base.durationS <= 0 || !draft) return [];
    return [{
      baseStartS: base.startS,
      baseEndS: base.startS + base.durationS,
      draftStartS: draft.startS,
      draftEndS: draft.startS + draft.durationS,
    }];
  });

  const last = entries.at(-1);

  return {
    entries,
    totalDurationS: last ? roundMillis(last.startS + last.durationS) : 0,
    hasMissingSource: clipEntries.some((entry) => !entry.sourceUrl),
    carouselProjection,
    segmentProjection,
  };
}

/**
 * Map a timestamp authored on the clip-only/base timeline onto the assembled
 * output timeline. Carousel is an inserted sequence block, so everything at
 * or after its insertion boundary ripples later. Music deliberately does not
 * use this mapping; it remains an output-clock bed and plays continuously.
 */
export function projectBaseTime(
  timeline: VirtualTimeline,
  baseTimeS: number,
  boundary: "before" | "after" = "after",
): number {
  const safe = Math.max(0, baseTimeS);
  const segments = timeline.segmentProjection ?? [];
  const segment = segments.find(
    (candidate) => safe >= candidate.baseStartS - EPSILON && safe <= candidate.baseEndS + EPSILON,
  );
  const nextSegment = segments.find((candidate) => safe < candidate.baseStartS - EPSILON);
  const previousSegment = [...segments].reverse().find((candidate) => safe > candidate.baseEndS + EPSILON);
  const projectedSegment = segment
    ? segment.draftStartS + Math.min(segment.draftEndS - segment.draftStartS, Math.max(0, safe - segment.baseStartS))
    : nextSegment
      ? nextSegment.draftStartS + Math.max(0, safe - nextSegment.baseStartS)
      : previousSegment
        ? previousSegment.draftEndS + Math.max(0, safe - previousSegment.baseEndS)
        : safe;
  const projection = timeline.carouselProjection;
  if (!projection) return roundMillis(projectedSegment);
  const shouldRipple =
    boundary === "after"
      ? projectedSegment >= projection.baseInsertionS - EPSILON
      : projectedSegment > projection.baseInsertionS + EPSILON;
  return roundMillis(projectedSegment + (shouldRipple ? projection.downstreamShiftS : 0));
}

/** Map an authored interval. A range crossing the insertion point stretches
 * across the Carousel, matching ripple-insert behavior on regular tracks. */
export function projectBaseRange(
  timeline: VirtualTimeline,
  range: ProjectedTimeRange,
): ProjectedTimeRange {
  const startS = projectBaseTime(timeline, range.startS, "after");
  const endS = projectBaseTime(timeline, range.endS, "after");
  // Reordering makes the baseline -> draft map piecewise rather than
  // monotonic. A UI range still needs an ordered envelope or it renders as a
  // negative-width bar (`start_s > end_s`).
  return startS <= endS ? { startS, endS } : { startS: endS, endS: startS };
}

/** Inverse used by editor gestures: first remove the output-only Carousel
 * ripple, then map the draft clock back to the baseline clock. Output time
 * inside the inserted block resolves to its draft insertion boundary. */
export function unprojectOutputTime(timeline: VirtualTimeline, outputTimeS: number): number {
  return roundMillis(inverseDraftTime(timeline, outputToDraftTime(timeline, outputTimeS)));
}

/** Invert an editor range without allowing a drag wholly inside the inserted
 * Carousel window to collapse to zero length. Such ranges snap to the nearer
 * side of the insertion boundary and preserve their output duration. */
export function unprojectOutputRange(
  timeline: VirtualTimeline,
  range: ProjectedTimeRange,
): ProjectedTimeRange {
  const startS = unprojectOutputTime(timeline, range.startS);
  const endS = unprojectOutputTime(timeline, range.endS);
  if (endS > startS + EPSILON) return { startS, endS };
  if (startS > endS + EPSILON) return { startS: endS, endS: startS };
  const carousel = timeline.entries.find((entry) => entry.kind === "carousel");
  const projection = timeline.carouselProjection;
  const durationS = Math.max(0, range.endS - range.startS);
  if (!carousel || !projection || durationS <= EPSILON) return { startS, endS };
  const midpointS = (range.startS + range.endS) / 2;
  const carouselMidpointS = carousel.startS + carousel.durationS / 2;
  const baseInsertionS = inverseDraftTime(timeline, projection.baseInsertionS);
  return midpointS < carouselMidpointS
    ? {
        startS: roundMillis(Math.max(0, baseInsertionS - durationS)),
        endS: baseInsertionS,
      }
    : {
        startS: baseInsertionS,
        endS: roundMillis(baseInsertionS + durationS),
      };
}

/**
 * Preview the same render-safe boundary contract used by FFmpeg: at most
 * 300ms and at most 30% of either adjacent active clip. The next deck is
 * already preloaded, so the canvas can blend it under the outgoing deck.
 */
export function transitionPreviewAtTime(
  timeline: VirtualTimeline,
  timeS: number,
): VirtualTransitionPreview | null {
  for (let index = 0; index < timeline.entries.length - 1; index += 1) {
    const entry = timeline.entries[index];
    const next = timeline.entries[index + 1];
    const carouselEntry = entry.kind === "carousel" ? entry : next.kind === "carousel" ? next : null;
    const durationS = carouselEntry
      ? next.overlapBeforeS
      : Math.min(
          0.3,
          entry.kind === "clip" ? (entry.transitionDurationS ?? 0.3) : 0.3,
          entry.durationS * 0.3,
          next.durationS * 0.3,
        );
    if (!carouselEntry && entry.kind === "clip" && entry.transitionAfter === "cut") continue;
    if (durationS < 0.1) continue;
    const boundaryS = entry.startS + entry.durationS;
    const startS = next.startS;
    if (timeS < startS || timeS >= boundaryS) continue;
    return {
      kind:
        carouselEntry || entry.kind !== "clip" || entry.transitionAfter === "cut"
          ? "crossfade"
          : entry.transitionAfter,
      durationS,
      progress: Math.max(0, Math.min(1, (timeS - startS) / durationS)),
      ...(carouselEntry
        ? {
            carouselEntry,
            carouselRole: next.kind === "carousel" ? ("incoming" as const) : ("outgoing" as const),
          }
        : {}),
    };
  }
  return null;
}

export function mapVirtualTime(
  timeline: VirtualTimeline,
  timeS: number,
): VirtualTimeMapping | null {
  if (timeline.entries.length === 0 || timeline.totalDurationS <= 0) return null;

  const virtualTimeS = Math.max(0, Math.min(timeline.totalDurationS, timeS));
  const endIndex = timeline.entries.length - 1;

  // Walk from the end so an overlapped transition interval belongs to the
  // incoming segment. This also makes exact shared boundaries right-biased,
  // matching the story compiler and inverse scrub contract.
  for (let i = endIndex; i >= 0; i -= 1) {
    const entry = timeline.entries[i];
    const endS = entry.startS + entry.durationS;
    const contains =
      virtualTimeS >= entry.startS - EPSILON &&
      (virtualTimeS < endS - EPSILON || (i === endIndex && virtualTimeS <= endS + EPSILON));
    if (!contains) continue;

    const localOffsetS = Math.max(0, Math.min(entry.durationS, virtualTimeS - entry.startS));
    return {
      entry,
      entryIndex: i,
      virtualTimeS,
      localOffsetS,
      sourceTimeS: entry.kind === "clip" ? entry.inS + localOffsetS : null,
    };
  }

  const last = timeline.entries[endIndex];
  return {
    entry: last,
    entryIndex: endIndex,
    virtualTimeS: timeline.totalDurationS,
    localOffsetS: last.durationS,
    sourceTimeS: last.kind === "clip" ? last.inS + last.durationS : null,
  };
}

export function nextVirtualEntry(
  timeline: VirtualTimeline,
  entryIndex: number,
): VirtualEntry | null {
  return timeline.entries[entryIndex + 1] ?? null;
}

export function slotsDifferFromBaseline(
  baseline: DraftSlot[],
  slots: DraftSlot[],
): boolean {
  if (baseline.length !== slots.length) return true;
  for (let i = 0; i < slots.length; i += 1) {
    const a = baseline[i];
    const b = slots[i];
    if (
      a.key !== b.key ||
      a.slotId !== b.slotId ||
      a.clipIndex !== b.clipIndex ||
      Math.abs(a.inS - b.inS) > EPSILON ||
      Math.abs((a.durationS ?? 0) - (b.durationS ?? 0)) > EPSILON ||
      a.durationBeats !== b.durationBeats ||
      a.removed !== b.removed ||
      (a.lookPreset ?? "none") !== (b.lookPreset ?? "none") ||
      !lookAdjustmentsEqual(a.lookAdjustments, b.lookAdjustments) ||
      (a.transitionAfter ?? "cut") !== (b.transitionAfter ?? "cut") ||
      Math.abs((a.transitionDurationS ?? 0) - (b.transitionDurationS ?? 0)) > EPSILON
    ) {
      return true;
    }
  }
  return false;
}
