"use client";

import { useEffect, useRef } from "react";
import type { SoundEffectPlacement } from "@/lib/plan-api";
import { sfxPlaybackOffsetAt } from "@/lib/sfx-preview-scheduler";

interface SfxAudioEntry {
  placement: SoundEffectPlacement;
  audio: HTMLAudioElement;
  gainNode: GainNode | null;
  scheduledAt: number | null; // timeout id
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
    entry.audio.volume = Math.max(0, Math.min(1, gain));
  }

  function playPreviewAudio(audio: HTMLAudioElement) {
    const ctx = audioContextRef.current;
    if (ctx?.state === "suspended") {
      void ctx.resume().catch(() => {});
    }
    void audio.play().catch(() => {});
  }

  function syncAll(video: HTMLVideoElement) {
    clearTimeouts();
    const now = video.currentTime;
    for (const entry of entriesRef.current) {
      const { placement, audio } = entry;
      const url = audioUrls[placement.src_gcs_path] || audioUrls[placement.id] || (placement as unknown as { _previewUrl?: string })._previewUrl;
      if (!url) { audio.pause(); continue; }
      if (audio.src !== url) {
        audio.src = url;
        audio.load();
      }
      setPreviewGain(entry);

      const offsetInSfx = now - placement.at_s;
      const trimStartS = Math.max(0, placement.trim_start_s ?? 0);
      const activeOffset = sfxPlaybackOffsetAt(
        placement,
        now,
        audio.duration || 60,
      );

      if (video.paused) {
        audio.pause();
        if (activeOffset != null) {
          audio.currentTime = activeOffset;
        }
      } else {
        if (activeOffset != null) {
          // Already past the start — play from offset
          audio.currentTime = activeOffset;
          playPreviewAudio(audio);
        } else if (offsetInSfx >= 0) {
          audio.pause();
        } else {
          // Not yet — schedule a future play
          audio.pause();
          const delayMs = -offsetInSfx * 1000;
          const tid = window.setTimeout(() => {
            if (!video.paused) {
              audio.currentTime = trimStartS;
              playPreviewAudio(audio);
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
      entry.audio.pause();
      entry.audio.src = "";
    }
    clearTimeouts();

    entriesRef.current = placements.map((p) => {
      const audio = new Audio();
      audio.preload = "auto";
      let gainNode: GainNode | null = null;
      try {
        const AudioContextCtor =
          window.AudioContext ??
          (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
        if (AudioContextCtor) {
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
      const url = audioUrls[p.src_gcs_path] || audioUrls[p.id] || (p as unknown as { _previewUrl?: string })._previewUrl;
      if (url) { audio.src = url; audio.load(); }
      const entry = { placement: p, audio, gainNode, scheduledAt: null };
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
      entriesRef.current.forEach(({ audio }) => audio.pause());
    };
    const onSeeked = () => syncAll(video);
    const onEnded = () => {
      clearTimeouts();
      entriesRef.current.forEach(({ audio }) => { audio.pause(); audio.currentTime = 0; });
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
      entriesRef.current.forEach(({ audio }) => { audio.pause(); audio.src = ""; });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoRef, placements, audioUrls]);
}
