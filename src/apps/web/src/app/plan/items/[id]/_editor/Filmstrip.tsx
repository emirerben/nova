"use client";

/**
 * Filmstrip — frame-thumbnail texture for the editor Video lane (plan §5/§6).
 *
 * Seeks an offscreen <video> on the existing signed preview URL and paints
 * evenly-spaced frames into one canvas, ≤ MAX_SEEKS per zoom bucket (bucket =
 * tile count, derived from the rendered width). Tiles stretch between seeks.
 *
 * Decode failure (HDR/HEVC, expired URL, cross-origin readback we don't even
 * attempt) → a flat labelled bar with the clip duration, never an unexplained
 * blank. We draw the video straight to the visible canvas (no toDataURL), so a
 * cross-origin/tainted frame still displays; only pixel readback would be
 * blocked, which we never do.
 */

import { useEffect, useRef, useState } from "react";
import { formatSeconds } from "@/lib/timeline/time-format";

/** Hard cap on seeks per bucket (plan §5 filmstrip cap). */
const MAX_SEEKS = 24;
/** Target on-screen width of one thumbnail (drives the bucket / tile count). */
const TILE_W = 56;
const TILE_H = 40;

export default function Filmstrip({
  src,
  durationS,
  widthPx,
  label,
}: {
  src: string;
  durationS: number;
  /** Rendered track width — buckets the seek count so we re-tile per zoom. */
  widthPx: number;
  /** Fallback label (clip duration + moment description). */
  label?: string;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [failed, setFailed] = useState(false);

  // Bucket by tile count so a small zoom nudge doesn't re-seek; only crossing
  // a tile-count boundary re-decodes.
  const tiles = Math.max(1, Math.min(MAX_SEEKS, Math.round(widthPx / TILE_W)));

  useEffect(() => {
    if (!src || durationS <= 0) return;
    let cancelled = false;
    setFailed(false);

    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = Math.min(2, typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1);
    canvas.width = tiles * TILE_W * dpr;
    canvas.height = TILE_H * dpr;
    ctx.scale(dpr, dpr);

    const video = document.createElement("video");
    video.src = src;
    video.muted = true;
    video.playsInline = true;
    video.preload = "auto";
    video.crossOrigin = "anonymous";

    const seekTimes = Array.from(
      { length: tiles },
      (_, i) => ((i + 0.5) / tiles) * durationS,
    );

    let i = 0;
    const failTimer = window.setTimeout(() => {
      if (!cancelled && i === 0) setFailed(true);
    }, 6000);

    function drawNext() {
      if (cancelled || i >= seekTimes.length) {
        window.clearTimeout(failTimer);
        return;
      }
      try {
        video.currentTime = Math.min(seekTimes[i], Math.max(0, durationS - 0.05));
      } catch {
        setFailed(true);
      }
    }

    function onSeeked() {
      if (cancelled) return;
      try {
        ctx!.drawImage(video, i * TILE_W, 0, TILE_W, TILE_H);
      } catch {
        // Drawing itself failed (rare) — bail to the fallback bar.
        setFailed(true);
        return;
      }
      i += 1;
      drawNext();
    }

    function onError() {
      if (!cancelled) setFailed(true);
    }

    video.addEventListener("seeked", onSeeked);
    video.addEventListener("error", onError);
    video.addEventListener("loadeddata", drawNext, { once: true });

    return () => {
      cancelled = true;
      window.clearTimeout(failTimer);
      video.removeEventListener("seeked", onSeeked);
      video.removeEventListener("error", onError);
      video.removeEventListener("loadeddata", drawNext);
      video.removeAttribute("src");
      video.load();
    };
  }, [src, durationS, tiles]);

  if (failed) {
    return (
      <div className="flex h-full w-full items-center justify-center overflow-hidden rounded bg-zinc-100 px-2">
        <span className="truncate text-[10px] text-[#71717a]">
          {label ?? formatSeconds(durationS)}
        </span>
      </div>
    );
  }

  return (
    <canvas
      ref={canvasRef}
      aria-hidden
      className="h-full w-full rounded object-cover"
      style={{ imageRendering: "auto" }}
    />
  );
}
