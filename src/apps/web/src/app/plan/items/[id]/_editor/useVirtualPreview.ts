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
  onTimeUpdate: (timeS: number) => void;
  onDuration: (durationS: number) => void;
  onPlayingChange: (playing: boolean) => void;
  onSourceError: () => void;
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
  onTimeUpdate,
  onDuration,
  onPlayingChange,
  onSourceError,
}: UseVirtualPreviewOptions): VirtualPreviewController {
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
      if (video) video.muted = muted;
    }
  }, [muted]);

  useEffect(() => {
    const audio = musicAudioRef.current;
    if (audio) audio.muted = soundMuted;
  }, [soundMuted]);

  const refForDeck = useCallback((deck: Deck) => {
    return deck === "a" ? videoARef : videoBRef;
  }, []);

  const pauseAll = useCallback(() => {
    pendingSeekRef.current.a = null;
    pendingSeekRef.current.b = null;
    videoARef.current?.pause();
    videoBRef.current?.pause();
    musicAudioRef.current?.pause();
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
    const audio = musicAudioRef.current;
    if (!audio || !musicAudioUrlRef.current) return;
    const musicTimeS = mapVirtualTimeToMusicTime(virtualTimeS, musicStartSRef.current);
    if (Math.abs(audio.currentTime - musicTimeS) > 0.08) {
      safeSetCurrentTime(audio, musicTimeS);
    }
    if (play) {
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
          onPlayingChange(false);
        });
      }
    },
    [loadDeck, onPlayingChange, onTimeUpdate, pause, preloadNext, refForDeck, syncMusicToVirtualTime],
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
      const audio = musicAudioRef.current;
      if (audio && musicAudioUrlRef.current && !audio.paused) {
        const target = mapVirtualTimeToMusicTime(virtualTimeS, musicStartSRef.current);
        if (Math.abs(audio.currentTime - target) > 0.25) {
          safeSetCurrentTime(audio, target);
        }
      }
      onTimeUpdate(virtualTimeS);

      if (localOffsetS >= entry.durationS - 0.05) {
        if (entry.startS + entry.durationS >= timelineRef.current.totalDurationS - 0.05) {
          pause();
          onTimeUpdate(timelineRef.current.totalDurationS);
        } else {
          swapToNext(entryIndex);
        }
      }
    },
    [onTimeUpdate, pause, refForDeck, swapToNext],
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

  const musicAudioProps: VirtualPreviewAudioProps | null = musicAudioUrl
    ? {
        ref: musicAudioRef,
        src: musicAudioUrl,
        muted: soundMuted,
        preload: "auto",
        "data-virtual-preview-music": true,
        onError: () => {
          musicAudioRef.current?.pause();
        },
      }
    : null;

  function videoProps(deck: Deck): VirtualPreviewVideoProps {
    return {
      ref: refForDeck(deck),
      muted,
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
      onEnded: () => handleTimeUpdate(deck),
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
