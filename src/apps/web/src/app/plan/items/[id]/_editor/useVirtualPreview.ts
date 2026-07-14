"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import type { TimelineClip } from "@/lib/generative-api";
import type { DraftSlot } from "@/app/generative/timeline-math";
import {
  buildVirtualTimeline,
  mapVirtualTimeToMusicTime,
  mapVirtualTime,
  nextVirtualEntry,
  type VirtualTimeline,
  type VirtualTimelineEntry,
} from "./virtual-timeline";

type Deck = "a" | "b";

// How long the active deck must stay stalled before the music is held. Long
// enough to swallow the transient `waiting` every boundary swap emits, short
// enough that a real network stall doesn't let the music drift audibly far.
const MUSIC_HOLD_DELAY_MS = 250;

interface PendingSeek {
  timeS: number;
  play: boolean;
}

export interface UseVirtualPreviewOptions {
  enabled: boolean;
  slots: DraftSlot[];
  clips: Pick<TimelineClip, "clip_index" | "signed_url">[];
  grid: number[];
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

export function useVirtualPreview({
  enabled,
  slots,
  clips,
  grid,
  currentTime,
  muted,
  musicAudioUrl,
  musicStartS = 0,
  soundMuted = false,
  musicTrackActive = false,
  onTimeUpdate,
  onDuration,
  onPlayingChange,
  onSourceError,
  onMusicError,
}: UseVirtualPreviewOptions): VirtualPreviewController {
  const deckMuted = muted || musicTrackActive;
  const timeline = useMemo(
    () => buildVirtualTimeline(slots, clips, grid),
    [clips, grid, slots],
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
  // Stall-hold state: the music is paused only when the active deck has been
  // stalled for MUSIC_HOLD_DELAY_MS. Sub-second boundary swaps fire a transient
  // `waiting` on almost every clip (seek + first-frame decode) — pausing the
  // music on each one produced an audible gap at every cut (measured live:
  // 33 pause/play cycles in 90s on a beat-synced edit).
  const musicHoldTimerRef = useRef<number | null>(null);
  const musicHeldRef = useRef(false);

  currentTimeRef.current = currentTime;
  timelineRef.current = timeline;
  enabledRef.current = enabled;
  musicAudioUrlRef.current = musicAudioUrl ?? null;
  musicStartSRef.current = musicStartS;
  soundMutedRef.current = soundMuted;

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

  const clearMusicHold = useCallback(() => {
    if (musicHoldTimerRef.current != null) {
      window.clearTimeout(musicHoldTimerRef.current);
      musicHoldTimerRef.current = null;
    }
    musicHeldRef.current = false;
  }, []);

  useEffect(() => clearMusicHold, [clearMusicHold]);

  const pauseAll = useCallback(() => {
    playingRef.current = false;
    clearMusicHold();
    pendingSeekRef.current.a = null;
    pendingSeekRef.current.b = null;
    videoARef.current?.pause();
    videoBRef.current?.pause();
    for (const audio of getVirtualMusicAudio(musicAudioRef)) {
      audio.pause();
    }
    onPlayingChange(false);
  }, [clearMusicHold, onPlayingChange]);

  const loadDeck = useCallback(
    (deck: Deck, entry: VirtualTimelineEntry, timeS: number | null, play: boolean) => {
      const video = refForDeck(deck).current;
      if (!video || !entry.sourceUrl) return;

      const needsSource = deckSlotRef.current[deck] !== entry.slotKey || video.src !== entry.sourceUrl;
      if (needsSource) {
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
        void video.play().catch(() => {
          pauseAll();
        });
      }
    },
    [pauseAll, refForDeck],
  );

  const preloadNext = useCallback(
    (deck: Deck, afterEntryIndex: number) => {
      const next = nextVirtualEntry(timelineRef.current, afterEntryIndex);
      if (!next || !next.sourceUrl) return;
      loadDeck(deck, next, next.inS, false);
    },
    [loadDeck],
  );

  const syncMusicToVirtualTime = useCallback((virtualTimeS: number, play: boolean) => {
    const audio = getVirtualMusicAudio(musicAudioRef)[0];
    if (!audio || !musicAudioUrlRef.current) return;
    const musicTimeS = mapVirtualTimeToMusicTime(virtualTimeS, musicStartSRef.current);
    // Same tolerance as the drift resync in handleTimeUpdate: a tighter
    // threshold here made boundary swaps issue audible catch-up seeks.
    if (Math.abs(audio.currentTime - musicTimeS) > 0.25) {
      safeSetCurrentTime(audio, musicTimeS);
    }
    if (play && playingRef.current) {
      if (audio.paused) {
        void audio.play().catch(() => {
          pauseAll();
        });
      }
    } else {
      audio.pause();
    }
  }, [pauseAll]);

  const showMapping = useCallback(
    (timeS: number, play: boolean) => {
      const mapping = mapVirtualTime(timelineRef.current, timeS);
      if (!mapping || !mapping.entry.sourceUrl) {
        onSourceError();
        return;
      }

      const deck = activeDeckRef.current;
      loadDeck(deck, mapping.entry, mapping.sourceTimeS, play);
      preloadNext(otherDeck(deck), mapping.entryIndex);
      syncMusicToVirtualTime(mapping.virtualTimeS, play);
      onTimeUpdate(mapping.virtualTimeS);
    },
    [loadDeck, onSourceError, onTimeUpdate, preloadNext, syncMusicToVirtualTime],
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
    const activeVideo = refForDeck(activeDeckRef.current).current;
    if (activeVideo && !activeVideo.paused) pause();
    else play();
  }, [pause, play, refForDeck]);

  const swapToNext = useCallback(
    (entryIndex: number) => {
      const next = nextVirtualEntry(timelineRef.current, entryIndex);
      if (!next || !next.sourceUrl) {
        pause();
        onTimeUpdate(timelineRef.current.totalDurationS);
        return;
      }

      const prevDeck = activeDeckRef.current;
      const nextDeck = otherDeck(prevDeck);
      const prevVideo = refForDeck(prevDeck).current;

      prevVideo?.pause();
      // loadDeck owns the seek+play: covered decks seek and play immediately,
      // fresh sources defer to the onLoadedMetadata pending-seek. Seeking or
      // playing the element here as well made a fresh source play from frame
      // 0 and then snap to the in-point (visible "restart"/repeat).
      loadDeck(nextDeck, next, next.inS, true);
      activeDeckRef.current = nextDeck;
      setActiveDeck(nextDeck);
      preloadNext(prevDeck, entryIndex + 1);
      syncMusicToVirtualTime(next.startS, true);
      onTimeUpdate(next.startS);
    },
    [loadDeck, onTimeUpdate, pause, preloadNext, refForDeck, syncMusicToVirtualTime],
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
        onTimeUpdate(timelineRef.current.totalDurationS);
      } else if (playingRef.current) {
        swapToNext(entryIndex);
      }
    },
    [onTimeUpdate, pause, swapToNext],
  );

  const handleLoadedMetadata = useCallback(
    (deck: Deck) => {
      const video = refForDeck(deck).current;
      const pending = pendingSeekRef.current[deck];
      if (!video || !pending) return;
      pendingSeekRef.current[deck] = null;
      safeSetCurrentTime(video, pending.timeS);
      if (pending.play) {
        void video.play().catch(() => {
          pauseAll();
        });
      }
    },
    [pauseAll, refForDeck],
  );

  const handleTimeUpdate = useCallback(
    (deck: Deck) => {
      if (!enabledRef.current || deck !== activeDeckRef.current) return;
      const slotKey = deckSlotRef.current[deck];
      const video = refForDeck(deck).current;
      if (slotKey == null || !video) return;

      const entryIndex = timelineRef.current.entries.findIndex(
        (entry) => entry.slotKey === slotKey,
      );
      const entry = timelineRef.current.entries[entryIndex];
      if (!entry) return;

      const localOffsetS = video.currentTime - entry.inS;
      const virtualTimeS = Math.max(
        entry.startS,
        Math.min(entry.startS + entry.durationS, entry.startS + localOffsetS),
      );
      const audio = getVirtualMusicAudio(musicAudioRef)[0];
      if (audio && musicAudioUrlRef.current && !audio.paused) {
        const target = mapVirtualTimeToMusicTime(virtualTimeS, musicStartSRef.current);
        if (Math.abs(audio.currentTime - target) > 0.25) {
          safeSetCurrentTime(audio, target);
        }
      }
      onTimeUpdate(virtualTimeS);

      if (localOffsetS >= entry.durationS - 0.05) {
        finishEntry(entryIndex);
      }
    },
    [finishEntry, onTimeUpdate, refForDeck],
  );

  const handleEnded = useCallback(
    (deck: Deck) => {
      if (!enabledRef.current || deck !== activeDeckRef.current) return;
      const slotKey = deckSlotRef.current[deck];
      if (slotKey == null) return;
      const entryIndex = timelineRef.current.entries.findIndex(
        (entry) => entry.slotKey === slotKey,
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
      onCanPlay: () => setBuffering(false),
      onPlaying: () => {
        setBuffering(false);
        if (deck === activeDeckRef.current) {
          onPlayingChange(true);
          if (musicHoldTimerRef.current != null) {
            window.clearTimeout(musicHoldTimerRef.current);
            musicHoldTimerRef.current = null;
          }
          // Re-attach the music only if the stall-hold actually paused it —
          // touching a running music element at every boundary is itself a
          // stutter source.
          if (musicHeldRef.current && playingRef.current) {
            musicHeldRef.current = false;
            syncMusicToVirtualTime(currentTimeRef.current, true);
          }
        }
      },
      onWaiting: () => {
        setBuffering(true);
        // A genuinely stalled active deck freezes virtual time while the music
        // runs ahead; the >0.25s drift resync then seeks the music BACKWARD on
        // every timeupdate (audible skip-back). Hold the music — but only after
        // MUSIC_HOLD_DELAY_MS: boundary swaps fire a transient `waiting` on
        // nearly every cut, and an instant hold gapped the music at each one.
        if (deck === activeDeckRef.current && playingRef.current) {
          if (musicHoldTimerRef.current != null) {
            window.clearTimeout(musicHoldTimerRef.current);
          }
          musicHoldTimerRef.current = window.setTimeout(() => {
            musicHoldTimerRef.current = null;
            if (!playingRef.current) return;
            getVirtualMusicAudio(musicAudioRef)[0]?.pause();
            musicHeldRef.current = true;
          }, MUSIC_HOLD_DELAY_MS);
        }
      },
      onSeeking: () => setBuffering(true),
      onSeeked: () => setBuffering(false),
      onTimeUpdate: () => handleTimeUpdate(deck),
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
    videoAProps: videoProps("a"),
    videoBProps: videoProps("b"),
    musicAudioProps,
    play,
    pause,
    toggle,
    seekTo,
  };
}
