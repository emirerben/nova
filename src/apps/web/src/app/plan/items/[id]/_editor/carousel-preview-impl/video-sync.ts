"use client";

import { useEffect, type RefObject } from "react";

// Resync a playing video if it drifts this far from target before forcing a
// seek (never re-seek every render just because of sub-frame float noise —
// that's what causes visible stutter; small native-clock drift self-heals).
const DRIFT_CORRECT_EPS_S = 0.15;
// Skip no-op seeks while paused/holding/scrubbing (avoid seek churn).
const SEEK_EPS_S = 0.03;

function safeSeek(video: HTMLVideoElement, timeS: number): void {
  try {
    video.currentTime = Math.max(0, timeS);
  } catch {
    // Some browsers reject seeks issued before metadata is ready — harmless,
    // the next sync tick retries once currentTime becomes settable.
  }
}

function playQuietly(video: HTMLVideoElement): void {
  // Handle play() promise rejections quietly: autoplay-policy denials, or an
  // AbortError from a fast src/seek change racing the pending play() — never
  // fatal for a muted preview tile.
  void video.play().catch(() => {});
}

export interface CardVideoTarget {
  /** false = hold at `timeS` (paused, e.g. an unfocused focus-mode tile). */
  active: boolean;
  /** Desired video-local playback position, in seconds. */
  timeS: number;
}

/**
 * Syncs one card's `<video>` element to `target`, re-evaluated every time
 * `target` or `isPlaying` changes (i.e. every parent re-render driven by a
 * fresh `currentTimeS`/transport-state prop — see CarouselBlockPreviewImpl's
 * docstring on why rendering is props-driven rather than rAF-driven).
 *
 * `isPlaying` is the editor transport's REAL play/pause state (threaded down
 * from EditorCanvas's own `playing` prop — no inference). Two branches:
 *  - `active && isPlaying`: the video plays forward on its OWN native clock
 *    (never re-seeked every frame — only a light drift correction if it
 *    wanders past `DRIFT_CORRECT_EPS_S`).
 *  - everything else (`!active`, or `active && !isPlaying` i.e. paused/
 *    scrubbing): the video is paused and precisely seeked to `target.timeS`.
 */
export function useCardVideoSync(
  videoRef: RefObject<HTMLVideoElement | null>,
  target: CardVideoTarget,
  isPlaying: boolean,
): void {
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    if (target.active && isPlaying) {
      if (video.paused) playQuietly(video);
      if (Math.abs(video.currentTime - target.timeS) > DRIFT_CORRECT_EPS_S) {
        safeSeek(video, target.timeS);
      }
      return;
    }

    // !active, or active-but-paused/scrubbing: always pause (idempotent —
    // harmless on an already-paused video) and seek precisely.
    video.pause();
    if (Math.abs(video.currentTime - target.timeS) > SEEK_EPS_S) {
      safeSeek(video, target.timeS);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- videoRef identity is stable (useRef)
  }, [target.active, target.timeS, isPlaying]);
}
