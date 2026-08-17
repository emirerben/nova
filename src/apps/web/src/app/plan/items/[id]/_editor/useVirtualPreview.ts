"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import type { TimelineClip } from "@/lib/generative-api";
import type { DraftSlot } from "@/app/generative/timeline-math";
import {
  buildVirtualTimeline,
  mapVirtualTimeToMusicTime,
  mapVirtualTime,
  nextVirtualEntry,
  transitionPreviewAtTime,
  type VirtualCarouselSplice,
  type VirtualTimeline,
  type VirtualTimelineEntry,
  type VirtualTransitionPreview,
} from "./virtual-timeline";

type Deck = "a" | "b";

// ── Sync policy: the MUSIC is the master clock ────────────────────────────
// While the music is audibly running, it is never paused, rewound, or seeked
// for the video's sake — every disturbance of a playing audio element is an
// audible artifact, and video-side stalls are frequent on real networks
// (each sub-second boundary reloads a deck source). Instead, the VIDEO yields:
// when it falls behind the music, it jumps forward to the mapped position.
// The music is only hard-seeked on authoritative jumps (play start, scrub,
// timeline edit, src change) and forward-caught when it stalled BEHIND the
// video on its own (never backward).
const VIDEO_LAG_JUMP_S = 0.3;
const MUSIC_FORWARD_CATCH_S = 0.35;
const MUSIC_HARD_SEEK_S = 0.25;
const COMMITTED_PLAYBACK_INTERVAL_MS = 200;
const INCOMING_DECK_CORRECTION_INTERVAL_MS = 500;
const INCOMING_DECK_DRIFT_TOLERANCE_S = 0.15;

interface PendingSeek {
  timeS: number;
  play: boolean;
}

export interface UseVirtualPreviewOptions {
  enabled: boolean;
  slots: DraftSlot[];
  clips: Pick<TimelineClip, "clip_index" | "signed_url">[];
  grid: number[];
  /** Staged carousel-moment block to splice into the virtual timeline
   * (undefined/null = no block this session). See `VirtualCarouselSplice`. */
  carousel?: VirtualCarouselSplice | null;
  currentTime: number;
  muted: boolean;
  musicAudioUrl?: string | null;
  musicStartS?: number;
  soundMuted?: boolean;
  /**
   * A music track is selected for this cut, whether or not its preview URL is
   * available. The final render drops footage audio entirely when a track is
   * mixed in, so the decks must stay silent even if the music itself fails.
   */
  musicTrackActive?: boolean;
  /** Enables decoded-frame output-timeline sampling. Flag-off preserves the
   * legacy timeupdate/rAF transport exactly. */
  frameDriven?: boolean;
  /** High-frequency output-timeline time for authored layers and playheads. */
  onFrameTimeUpdate?: (timeS: number) => void;
  onTimeUpdate: (timeS: number) => void;
  onDuration: (durationS: number) => void;
  onPlayingChange: (playing: boolean) => void;
  onSourceError: () => void;
  onMusicError?: () => void;
}

export interface VirtualPreviewVideoProps {
  ref: RefObject<HTMLVideoElement>;
  muted: boolean;
  playsInline: true;
  preload: "auto";
  "data-virtual-preview-deck": Deck;
  "data-active": boolean;
  onLoadedMetadata: () => void;
  onCanPlay: () => void;
  onPlaying: () => void;
  onWaiting: () => void;
  onSeeking: () => void;
  onSeeked: () => void;
  onTimeUpdate: () => void;
  onRateChange?: () => void;
  onEnded: () => void;
  onPlay: () => void;
  onPause: () => void;
  onError: () => void;
}

export interface VirtualPreviewAudioProps {
  ref: RefObject<HTMLAudioElement>;
  src: string;
  muted: boolean;
  preload: "auto";
  "data-virtual-preview-music": true;
  onLoadedMetadata: () => void;
  onError: () => void;
}

export interface VirtualPreviewController {
  timeline: VirtualTimeline;
  activeDeck: Deck;
  buffering: boolean;
  /** Compatibility state sampled from committed transport time. Frame-driven
   * consumers should evaluate `timeline` against the external frame clock. */
  transitionPreview?: VirtualTransitionPreview | null;
  videoAProps: VirtualPreviewVideoProps;
  videoBProps: VirtualPreviewVideoProps;
  musicAudioProps: VirtualPreviewAudioProps | null;
  play: () => void;
  pause: () => void;
  toggle: () => void;
  seekTo: (timeS: number) => void;
}

function otherDeck(deck: Deck): Deck {
  return deck === "a" ? "b" : "a";
}

function playIgnoringAbort(el: HTMLMediaElement, onFailure: () => void) {
  void el.play().catch((err: unknown) => {
    // A src swap mid-play rejects the pending play() with AbortError — that's
    // routine (deck source reloads, the music blob swap), NOT a playback
    // failure. Treating it as fatal paused the whole transport the moment the
    // music src changed under a running preview.
    if ((err as DOMException | null)?.name === "AbortError") return;
    onFailure();
  });
}

function safeSetCurrentTime(video: HTMLMediaElement, timeS: number) {
  try {
    video.currentTime = Math.max(0, timeS);
  } catch {
    // Some browsers reject seeking before metadata is available. The pending
    // seek is retried from onLoadedMetadata.
  }
}

function getVirtualMusicAudio(ref: RefObject<HTMLAudioElement>): HTMLAudioElement[] {
  const audio = ref.current;
  const domAudio =
    typeof document === "undefined"
      ? null
      : document.querySelector<HTMLAudioElement>("audio[data-virtual-preview-music]");
  return [audio, domAudio].filter(
    (item, index, all): item is HTMLAudioElement => !!item && all.indexOf(item) === index,
  );
}

export function mapDeckMediaTimeToVirtualTime(
  entry: Pick<VirtualTimelineEntry, "startS" | "durationS" | "inS">,
  mediaTimeS: number,
): number {
  const localOffsetS = mediaTimeS - entry.inS;
  return Math.max(
    entry.startS,
    Math.min(entry.startS + entry.durationS, entry.startS + localOffsetS),
  );
}

export function useVirtualPreview({
  enabled,
  slots,
  clips,
  grid,
  carousel,
  currentTime,
  muted,
  musicAudioUrl,
  musicStartS = 0,
  soundMuted = false,
  musicTrackActive = false,
  frameDriven = false,
  onFrameTimeUpdate,
  onTimeUpdate,
  onDuration,
  onPlayingChange,
  onSourceError,
  onMusicError,
}: UseVirtualPreviewOptions): VirtualPreviewController {
  const deckMuted = muted || musicTrackActive;
  const timeline = useMemo(
    () => buildVirtualTimeline(slots, clips, grid, carousel),
    [carousel, clips, grid, slots],
  );
  const transitionPreview = useMemo(
    () => transitionPreviewAtTime(timeline, currentTime),
    [currentTime, timeline],
  );
  const videoARef = useRef<HTMLVideoElement>(null) as RefObject<HTMLVideoElement>;
  const videoBRef = useRef<HTMLVideoElement>(null) as RefObject<HTMLVideoElement>;
  const musicAudioRef = useRef<HTMLAudioElement>(null) as RefObject<HTMLAudioElement>;
  const [activeDeck, setActiveDeck] = useState<Deck>("a");
  const [buffering, setBuffering] = useState(false);

  const activeDeckRef = useRef<Deck>("a");
  const currentTimeRef = useRef(currentTime);
  const timelineRef = useRef(timeline);
  const enabledRef = useRef(enabled);
  const musicAudioUrlRef = useRef(musicAudioUrl ?? null);
  const musicStartSRef = useRef(musicStartS);
  const soundMutedRef = useRef(soundMuted);
  // Decks bind to slot KEYS, not array indices: splits/inserts shift every
  // later slot's index, so an index-bound deck would resolve to the wrong
  // entry between an edit and the timeline-change effect re-mapping it.
  const deckSlotRef = useRef<Record<Deck, string | null>>({ a: null, b: null });
  const pendingSeekRef = useRef<Record<Deck, PendingSeek | null>>({ a: null, b: null });
  const playingRef = useRef(false);
  const frameDrivenRef = useRef(frameDriven);
  const onFrameTimeUpdateRef = useRef(onFrameTimeUpdate);
  const deckFrameGenerationRef = useRef<Record<Deck, number>>({ a: 0, b: 0 });
  const deckFrameCallbackRef = useRef<Record<Deck, number | null>>({ a: null, b: null });
  const lastCommittedPlaybackRef = useRef(0);
  const lastIncomingCorrectionRef = useRef<Record<Deck, number>>({
    a: Number.NEGATIVE_INFINITY,
    b: Number.NEGATIVE_INFINITY,
  });
  const lastEmittedCommittedTimeRef = useRef(currentTime);
  const committedPropTimeRef = useRef(currentTime);
  // Carousel-window transport clock (bug fix: the block has no video deck to
  // source `timeupdate` from — a paused deck never fires it, so without this
  // the transport froze at the block's start the instant play carried the
  // playhead in). While playing inside a spliced carousel entry, this drives
  // `currentTime` from a rAF/wall-clock delta instead, then hands back to
  // `finishEntry` (the same clip-to-clip boundary path) once the window ends.
  const carouselClockRef = useRef<{
    raf: number;
    entryIndex: number;
    startWallMs: number;
    startVirtualS: number;
  } | null>(null);
  // `finishEntry`/`swapToNext` are declared further down (they close over
  // `showMapping`, which itself needs to start this clock) — a real mutual
  // reference. Routed through a ref, refreshed every render below their
  // definitions, so the clock always calls the latest closure without
  // forcing a declaration-order cycle or stale-closure bugs.
  const finishEntryRef = useRef<(entryIndex: number) => void>(() => {});

  // The shell's committed time intentionally trails the decoded-frame clock.
  // Do not let that throttled prop overwrite a newer live frame on unrelated
  // shell renders. A value we did not just emit is an authoritative external
  // seek/timeline correction and does take ownership of the transport ref.
  if (!Object.is(committedPropTimeRef.current, currentTime)) {
    committedPropTimeRef.current = currentTime;
    if (
      !playingRef.current ||
      Math.abs(currentTime - lastEmittedCommittedTimeRef.current) > 0.001
    ) {
      currentTimeRef.current = currentTime;
    }
  }
  timelineRef.current = timeline;
  enabledRef.current = enabled;
  musicAudioUrlRef.current = musicAudioUrl ?? null;
  musicStartSRef.current = musicStartS;
  soundMutedRef.current = soundMuted;
  frameDrivenRef.current = frameDriven;
  onFrameTimeUpdateRef.current = onFrameTimeUpdate;

  useEffect(() => {
    onDuration(enabled ? timeline.totalDurationS : 0);
  }, [enabled, onDuration, timeline.totalDurationS]);

  useEffect(() => {
    for (const video of [videoARef.current, videoBRef.current]) {
      if (video) video.muted = deckMuted;
    }
  }, [deckMuted]);

  useEffect(() => {
    for (const audio of getVirtualMusicAudio(musicAudioRef)) {
      audio.muted = soundMuted;
    }
  }, [soundMuted]);

  const refForDeck = useCallback((deck: Deck) => {
    return deck === "a" ? videoARef : videoBRef;
  }, []);

  const publishFrameTime = useCallback((timeS: number) => {
    currentTimeRef.current = timeS;
    onFrameTimeUpdateRef.current?.(timeS);
  }, []);

  const publishCommittedTime = useCallback(
    (timeS: number, publishFrame = true) => {
      if (publishFrame) publishFrameTime(timeS);
      lastCommittedPlaybackRef.current = performance.now();
      lastEmittedCommittedTimeRef.current = timeS;
      onTimeUpdate(timeS);
    },
    [onTimeUpdate, publishFrameTime],
  );

  const publishPlaybackTime = useCallback(
    (
      timeS: number,
      { forceCommit = false, publishFrame = true }: {
        forceCommit?: boolean;
        publishFrame?: boolean;
      } = {},
    ) => {
      if (!frameDrivenRef.current) {
        publishCommittedTime(timeS);
        return;
      }
      if (publishFrame) publishFrameTime(timeS);
      const now = performance.now();
      if (forceCommit || now - lastCommittedPlaybackRef.current >= COMMITTED_PLAYBACK_INTERVAL_MS) {
        lastCommittedPlaybackRef.current = now;
        lastEmittedCommittedTimeRef.current = timeS;
        onTimeUpdate(timeS);
      }
    },
    [onTimeUpdate, publishCommittedTime, publishFrameTime],
  );

  const sampleDeckFrameRef = useRef<(deck: Deck, mediaTimeS: number) => void>(() => {});

  const cancelDeckFrameClock = useCallback(
    (deck: Deck) => {
      deckFrameGenerationRef.current[deck] += 1;
      const video = refForDeck(deck).current;
      const callbackId = deckFrameCallbackRef.current[deck];
      if (video && callbackId != null && typeof video.cancelVideoFrameCallback === "function") {
        video.cancelVideoFrameCallback(callbackId);
      }
      deckFrameCallbackRef.current[deck] = null;
    },
    [refForDeck],
  );

  const startDeckFrameClock = useCallback(
    (deck: Deck) => {
      if (!frameDrivenRef.current) return;
      const video = refForDeck(deck).current;
      if (!video || typeof video.requestVideoFrameCallback !== "function") return;

      cancelDeckFrameClock(deck);
      const generation = deckFrameGenerationRef.current[deck];
      const drawDecodedFrame: VideoFrameRequestCallback = (_now, metadata) => {
        if (
          generation !== deckFrameGenerationRef.current[deck] ||
          deck !== activeDeckRef.current ||
          !enabledRef.current ||
          !playingRef.current
        ) {
          return;
        }
        sampleDeckFrameRef.current(deck, metadata.mediaTime);
        deckFrameCallbackRef.current[deck] = video.requestVideoFrameCallback(drawDecodedFrame);
      };
      deckFrameCallbackRef.current[deck] = video.requestVideoFrameCallback(drawDecodedFrame);
    },
    [cancelDeckFrameClock, refForDeck],
  );

  const stopCarouselClock = useCallback(() => {
    const running = carouselClockRef.current;
    if (running) {
      cancelAnimationFrame(running.raf);
      carouselClockRef.current = null;
    }
  }, []);

  const pauseAll = useCallback(() => {
    playingRef.current = false;
    stopCarouselClock();
    cancelDeckFrameClock("a");
    cancelDeckFrameClock("b");
    pendingSeekRef.current.a = null;
    pendingSeekRef.current.b = null;
    videoARef.current?.pause();
    videoBRef.current?.pause();
    for (const audio of getVirtualMusicAudio(musicAudioRef)) {
      audio.pause();
    }
    if (frameDrivenRef.current) publishCommittedTime(currentTimeRef.current);
    onPlayingChange(false);
  }, [cancelDeckFrameClock, onPlayingChange, publishCommittedTime, stopCarouselClock]);

  const loadDeck = useCallback(
    (deck: Deck, entry: VirtualTimelineEntry, timeS: number | null, play: boolean) => {
      const video = refForDeck(deck).current;
      if (!video || !entry.sourceUrl) return;

      const needsSource = deckSlotRef.current[deck] !== entry.slotKey || video.src !== entry.sourceUrl;
      if (needsSource) {
        cancelDeckFrameClock(deck);
        deckSlotRef.current[deck] = entry.slotKey;
        pendingSeekRef.current[deck] = timeS == null ? null : { timeS, play };
        video.src = entry.sourceUrl;
        video.preload = "auto";
        video.load();
        return;
      }

      // Skip no-op seeks: the preload already parked the deck at the in-point,
      // and re-seeking to the same position fires seeking/waiting churn that
      // reads as a stall at every boundary.
      if (timeS != null && Math.abs(video.currentTime - timeS) > 0.05) {
        safeSetCurrentTime(video, timeS);
      }
      if (play) {
        if (video.paused) playIgnoringAbort(video, pauseAll);
        // Only the active deck owns the output clock. Incoming crossfade
        // decks may play for compositing, but registering/cancelling their
        // callbacks on every outgoing frame creates needless decoder churn.
        if (deck === activeDeckRef.current) startDeckFrameClock(deck);
      }
    },
    [cancelDeckFrameClock, pauseAll, refForDeck, startDeckFrameClock],
  );

  const preloadNext = useCallback(
    (deck: Deck, afterEntryIndex: number) => {
      const next = nextVirtualEntry(timelineRef.current, afterEntryIndex);
      // A carousel block has no video source to preload — the mounted
      // preview component owns rendering that window.
      if (!next || next.kind !== "clip" || !next.sourceUrl) return;
      loadDeck(deck, next, next.inS, false);
    },
    [loadDeck],
  );

  const syncIncomingDeck = useCallback(
    (entryIndex: number, virtualTimeS: number, play: boolean) => {
      const entry = timelineRef.current.entries[entryIndex];
      const next = nextVirtualEntry(timelineRef.current, entryIndex);
      if (
        !entry ||
        !next ||
        next.kind !== "clip" ||
        !next.sourceUrl ||
        next.overlapBeforeS <= 0 ||
        virtualTimeS < next.startS ||
        virtualTimeS >= entry.startS + entry.durationS
      ) {
        return false;
      }
      const incomingTimeS =
        next.inS + Math.max(0, Math.min(next.overlapBeforeS, virtualTimeS - next.startS));
      const incomingDeck = otherDeck(activeDeckRef.current);
      const video = refForDeck(incomingDeck).current;
      const needsSource =
        !video ||
        deckSlotRef.current[incomingDeck] !== next.slotKey ||
        video.src !== next.sourceUrl;
      if (needsSource || !play || video?.paused) {
        loadDeck(incomingDeck, next, incomingTimeS, play);
      } else {
        const now = performance.now();
        if (
          now - lastIncomingCorrectionRef.current[incomingDeck] >=
          INCOMING_DECK_CORRECTION_INTERVAL_MS
        ) {
          lastIncomingCorrectionRef.current[incomingDeck] = now;
          if (
            Math.abs(video.currentTime - incomingTimeS) >
            INCOMING_DECK_DRIFT_TOLERANCE_S
          ) {
            safeSetCurrentTime(video, incomingTimeS);
          }
        }
      }
      return true;
    },
    [loadDeck, refForDeck],
  );

  const syncMusicToVirtualTime = useCallback(
    (virtualTimeS: number, play: boolean, mode: "hard" | "soft" = "hard") => {
      const audio = getVirtualMusicAudio(musicAudioRef)[0];
      if (!audio || !musicAudioUrlRef.current) return;
      const musicTimeS = mapVirtualTimeToMusicTime(virtualTimeS, musicStartSRef.current);
      const behindS = musicTimeS - audio.currentTime; // >0: the music is behind
      // "hard" = authoritative jump (play start, scrub, timeline edit, src
      // change): seek in either direction. "soft" = continuous sync (boundary
      // swaps): NEVER rewind a running music element — only catch it up when
      // it fell behind on its own.
      if (mode === "hard" ? Math.abs(behindS) > MUSIC_HARD_SEEK_S : behindS > MUSIC_FORWARD_CATCH_S) {
        safeSetCurrentTime(audio, musicTimeS);
      }
      if (play && playingRef.current) {
        if (audio.paused) {
          playIgnoringAbort(audio, pauseAll);
        }
      } else {
        audio.pause();
      }
    },
    [pauseAll],
  );

  // Carousel-window transport clock: while playing inside a spliced carousel
  // entry, no deck fires `timeupdate` (both are paused by design — see the
  // `showMapping` carousel branch below), so without a clock of its own the
  // transport froze the instant the playhead entered the block. Drives
  // `currentTime` from a rAF/wall-clock delta instead, and hands off to the
  // exact same clip-to-clip boundary path (`finishEntry` → `swapToNext`) once
  // the window ends — the next deck gets seeked to its correct start and
  // resumed exactly as it would at any other cut.
  const tickCarouselClock = useCallback(() => {
    const running = carouselClockRef.current;
    if (!running) return;
    const entry = timelineRef.current.entries[running.entryIndex];
    if (!entry || entry.kind === "clip" || !playingRef.current) {
      // Timeline changed shape (edit) or playback stopped out-of-band —
      // don't keep driving a clock nothing asked for anymore.
      carouselClockRef.current = null;
      return;
    }
    const endS = entry.startS + entry.durationS;
    const elapsedS = (performance.now() - running.startWallMs) / 1000;
    const virtualTimeS = Math.min(endS, running.startVirtualS + elapsedS);
    publishPlaybackTime(virtualTimeS);
    // Music is the master clock (see the sync-policy note at the top of this
    // file): "soft" mode only forward-catches a stall, never rewinds a
    // running track, same as the clip-to-clip boundary swap below.
    syncMusicToVirtualTime(virtualTimeS, true, "soft");
    if (virtualTimeS >= endS - 0.05) {
      carouselClockRef.current = null;
      finishEntryRef.current(running.entryIndex);
      return;
    }
    running.raf = requestAnimationFrame(tickCarouselClock);
  }, [publishPlaybackTime, syncMusicToVirtualTime]);

  const startCarouselClock = useCallback(
    (entryIndex: number, virtualTimeS: number) => {
      stopCarouselClock();
      carouselClockRef.current = {
        raf: requestAnimationFrame(tickCarouselClock),
        entryIndex,
        startWallMs: performance.now(),
        startVirtualS: virtualTimeS,
      };
    },
    [stopCarouselClock, tickCarouselClock],
  );

  const showMapping = useCallback(
    (timeS: number, play: boolean) => {
      const mapping = mapVirtualTime(timelineRef.current, timeS);
      if (!mapping) {
        onSourceError();
        return;
      }

      if (mapping.entry.kind !== "clip") {
        // Carousel-block window: no video deck to load/seek — the mounted
        // preview component (CarouselBlockPreview) owns rendering this
        // window. Pause both decks and gate deck-driven playback, but keep
        // the music track (if any) in sync — the final render's audio bed
        // continues under whatever visual is on screen.
        cancelDeckFrameClock("a");
        cancelDeckFrameClock("b");
        videoARef.current?.pause();
        videoBRef.current?.pause();
        // Neither deck is "active" for the duration of the window — null out
        // both slot bindings so a stale/queued `timeupdate`/`ended` event from
        // the deck that was just paused (browsers can still dispatch one
        // already-enqueued event after a synchronous pause()) fails the
        // `slotKey == null` guard in handleTimeUpdate/handleEnded instead of
        // being matched back to the clip entry we just left — which would
        // re-trigger finishEntry and restart this carousel clock from
        // startS. preloadNext (below) re-populates the deck it loads.
        deckSlotRef.current.a = null;
        deckSlotRef.current.b = null;
        // Preload whatever plays AFTER the block onto the currently-inactive
        // deck now, not at the boundary — otherwise the handoff out of the
        // block (a fresh `loadDeck` src+load()) would defer its `play()` to
        // `onLoadedMetadata`, a beat of black/frozen frame exactly where the
        // clip-to-clip crossfade path never has one. Mirrors the clip
        // branch's own preload of its neighboring deck below.
        preloadNext(otherDeck(activeDeckRef.current), mapping.entryIndex);
        syncMusicToVirtualTime(mapping.virtualTimeS, play);
        publishCommittedTime(mapping.virtualTimeS);
        if (play) {
          startCarouselClock(mapping.entryIndex, mapping.virtualTimeS);
        } else {
          stopCarouselClock();
        }
        return;
      }
      stopCarouselClock();
      if (!mapping.entry.sourceUrl) {
        onSourceError();
        return;
      }

      const deck = activeDeckRef.current;
      loadDeck(deck, mapping.entry, mapping.sourceTimeS, play);
      if (!syncIncomingDeck(mapping.entryIndex, mapping.virtualTimeS, play)) {
        preloadNext(otherDeck(deck), mapping.entryIndex);
      }
      syncMusicToVirtualTime(mapping.virtualTimeS, play);
      // In a video window the transport target is committed immediately, but
      // authored layers wait for seeked/rVFC to confirm the decoded picture.
      publishCommittedTime(mapping.virtualTimeS, !frameDrivenRef.current);
    },
    [
      loadDeck,
      cancelDeckFrameClock,
      onSourceError,
      publishCommittedTime,
      preloadNext,
      startCarouselClock,
      stopCarouselClock,
      syncIncomingDeck,
      syncMusicToVirtualTime,
    ],
  );

  const pause = useCallback(() => {
    pauseAll();
  }, [pauseAll]);

  const play = useCallback(() => {
    if (!enabledRef.current) return;
    playingRef.current = true;
    const atEnd =
      timelineRef.current.totalDurationS > 0 &&
      currentTimeRef.current >= timelineRef.current.totalDurationS - 0.05;
    showMapping(atEnd ? 0 : currentTimeRef.current, true);
  }, [showMapping]);

  const seekTo = useCallback(
    (timeS: number) => {
      pause();
      showMapping(timeS, false);
    },
    [pause, showMapping],
  );

  const toggle = useCallback(() => {
    if (playingRef.current) pause();
    else play();
  }, [pause, play]);

  const swapToNext = useCallback(
    (entryIndex: number) => {
      // Defensive: `tickCarouselClock` already clears this before calling
      // `finishEntry` -> `swapToNext` on a normal block-end handoff, but any
      // other caller transitioning away from the current entry shouldn't
      // leave a stale clock racing the deck it's about to hand off to.
      stopCarouselClock();
      const next = nextVirtualEntry(timelineRef.current, entryIndex);
      if (next && next.kind !== "clip") {
        // Advancing INTO a carousel block: no deck to swap onto. Re-route
        // through showMapping's carousel gate (pauses both decks, keeps the
        // music/time moving) rather than duplicating that logic here.
        refForDeck(activeDeckRef.current).current?.pause();
        showMapping(next.startS, playingRef.current);
        return;
      }
      if (!next || !next.sourceUrl) {
        pause();
        publishCommittedTime(timelineRef.current.totalDurationS);
        return;
      }

      const prevDeck = activeDeckRef.current;
      const nextDeck = otherDeck(prevDeck);
      const prevVideo = refForDeck(prevDeck).current;
      const outgoing = timelineRef.current.entries[entryIndex];
      const boundaryTimeS = Math.min(
        timelineRef.current.totalDurationS,
        (outgoing?.startS ?? next.startS) + (outgoing?.durationS ?? 0),
      );

      cancelDeckFrameClock(prevDeck);
      prevVideo?.pause();
      // loadDeck owns the seek+play: covered decks seek and play immediately,
      // fresh sources defer to the onLoadedMetadata pending-seek. Seeking or
      // playing the element here as well made a fresh source play from frame
      // 0 and then snap to the in-point (visible "restart"/repeat).
      activeDeckRef.current = nextDeck;
      setActiveDeck(nextDeck);
      loadDeck(nextDeck, next, next.inS + next.overlapBeforeS, true);
      preloadNext(prevDeck, entryIndex + 1);
      syncMusicToVirtualTime(boundaryTimeS, true, "soft");
      publishCommittedTime(boundaryTimeS, !frameDrivenRef.current);
    },
    [
      loadDeck,
      cancelDeckFrameClock,
      publishCommittedTime,
      pause,
      preloadNext,
      refForDeck,
      showMapping,
      stopCarouselClock,
      syncMusicToVirtualTime,
    ],
  );

  const finishEntry = useCallback(
    (entryIndex: number) => {
      const entry = timelineRef.current.entries[entryIndex];
      if (!entry) {
        pause();
        return;
      }
      if (entry.startS + entry.durationS >= timelineRef.current.totalDurationS - 0.05) {
        pause();
        publishCommittedTime(timelineRef.current.totalDurationS);
      } else if (playingRef.current) {
        swapToNext(entryIndex);
      }
    },
    [pause, publishCommittedTime, swapToNext],
  );
  // Kept fresh every render so `tickCarouselClock` (declared earlier — a real
  // mutual reference with `finishEntry`/`swapToNext`/`showMapping`) always
  // calls the latest closure without an initialization-order cycle.
  finishEntryRef.current = finishEntry;

  const handleLoadedMetadata = useCallback(
    (deck: Deck) => {
      const video = refForDeck(deck).current;
      const pending = pendingSeekRef.current[deck];
      if (!video || !pending) return;
      pendingSeekRef.current[deck] = null;
      safeSetCurrentTime(video, pending.timeS);
      if (pending.play) {
        if (video.paused) playIgnoringAbort(video, pauseAll);
        if (deck === activeDeckRef.current) startDeckFrameClock(deck);
      }
    },
    [pauseAll, refForDeck, startDeckFrameClock],
  );

  const sampleDeckMediaTime = useCallback(
    (
      deck: Deck,
      mediaTimeS: number,
      forceCommit: boolean,
      authoritativeFrame = true,
    ) => {
      if (!enabledRef.current || deck !== activeDeckRef.current) return;
      const slotKey = deckSlotRef.current[deck];
      if (slotKey == null) return;

      const entryIndex = timelineRef.current.entries.findIndex(
        (entry) => entry.kind === "clip" && entry.slotKey === slotKey,
      );
      const entry = timelineRef.current.entries[entryIndex];
      // A deck only ever binds to a clip entry (loadDeck's param type), so a
      // match here is always "clip" — this guard is a type-narrow, not a
      // real runtime branch.
      if (!entry || entry.kind !== "clip") return;

      const localOffsetS = mediaTimeS - entry.inS;
      const virtualTimeS = mapDeckMediaTimeToVirtualTime(entry, mediaTimeS);
      syncIncomingDeck(entryIndex, virtualTimeS, playingRef.current);
      const audio = getVirtualMusicAudio(musicAudioRef)[0];
      if (audio && musicAudioUrlRef.current && !audio.paused && playingRef.current) {
        const audioVirtualS = audio.currentTime - Math.max(0, musicStartSRef.current);
        const diffS = audioVirtualS - virtualTimeS;
        if (diffS > VIDEO_LAG_JUMP_S) {
          // The video fell behind the running music (deck stall / slow load):
          // the music is the master clock, so jump the VIDEO forward to the
          // mapped position — possibly into a later entry.
          showMapping(Math.min(audioVirtualS, timelineRef.current.totalDurationS), true);
          return;
        }
        if (diffS < -MUSIC_FORWARD_CATCH_S) {
          // The music stalled on its own and recovered behind the video —
          // forward-catch it (never rewind a running music element).
          safeSetCurrentTime(
            audio,
            mapVirtualTimeToMusicTime(virtualTimeS, musicStartSRef.current),
          );
        }
      }
      const video = refForDeck(deck).current;
      const decodedFrameClockActive =
        frameDrivenRef.current &&
        video != null &&
        typeof video.requestVideoFrameCallback === "function";
      publishPlaybackTime(virtualTimeS, {
        forceCommit,
        // Native timeupdate is a compatibility/resync signal. When rVFC is
        // available it must not move authored layers ahead of the decoded
        // frame the user can actually see.
        publishFrame: authoritativeFrame || !decodedFrameClockActive,
      });

      if (localOffsetS >= entry.durationS - 0.05) {
        finishEntry(entryIndex);
      }
    },
    [finishEntry, publishPlaybackTime, refForDeck, showMapping, syncIncomingDeck],
  );
  sampleDeckFrameRef.current = (deck, mediaTimeS) => {
    sampleDeckMediaTime(deck, mediaTimeS, false);
  };

  const handleTimeUpdate = useCallback(
    (deck: Deck) => {
      const video = refForDeck(deck).current;
      if (!video) return;
      sampleDeckMediaTime(deck, video.currentTime, true, false);
    },
    [refForDeck, sampleDeckMediaTime],
  );

  const handleEnded = useCallback(
    (deck: Deck) => {
      if (!enabledRef.current || deck !== activeDeckRef.current) return;
      const slotKey = deckSlotRef.current[deck];
      if (slotKey == null) return;
      const entryIndex = timelineRef.current.entries.findIndex(
        (entry) => entry.kind === "clip" && entry.slotKey === slotKey,
      );
      finishEntry(entryIndex);
    },
    [finishEntry],
  );

  const handleSourceError = useCallback(() => {
    pause();
    onSourceError();
  }, [onSourceError, pause]);

  useEffect(() => {
    if (!enabled) {
      pause();
      return;
    }
    if (timeline.hasMissingSource || timeline.entries.length === 0) {
      onSourceError();
      return;
    }
    // Preserve transport state across edits: re-mapping with play=false while
    // playing paused the music but left the video rolling until the next
    // boundary (music dropout on every mid-play edit).
    showMapping(currentTimeRef.current, playingRef.current);
  }, [enabled, onSourceError, pause, showMapping, timeline]);

  // When a fresh music URL arrives (e.g. re-signed after an expired-signature
  // error), resync so playback resumes at the mapped offset. An identical URL
  // won't re-fire this; music then resumes on the next play/seek.
  useEffect(() => {
    if (!enabledRef.current || !musicAudioUrl) return;
    syncMusicToVirtualTime(currentTimeRef.current, playingRef.current);
  }, [musicAudioUrl, syncMusicToVirtualTime]);

  // A background tab can suppress both rAF and decoded-frame callbacks. On
  // return, resample the authoritative media clock before scheduling a new
  // callback so authored layers never resume from the stale pre-hide frame.
  useEffect(() => {
    if (!frameDriven || typeof document === "undefined") return;
    const handleVisibilityChange = () => {
      if (document.visibilityState !== "visible" || !enabledRef.current) return;
      const audio = getVirtualMusicAudio(musicAudioRef)[0];
      if (audio && musicAudioUrlRef.current && playingRef.current && !audio.paused) {
        const audioVirtualS = Math.max(
          0,
          Math.min(
            timelineRef.current.totalDurationS,
            audio.currentTime - Math.max(0, musicStartSRef.current),
          ),
        );
        showMapping(audioVirtualS, true);
        return;
      }
      const carouselClock = carouselClockRef.current;
      if (carouselClock) {
        cancelAnimationFrame(carouselClock.raf);
        tickCarouselClock();
        return;
      }
      const deck = activeDeckRef.current;
      const video = refForDeck(deck).current;
      if (video) sampleDeckMediaTime(deck, video.currentTime, true);
      if (playingRef.current) startDeckFrameClock(deck);
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => document.removeEventListener("visibilitychange", handleVisibilityChange);
  }, [
    frameDriven,
    refForDeck,
    sampleDeckMediaTime,
    showMapping,
    startDeckFrameClock,
    tickCarouselClock,
  ]);

  // Cancel any in-flight carousel-window clock on unmount — otherwise the
  // rAF loop keeps calling onTimeUpdate/onPlayingChange against a torn-down
  // editor.
  useEffect(
    () => () => {
      stopCarouselClock();
      cancelDeckFrameClock("a");
      cancelDeckFrameClock("b");
    },
    [cancelDeckFrameClock, stopCarouselClock],
  );

  const musicAudioProps: VirtualPreviewAudioProps | null = musicAudioUrl
    ? {
        ref: musicAudioRef,
        src: musicAudioUrl,
        muted: soundMuted,
        preload: "auto",
        "data-virtual-preview-music": true,
        // Seeks issued before metadata exists are swallowed (safeSetCurrentTime),
        // so a mid-play src swap (song picker) started the new track at 0:00.
        // Mirror the video decks' pending-seek: re-map once metadata is ready.
        onLoadedMetadata: () => {
          syncMusicToVirtualTime(currentTimeRef.current, playingRef.current);
        },
        onError: () => {
          musicAudioRef.current?.pause();
          onMusicError?.();
        },
      }
    : null;

  function videoProps(deck: Deck): VirtualPreviewVideoProps {
    return {
      ref: refForDeck(deck),
      muted: deckMuted,
      playsInline: true,
      preload: "auto",
      "data-virtual-preview-deck": deck,
      "data-active": activeDeck === deck,
      onLoadedMetadata: () => handleLoadedMetadata(deck),
      onCanPlay: () => {
        setBuffering(false);
        const video = refForDeck(deck).current;
        if (frameDrivenRef.current && video && deck === activeDeckRef.current) {
          sampleDeckMediaTime(deck, video.currentTime, true);
          if (playingRef.current) startDeckFrameClock(deck);
        }
      },
      onPlaying: () => {
        setBuffering(false);
        if (deck === activeDeckRef.current) {
          onPlayingChange(true);
          startDeckFrameClock(deck);
        }
      },
      // Deck stalls do NOT touch the music: it is the master clock, and the
      // video catch-up in handleTimeUpdate re-aligns the picture when the
      // deck recovers. (Both the instant hold and the debounced hold gapped
      // the music audibly — boundary swaps stall briefly on almost every cut.)
      onWaiting: () => setBuffering(true),
      onSeeking: () => {
        setBuffering(true);
        cancelDeckFrameClock(deck);
      },
      onSeeked: () => {
        setBuffering(false);
        if (!frameDrivenRef.current) return;
        const video = refForDeck(deck).current;
        if (video && deck === activeDeckRef.current) {
          sampleDeckMediaTime(deck, video.currentTime, true);
          if (playingRef.current) startDeckFrameClock(deck);
        }
      },
      onTimeUpdate: () => handleTimeUpdate(deck),
      onRateChange: () => {
        if (!frameDrivenRef.current) return;
        const video = refForDeck(deck).current;
        if (!video || deck !== activeDeckRef.current) return;
        sampleDeckMediaTime(deck, video.currentTime, true);
        if (playingRef.current) startDeckFrameClock(deck);
      },
      onEnded: () => handleEnded(deck),
      onPlay: () => {
        if (deck === activeDeckRef.current) onPlayingChange(true);
      },
      onPause: () => {
        if (deck === activeDeckRef.current) onPlayingChange(false);
      },
      onError: handleSourceError,
    };
  }

  return {
    timeline,
    activeDeck,
    buffering,
    transitionPreview,
    videoAProps: videoProps("a"),
    videoBProps: videoProps("b"),
    musicAudioProps,
    play,
    pause,
    toggle,
    seekTo,
  };
}
