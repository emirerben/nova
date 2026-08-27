"use client";

/**
 * Filmstrip — frame-thumbnail texture for one editor clip slot.
 *
 * Samples the slot's source video across the current source window
 * [in_s, in_s + duration_s]. The parent divides the global seek budget across
 * clip strips; this component buckets by tile count so zoom nudges don't
 * re-decode until the bucket changes.
 *
 * Decode failure (HDR/HEVC, expired URL, cross-origin readback we don't even
 * attempt) → a flat labelled bar with the clip duration, never an unexplained
 * blank. We draw video frames to canvas only for display, never for pixel
 * readback.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { formatSeconds } from "@/lib/timeline/time-format";

/** Hard cap on seeks across one redraw cycle for the whole track. */
export const FILMSTRIP_MAX_SEEKS = 24;
/** Target on-screen width of one thumbnail (drives the zoom bucket). */
export const FILMSTRIP_TILE_W = 56;

const TILE_H = 40;

interface FilmstripRequest {
  src: string | null;
  clipId: string;
  sourceId: string;
  sourceStartS: number;
  durationS: number;
  sourceDurationS: number | null;
  tiles: number;
  cacheKey: string;
}

const rasterCache = new Map<string, HTMLCanvasElement>();
const MAX_RASTER_CACHE_ENTRIES = 96;
const latestRequestByClip = new Map<string, string>();
const sourceVideos = new Map<
  string,
  { video: HTMLVideoElement; queue: Promise<void> }
>();

function roundKeyTiming(value: number): string {
  return `${Math.round(value * 1000) / 1000}`;
}

export function filmstripZoomBucket(
  widthPx: number,
  maxSeekCount = FILMSTRIP_MAX_SEEKS,
): number {
  if (widthPx <= 0 || maxSeekCount <= 0) return 0;
  return Math.max(
    1,
    Math.min(maxSeekCount, Math.round(widthPx / FILMSTRIP_TILE_W)),
  );
}

export function filmstripDecodeKey({
  clipId,
  sourceId,
  inS,
  durationS,
  zoomBucket,
}: {
  clipId: string;
  sourceId: string | number;
  inS: number;
  durationS: number;
  zoomBucket: number;
}): string {
  return [
    clipId,
    sourceId,
    roundKeyTiming(inS),
    roundKeyTiming(durationS),
    zoomBucket,
  ].join(":");
}

export function filmstripFallbackLabel(
  label: string | undefined,
  durationS: number,
): string {
  const trimmed = label?.trim();
  return trimmed && trimmed.length > 0 ? trimmed : formatSeconds(durationS);
}

export function filmstripSampleTimes({
  sourceStartS,
  durationS,
  sourceDurationS,
  tiles,
}: {
  sourceStartS: number;
  durationS: number;
  sourceDurationS: number | null;
  tiles: number;
}): number[] {
  if (tiles <= 0 || durationS <= 0) return [];
  const maxTime =
    sourceDurationS != null
      ? Math.max(0, sourceDurationS - 0.05)
      : Math.max(0, sourceStartS + durationS - 0.05);
  return Array.from({ length: tiles }, (_, index) =>
    Math.max(
      0,
      Math.min(
        sourceStartS + ((index + 0.5) / tiles) * durationS,
        maxTime,
      ),
    ),
  );
}

export function filmstripCoverCrop({
  sourceWidth,
  sourceHeight,
  targetWidth,
  targetHeight,
}: {
  sourceWidth: number;
  sourceHeight: number;
  targetWidth: number;
  targetHeight: number;
}): { sx: number; sy: number; sw: number; sh: number } {
  if (
    sourceWidth <= 0 ||
    sourceHeight <= 0 ||
    targetWidth <= 0 ||
    targetHeight <= 0
  ) {
    return { sx: 0, sy: 0, sw: 0, sh: 0 };
  }
  const sourceAspect = sourceWidth / sourceHeight;
  const targetAspect = targetWidth / targetHeight;
  if (sourceAspect > targetAspect) {
    const sw = sourceHeight * targetAspect;
    return { sx: (sourceWidth - sw) / 2, sy: 0, sw, sh: sourceHeight };
  }
  const sh = sourceWidth / targetAspect;
  return { sx: 0, sy: (sourceHeight - sh) / 2, sw: sourceWidth, sh };
}

export function allocateFilmstripSeekBudget(
  widthsPx: number[],
  budget = FILMSTRIP_MAX_SEEKS,
): number[] {
  const desired = widthsPx.map((width) =>
    width > 0 ? Math.max(1, Math.round(width / FILMSTRIP_TILE_W)) : 0,
  );
  const allocated = desired.map(() => 0);
  let remaining = Math.max(0, budget);
  const indices = desired
    .map((tiles, index) => ({ index, tiles }))
    .filter((entry) => entry.tiles > 0)
    .sort((a, b) => b.tiles - a.tiles);

  while (
    remaining > 0 &&
    indices.some(({ index, tiles }) => allocated[index] < tiles)
  ) {
    for (const { index, tiles } of indices) {
      if (remaining <= 0) break;
      if (allocated[index] >= tiles) continue;
      allocated[index] += 1;
      remaining -= 1;
    }
  }

  return allocated;
}

function pooledSourceVideo(src: string) {
  const existing = sourceVideos.get(src);
  if (existing) return existing;
  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.preload = "auto";
  const entry = { video, queue: Promise.resolve() };
  sourceVideos.set(src, entry);
  return entry;
}

function waitForLoadedData(video: HTMLVideoElement): Promise<void> {
  if (video.readyState >= 2) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => fail(), 5000);
    const done = () => {
      cleanup();
      resolve();
    };
    const fail = () => {
      cleanup();
      reject(new Error("filmstrip video failed to load"));
    };
    const cleanup = () => {
      window.clearTimeout(timeout);
      video.removeEventListener("loadeddata", done);
      video.removeEventListener("error", fail);
    };
    video.addEventListener("loadeddata", done, { once: true });
    video.addEventListener("error", fail, { once: true });
  });
}

function seekVideo(video: HTMLVideoElement, seconds: number): Promise<void> {
  if (video.readyState >= 2 && Math.abs(video.currentTime - seconds) < 0.02) {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => fail(), 2000);
    const done = () => {
      cleanup();
      resolve();
    };
    const fail = () => {
      cleanup();
      reject(new Error("filmstrip video seek failed"));
    };
    const cleanup = () => {
      window.clearTimeout(timeout);
      video.removeEventListener("seeked", done);
      video.removeEventListener("error", fail);
    };
    video.addEventListener("seeked", done, { once: true });
    video.addEventListener("error", fail, { once: true });
    video.currentTime = seconds;
  });
}

function enqueueSourceDecode(
  src: string,
  decode: (video: HTMLVideoElement) => Promise<void>,
): Promise<void> {
  const entry = pooledSourceVideo(src);
  const run = entry.queue
    .catch(() => undefined)
    .then(() => decode(entry.video));
  entry.queue = run.catch(() => undefined);
  return run;
}

function copyCanvas(source: HTMLCanvasElement, target: HTMLCanvasElement) {
  const ctx = target.getContext("2d");
  if (!ctx) return false;
  target.width = source.width;
  target.height = source.height;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, target.width, target.height);
  ctx.drawImage(source, 0, 0);
  return true;
}

function cacheRaster(key: string, canvas: HTMLCanvasElement) {
  rasterCache.delete(key);
  rasterCache.set(key, canvas);
  while (rasterCache.size > MAX_RASTER_CACHE_ENTRIES) {
    const oldest = rasterCache.keys().next().value as string | undefined;
    if (oldest == null) break;
    rasterCache.delete(oldest);
  }
}

export default function Filmstrip({
  src,
  clipId,
  sourceId,
  sourceStartS,
  durationS,
  sourceDurationS,
  widthPx,
  maxSeekCount = FILMSTRIP_MAX_SEEKS,
  label,
}: {
  src: string | null;
  clipId: string;
  sourceId: string | number;
  sourceStartS: number;
  durationS: number;
  sourceDurationS?: number | null;
  /** Rendered clip width, bucketed into a bounded seek count. */
  widthPx: number;
  /** Seek budget allocated to this clip by the parent track. */
  maxSeekCount?: number;
  /** Fallback label (clip duration + moment description). */
  label?: string;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [failed, setFailed] = useState(false);

  const tiles = filmstripZoomBucket(widthPx, maxSeekCount);
  const request = useMemo<FilmstripRequest>(
    () => ({
      src,
      clipId,
      sourceId: `${sourceId}`,
      sourceStartS,
      durationS,
      sourceDurationS: sourceDurationS ?? null,
      tiles,
      cacheKey: filmstripDecodeKey({
        clipId,
        sourceId,
        inS: sourceStartS,
        durationS,
        zoomBucket: tiles,
      }),
    }),
    [clipId, durationS, sourceDurationS, sourceId, sourceStartS, src, tiles],
  );
  const activeRequest = request;
  const sampleTimes = useMemo(
    () =>
      filmstripSampleTimes({
        sourceStartS: activeRequest.sourceStartS,
        durationS: activeRequest.durationS,
        sourceDurationS: activeRequest.sourceDurationS,
        tiles: activeRequest.tiles,
      }),
    [activeRequest],
  );

  useEffect(() => {
    if (
      !activeRequest.src ||
      activeRequest.durationS <= 0 ||
      activeRequest.tiles <= 0
    ) {
      setFailed(true);
      return;
    }

    let cancelled = false;
    latestRequestByClip.set(activeRequest.clipId, activeRequest.cacheKey);
    setFailed(false);

    const canvas = canvasRef.current;
    if (!canvas) return;

    const dpr = Math.min(
      2,
      typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1,
    );
    const cached = rasterCache.get(activeRequest.cacheKey);
    if (cached) {
      if (!copyCanvas(cached, canvas)) setFailed(true);
      else canvas.dataset.renderedTiles = `${activeRequest.tiles}`;
      return;
    }

    const failTimer = window.setTimeout(() => {
      if (!cancelled) setFailed(true);
    }, 6000);

    void enqueueSourceDecode(activeRequest.src, async (video) => {
      const isStale = () =>
        cancelled ||
        latestRequestByClip.get(activeRequest.clipId) !== activeRequest.cacheKey;
      if (isStale()) return;
      if (video.getAttribute("src") !== activeRequest.src) {
        video.src = activeRequest.src!;
        video.load();
      }
      await waitForLoadedData(video);
      if (isStale()) return;
      if (video.videoWidth <= 0 || video.videoHeight <= 0) {
        throw new Error("filmstrip source has no drawable video frame");
      }

      const rendered = document.createElement("canvas");
      const renderedWidth = Math.max(1, activeRequest.tiles * FILMSTRIP_TILE_W);
      rendered.width = Math.round(renderedWidth * dpr);
      rendered.height = TILE_H * dpr;
      const ctx = rendered.getContext("2d");
      const visibleCtx = canvas.getContext("2d");
      if (!ctx || !visibleCtx) throw new Error("filmstrip canvas unavailable");
      canvas.width = rendered.width;
      canvas.height = rendered.height;
      ctx.scale(dpr, dpr);
      visibleCtx.scale(dpr, dpr);
      ctx.fillStyle = "#e4e4e7";
      ctx.fillRect(0, 0, renderedWidth, TILE_H);
      visibleCtx.fillStyle = "#e4e4e7";
      visibleCtx.fillRect(0, 0, renderedWidth, TILE_H);
      const crop = filmstripCoverCrop({
        sourceWidth: video.videoWidth,
        sourceHeight: video.videoHeight,
        targetWidth: FILMSTRIP_TILE_W,
        targetHeight: TILE_H,
      });

      for (let i = 0; i < sampleTimes.length; i += 1) {
        if (isStale()) return;
        await seekVideo(video, sampleTimes[i]);
        if (isStale()) return;
        if (video.videoWidth <= 0 || video.videoHeight <= 0) {
          throw new Error("filmstrip source has no drawable video frame");
        }
        ctx.drawImage(
          video,
          crop.sx,
          crop.sy,
          crop.sw,
          crop.sh,
          i * FILMSTRIP_TILE_W,
          0,
          FILMSTRIP_TILE_W,
          TILE_H,
        );
        visibleCtx.drawImage(
          video,
          crop.sx,
          crop.sy,
          crop.sw,
          crop.sh,
          i * FILMSTRIP_TILE_W,
          0,
          FILMSTRIP_TILE_W,
          TILE_H,
        );
        canvas.dataset.renderedTiles = `${i + 1}`;
      }

      cacheRaster(activeRequest.cacheKey, rendered);
    })
      .catch(() => {
        if (!cancelled) setFailed(true);
      })
      .finally(() => {
        if (!cancelled) window.clearTimeout(failTimer);
      });

    return () => {
      cancelled = true;
      window.clearTimeout(failTimer);
    };
  }, [activeRequest, sampleTimes]);

  if (failed) {
    const fallbackText = filmstripFallbackLabel(label, durationS);
    return (
      <div
        data-testid="editor-filmstrip"
        data-clip-key={clipId}
        data-source-range-key={activeRequest.cacheKey}
        data-sample-times={sampleTimes.map(roundKeyTiming).join(",")}
        className="flex h-full w-full items-center justify-center overflow-hidden rounded bg-zinc-100 px-2"
      >
        {fallbackText ? (
          <span className="truncate text-[10px] text-[#71717a]">
            {fallbackText}
          </span>
        ) : null}
      </div>
    );
  }

  return (
    <canvas
      ref={canvasRef}
      data-testid="editor-filmstrip"
      data-clip-key={clipId}
      data-source-range-key={activeRequest.cacheKey}
      data-sample-times={sampleTimes.map(roundKeyTiming).join(",")}
      aria-hidden
      className="h-full w-full rounded object-cover [contain:paint]"
      style={{ imageRendering: "auto" }}
    />
  );
}
