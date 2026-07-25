"use client";

import { useEffect, useRef } from "react";
import type { SoundEffectPlacement } from "@/lib/plan-api";
import { sfxPlaybackOffsetAt } from "@/lib/sfx-preview-scheduler";

interface SfxAudioEntry {
  placement: SoundEffectPlacement;
  audio: HTMLAudioElement;
  auxAudios: HTMLAudioElement[];
  gainNode: GainNode | null;
  scheduledAt: number | null; // timeout id
}

const MAX_NATIVE_GAIN_AUDIOS = 4;

export function shouldRouteSfxThroughWebAudio(url: string | undefined): boolean {
  if (!url) return false;
  if (url.startsWith("blob:") || url.startsWith("data:")) return true;
  try {
    const parsed = new URL(url, window.location.href);
    return parsed.origin === window.location.origin;
  } catch {
    return false;
  }
}

/**
 * Syncs one <audio> element per SFX placement to the main video element.
 * When the video plays/pauses/seeks, each audio element is positioned at
 * (video.currentTime - placement.at_s) and played/paused accordingly.
 *
 * audioUrls: map from src_gcs_path → playable URL (signed GCS or blob URL).
 */
export function useSfxPreview(
  videoRef: React.RefObject<HTMLVideoElement | null>,
  placements: SoundEffectPlacement[],
  audioUrls: Record<string, string>,
) {
  const entriesRef = useRef<SfxAudioEntry[]>([]);
  const timeoutsRef = useRef<number[]>([]);
  const audioContextRef = useRef<AudioContext | null>(null);

  function clearTimeouts() {
    timeoutsRef.current.forEach((t) => clearTimeout(t));
    timeoutsRef.current = [];
  }

  function setPreviewGain(entry: SfxAudioEntry) {
    const gain = Math.max(0, Math.min(4, entry.placement.gain ?? 1));
    if (entry.gainNode) {
      entry.audio.volume = 1;
      entry.gainNode.gain.value = gain;
      return;
    }
    const audios = [entry.audio, ...entry.auxAudios];
    const activeCount = Math.max(1, Math.min(MAX_NATIVE_GAIN_AUDIOS, Math.ceil(gain)));
    const perAudioVolume = activeCount === 0 ? 0 : Math.max(0, Math.min(1, gain / activeCount));
    audios.forEach((audio, index) => {
      audio.volume = index < activeCount ? perAudioVolume : 0;
      if (index >= activeCount) audio.pause();
    });
  }

  function setEntryUrl(entry: SfxAudioEntry, url: string) {
    for (const audio of [entry.audio, ...entry.auxAudios]) {
      if (audio.src !== url) {
        audio.src = url;
        audio.load();
      }
    }
  }

  function setEntryCurrentTime(entry: SfxAudioEntry, seconds: number) {
    for (const audio of [entry.audio, ...entry.auxAudios]) {
      if (audio.volume > 0) audio.currentTime = seconds;
    }
  }

  function pauseEntry(entry: SfxAudioEntry, clearSrc = false) {
    for (const audio of [entry.audio, ...entry.auxAudios]) {
      audio.pause();
      if (clearSrc) audio.src = "";
    }
  }

  function playPreviewAudio(entry: SfxAudioEntry) {
    const ctx = audioContextRef.current;
    if (ctx?.state === "suspended") {
      void ctx.resume().catch(() => {});
    }
    for (const audio of [entry.audio, ...entry.auxAudios]) {
      if (audio.volume > 0) void audio.play().catch(() => {});
    }
  }

  function syncAll(video: HTMLVideoElement) {
    clearTimeouts();
    const now = video.currentTime;
    for (const entry of entriesRef.current) {
      const { placement, audio } = entry;
      const url = audioUrls[placement.src_gcs_path] || audioUrls[placement.id] || (placement as unknown as { _previewUrl?: string })._previewUrl;
      if (!url) { pauseEntry(entry); continue; }
      setEntryUrl(entry, url);
      setPreviewGain(entry);

      const offsetInSfx = now - placement.at_s;
      const trimStartS = Math.max(0, placement.trim_start_s ?? 0);
      const activeOffset = sfxPlaybackOffsetAt(
        placement,
        now,
        audio.duration || 60,
      );

      if (video.paused) {
        pauseEntry(entry);
        if (activeOffset != null) {
          setEntryCurrentTime(entry, activeOffset);
        }
      } else {
        if (activeOffset != null) {
          // Already past the start — play from offset
          setEntryCurrentTime(entry, activeOffset);
          playPreviewAudio(entry);
        } else if (offsetInSfx >= 0) {
          pauseEntry(entry);
        } else {
          // Not yet — schedule a future play
          pauseEntry(entry);
          const delayMs = -offsetInSfx * 1000;
          const tid = window.setTimeout(() => {
            if (!video.paused) {
              setEntryCurrentTime(entry, trimStartS);
              playPreviewAudio(entry);
            }
          }, delayMs);
          timeoutsRef.current.push(tid);
        }
      }
    }
  }

  // Rebuild audio elements when placements change
  useEffect(() => {
    // Destroy old entries
    for (const entry of entriesRef.current) {
      pauseEntry(entry, true);
    }
    clearTimeouts();

    entriesRef.current = placements.map((p) => {
      const audio = new Audio();
      audio.preload = "auto";
      let auxAudios: HTMLAudioElement[] = [];
      let gainNode: GainNode | null = null;
      const url = audioUrls[p.src_gcs_path] || audioUrls[p.id] || (p as unknown as { _previewUrl?: string })._previewUrl;
      try {
        if (shouldRouteSfxThroughWebAudio(url)) {
          const AudioContextCtor =
            window.AudioContext ??
            (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
          if (!AudioContextCtor) throw new Error("AudioContext unavailable");
          audio.crossOrigin = "anonymous";
          const ctx = audioContextRef.current ?? new AudioContextCtor();
          audioContextRef.current = ctx;
          const source = ctx.createMediaElementSource(audio);
          gainNode = ctx.createGain();
          source.connect(gainNode);
          gainNode.connect(ctx.destination);
        }
      } catch {
        gainNode = null;
      }
      if (!gainNode) {
        auxAudios = Array.from({ length: MAX_NATIVE_GAIN_AUDIOS - 1 }, () => {
          const aux = new Audio();
          aux.preload = "auto";
          return aux;
        });
      }
      const entry = { placement: p, audio, auxAudios, gainNode, scheduledAt: null };
      if (url) setEntryUrl(entry, url);
      setPreviewGain(entry);
      return entry;
    });

    const video = videoRef.current;
    if (video) syncAll(video);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [placements, audioUrls]);

  // Attach video event listeners
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    const onPlay = () => syncAll(video);
    const onPause = () => {
      clearTimeouts();
      entriesRef.current.forEach((entry) => pauseEntry(entry));
    };
    const onSeeked = () => syncAll(video);
    const onEnded = () => {
      clearTimeouts();
      entriesRef.current.forEach((entry) => {
        pauseEntry(entry);
        setEntryCurrentTime(entry, 0);
      });
    };
    // A native <video loop> wraps to 0 WITHOUT firing `ended` or (in Chrome) a
    // reliable `seeked`, so the one-shot SFX timers scheduled in syncAll would
    // never re-arm — effects would play on the first pass only and stay silent on
    // every loop after. The looping preview is exactly LiveEditPreview's case.
    // Detect the backward jump on timeupdate and re-sync. (A manual seek-back also
    // lands here, harmlessly redundant with onSeeked since syncAll is idempotent.)
    let lastTime = video.currentTime;
    const onTimeUpdate = () => {
      if (video.currentTime + 0.25 < lastTime) syncAll(video);
      lastTime = video.currentTime;
    };

    video.addEventListener("play", onPlay);
    video.addEventListener("pause", onPause);
    video.addEventListener("seeked", onSeeked);
    video.addEventListener("ended", onEnded);
    video.addEventListener("timeupdate", onTimeUpdate);

    return () => {
      video.removeEventListener("play", onPlay);
      video.removeEventListener("pause", onPause);
      video.removeEventListener("seeked", onSeeked);
      video.removeEventListener("ended", onEnded);
      video.removeEventListener("timeupdate", onTimeUpdate);
      clearTimeouts();
      entriesRef.current.forEach((entry) => pauseEntry(entry, true));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoRef, placements, audioUrls]);
}
