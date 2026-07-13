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
  const deckSlotRef = useRef<Record<Deck, number | null>>({ a: null, b: null });
  const pendingSeekRef = useRef<Record<Deck, PendingSeek | null>>({ a: null, b: null });
  const playingRef = useRef(false);

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

  const pauseAll = useCallback(() => {
    playingRef.current = false;
    pendingSeekRef.current.a = null;
    pendingSeekRef.current.b = null;
    videoARef.current?.pause();
    videoBRef.current?.pause();
    for (const audio of getVirtualMusicAudio(musicAudioRef)) {
      audio.pause();
    }
    onPlayingChange(false);
  }, [onPlayingChange]);

  const loadDeck = useCallback(
    (deck: Deck, entry: VirtualTimelineEntry, timeS: number | null, play: boolean) => {
      const video = refForDeck(deck).current;
      if (!video || !entry.sourceUrl) return;

      const needsSource = deckSlotRef.current[deck] !== entry.slotIndex || video.src !== entry.sourceUrl;
      if (needsSource) {
        deckSlotRef.current[deck] = entry.slotIndex;
        pendingSeekRef.current[deck] = timeS == null ? null : { timeS, play };
        video.src = entry.sourceUrl;
        video.preload = "auto";
        video.load();
        return;
      }

      if (timeS != null) safeSetCurrentTime(video, timeS);
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
    if (Math.abs(audio.currentTime - musicTimeS) > 0.08) {
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
      const nextVideo = refForDeck(nextDeck).current;

      prevVideo?.pause();
      loadDeck(nextDeck, next, next.inS, true);
      activeDeckRef.current = nextDeck;
      setActiveDeck(nextDeck);
      preloadNext(prevDeck, entryIndex + 1);
      syncMusicToVirtualTime(next.startS, true);
      onTimeUpdate(next.startS);

      if (nextVideo) {
        safeSetCurrentTime(nextVideo, next.inS);
        void nextVideo.play().catch(() => {
          pauseAll();
        });
      }
    },
    [loadDeck, onTimeUpdate, pause, pauseAll, preloadNext, refForDeck, syncMusicToVirtualTime],
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
      const slotIndex = deckSlotRef.current[deck];
      const video = refForDeck(deck).current;
      if (slotIndex == null || !video) return;

      const entryIndex = timelineRef.current.entries.findIndex(
        (entry) => entry.slotIndex === slotIndex,
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
      const slotIndex = deckSlotRef.current[deck];
      if (slotIndex == null) return;
      const entryIndex = timelineRef.current.entries.findIndex(
        (entry) => entry.slotIndex === slotIndex,
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
    showMapping(currentTimeRef.current, false);
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
        if (deck === activeDeckRef.current) onPlayingChange(true);
      },
      onWaiting: () => setBuffering(true),
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
