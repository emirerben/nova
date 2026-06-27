"use client";

import { useEffect, useRef } from "react";
import type { SoundEffectPlacement } from "@/lib/plan-api";

interface SfxAudioEntry {
  placement: SoundEffectPlacement;
  audio: HTMLAudioElement;
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

  function clearTimeouts() {
    timeoutsRef.current.forEach((t) => clearTimeout(t));
    timeoutsRef.current = [];
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
        audio.volume = Math.min(2, Math.max(0, placement.gain ?? 1));
        audio.load();
      }

      const offsetInSfx = now - placement.at_s;

      if (video.paused) {
        audio.pause();
        if (offsetInSfx >= 0 && offsetInSfx < (placement.duration_s ?? (audio.duration || 60))) {
          audio.currentTime = offsetInSfx;
        }
      } else {
        if (offsetInSfx >= 0) {
          // Already past the start — play from offset
          const dur = placement.duration_s ?? (audio.duration || 60);
          if (offsetInSfx < dur) {
            audio.currentTime = offsetInSfx;
            audio.play().catch(() => {});
          } else {
            audio.pause();
          }
        } else {
          // Not yet — schedule a future play
          audio.pause();
          const delayMs = -offsetInSfx * 1000;
          const tid = window.setTimeout(() => {
            if (!video.paused) {
              audio.currentTime = 0;
              audio.play().catch(() => {});
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
      audio.volume = Math.min(2, Math.max(0, p.gain ?? 1));
      const url = audioUrls[p.src_gcs_path] || audioUrls[p.id] || (p as unknown as { _previewUrl?: string })._previewUrl;
      if (url) { audio.src = url; audio.load(); }
      return { placement: p, audio, scheduledAt: null };
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

    video.addEventListener("play", onPlay);
    video.addEventListener("pause", onPause);
    video.addEventListener("seeked", onSeeked);
    video.addEventListener("ended", onEnded);

    return () => {
      video.removeEventListener("play", onPlay);
      video.removeEventListener("pause", onPause);
      video.removeEventListener("seeked", onSeeked);
      video.removeEventListener("ended", onEnded);
      clearTimeouts();
      entriesRef.current.forEach(({ audio }) => { audio.pause(); audio.src = ""; });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoRef, placements, audioUrls]);
}
