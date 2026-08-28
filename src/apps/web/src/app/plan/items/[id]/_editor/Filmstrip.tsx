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

const DEFAULT_TILE_H = 40;
const IDLE_DECODER_TTL_MS = 15_000;
const MAX_CONCURRENT_FILMSTRIP_DECODES = 3;
const MAX_RASTER_CACHE_BYTES = 32 * 1024 * 1024;

interface FilmstripRequest {
  src: string | null;
  clipId: string;
  sourceId: string;
  sourceStartS: number;
  durationS: number;
  sourceDurationS: number | null;
  tiles: number;
  rasterWidthPx: number;
  rasterHeightPx: number;
  cacheKey: string;
}

const rasterCache = new Map<string, HTMLCanvasElement>();
const MAX_RASTER_CACHE_ENTRIES = 96;
let rasterCacheBytes = 0;
let activeFilmstripDecodes = 0;
const filmstripDecodeWaiters: Array<() => void> = [];
const latestRequestByPoolKey = new Map<string, string>();
const clipVideos = new Map<
  string,
  {
    video: HTMLVideoElement;
    queue: Promise<void>;
    refs: number;
    releaseTimer: number | null;
  }
>();

export function filmstripPoolKey(src: string, clipId: string): string {
  return `${src}\u0000${clipId}`;
}

function roundKeyTiming(value: number): string {
  return `${Math.round(value * 1000) / 1000}`;
}

export function filmstripZoomBucket(
  widthPx: number,
  maxSeekCount = FILMSTRIP_MAX_SEEKS,
  minSeekCount = 0,
): number {
  if (widthPx <= 0 || maxSeekCount <= 0) return 0;
  const boundedMinimum = Math.max(0, Math.min(maxSeekCount, minSeekCount));
  return Math.max(
    1,
    Math.min(
      maxSeekCount,
      Math.max(boundedMinimum, Math.round(widthPx / FILMSTRIP_TILE_W)),
    ),
  );
}

/** Keep canvas backing stores bounded even when a long clip is highly zoomed. */
export function filmstripRasterWidth(widthPx: number, tiles: number): number {
  return Math.max(
    1,
    Math.min(Math.round(widthPx), Math.max(1, tiles) * FILMSTRIP_TILE_W),
  );
}

export function filmstripDecodeKey({
  clipId,
  sourceId,
  inS,
  durationS,
  zoomBucket,
  rasterWidthPx = zoomBucket * FILMSTRIP_TILE_W,
  rasterHeightPx = DEFAULT_TILE_H,
}: {
  clipId: string;
  sourceId: string | number;
  inS: number;
  durationS: number;
  zoomBucket: number;
  rasterWidthPx?: number;
  rasterHeightPx?: number;
}): string {
  return [
    clipId,
    sourceId,
    roundKeyTiming(inS),
    roundKeyTiming(durationS),
    zoomBucket,
    `${Math.max(1, Math.round(rasterWidthPx))}x${Math.max(1, Math.round(rasterHeightPx))}`,
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
  const windowStartS = Math.max(0, Math.min(sourceStartS, maxTime));
  const windowEndS = Math.max(
    windowStartS,
    Math.min(sourceStartS + durationS, maxTime),
  );
  const drawableDurationS = windowEndS - windowStartS;
  return Array.from({ length: tiles }, (_, index) =>
    windowStartS + ((index + 0.5) / tiles) * drawableDurationS,
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

/** Match the desktop editor's dense sampling without exceeding the track cap. */
export function allocateFilmstripDensityBudget(
  widthsPx: number[],
  zoom: number,
  budget = FILMSTRIP_MAX_SEEKS,
): number[] {
  const activeCount = widthsPx.filter((width) => width > 0).length;
  if (activeCount === 0 || budget <= 0) return widthsPx.map(() => 0);
  const perClipCap = Math.max(1, Math.floor(budget / activeCount));
  const safeZoom = Number.isFinite(zoom) ? Math.max(0.1, zoom) : 1;
  const zoomDensity = Math.max(1, Math.round(safeZoom * 10));
  const perClipBudget = Math.min(perClipCap, zoomDensity);
  return widthsPx.map((width) => (width > 0 ? perClipBudget : 0));
}

function pooledClipVideo(src: string, clipId: string) {
  const poolKey = filmstripPoolKey(src, clipId);
  const existing = clipVideos.get(poolKey);
  if (existing) {
    if (existing.releaseTimer != null) {
      window.clearTimeout(existing.releaseTimer);
      existing.releaseTimer = null;
    }
    existing.refs += 1;
    return existing;
  }
  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.preload = "auto";
  const entry = {
    video,
    queue: Promise.resolve(),
    refs: 1,
    releaseTimer: null,
  };
  clipVideos.set(poolKey, entry);
  return entry;
}

function releaseClipVideo(
  poolKey: string,
  entry: {
    video: HTMLVideoElement;
    queue: Promise<void>;
    refs: number;
    releaseTimer: number | null;
  },
) {
  entry.refs -= 1;
  if (
    entry.refs > 0 ||
    entry.releaseTimer != null ||
    clipVideos.get(poolKey) !== entry
  ) {
    return;
  }
  entry.releaseTimer = window.setTimeout(() => {
    if (entry.refs > 0 || clipVideos.get(poolKey) !== entry) return;
    clipVideos.delete(poolKey);
    entry.video.pause();
    entry.video.removeAttribute("src");
    entry.video.load();
  }, IDLE_DECODER_TTL_MS);
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

function waitForDrawableFrame(video: HTMLVideoElement): Promise<void> {
  if (typeof video.requestVideoFrameCallback !== "function") {
    return new Promise((resolve) => requestAnimationFrame(() => resolve()));
  }
  return new Promise((resolve) => {
    let settled = false;
    let callbackId = 0;
    const finish = () => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timeout);
      if (callbackId) video.cancelVideoFrameCallback(callbackId);
      resolve();
    };
    const timeout = window.setTimeout(finish, 120);
    callbackId = video.requestVideoFrameCallback(() => finish());
  });
}

function enqueueClipDecode(
  src: string,
  clipId: string,
  decode: (video: HTMLVideoElement) => Promise<void>,
): Promise<void> {
  return withFilmstripDecodeSlot(async () => {
    const poolKey = filmstripPoolKey(src, clipId);
    const entry = pooledClipVideo(src, clipId);
    const run = entry.queue
      .catch(() => undefined)
      .then(() => decode(entry.video));
    entry.queue = run.catch(() => undefined);
    return run.finally(() => releaseClipVideo(poolKey, entry));
  });
}

function withFilmstripDecodeSlot<T>(task: () => Promise<T>): Promise<T> {
  const acquire = () => {
    if (activeFilmstripDecodes < MAX_CONCURRENT_FILMSTRIP_DECODES) {
      activeFilmstripDecodes += 1;
      return Promise.resolve();
    }
    return new Promise<void>((resolve) => filmstripDecodeWaiters.push(resolve));
  };
  return acquire()
    .then(task)
    .finally(() => {
      const next = filmstripDecodeWaiters.shift();
      if (next) next();
      else activeFilmstripDecodes = Math.max(0, activeFilmstripDecodes - 1);
    });
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
  const existing = rasterCache.get(key);
  if (existing) {
    rasterCacheBytes -= existing.width * existing.height * 4;
    rasterCache.delete(key);
  }
  rasterCache.set(key, canvas);
  rasterCacheBytes += canvas.width * canvas.height * 4;
  while (
    rasterCache.size > MAX_RASTER_CACHE_ENTRIES ||
    rasterCacheBytes > MAX_RASTER_CACHE_BYTES
  ) {
    const oldest = rasterCache.keys().next().value as string | undefined;
    if (oldest == null) break;
    const evicted = rasterCache.get(oldest);
    if (evicted) rasterCacheBytes -= evicted.width * evicted.height * 4;
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
  heightPx = DEFAULT_TILE_H,
  maxSeekCount = FILMSTRIP_MAX_SEEKS,
  minSeekCount = 0,
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
  /** Actual rendered strip height; prevents CSS from stretching frame crops. */
  heightPx?: number;
  /** Seek budget allocated to this clip by the parent track. */
  maxSeekCount?: number;
  /** Optional dense floor, still capped by maxSeekCount. */
  minSeekCount?: number;
  /** Fallback label (clip duration + moment description). */
  label?: string;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [failed, setFailed] = useState(false);

  const tiles = filmstripZoomBucket(widthPx, maxSeekCount, minSeekCount);
  const rasterWidthPx = filmstripRasterWidth(widthPx, tiles);
  const rasterHeightPx = Math.max(1, Math.round(heightPx));
  const request = useMemo<FilmstripRequest>(
    () => ({
      src,
      clipId,
      sourceId: `${sourceId}`,
      sourceStartS,
      durationS,
      sourceDurationS: sourceDurationS ?? null,
      tiles,
      rasterWidthPx,
      rasterHeightPx,
      cacheKey: filmstripDecodeKey({
        clipId,
        sourceId,
        inS: sourceStartS,
        durationS,
        zoomBucket: tiles,
        rasterWidthPx,
        rasterHeightPx,
      }),
    }),
    [
      clipId,
      durationS,
      rasterHeightPx,
      rasterWidthPx,
      sourceDurationS,
      sourceId,
      sourceStartS,
      src,
      tiles,
    ],
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
    const poolKey = filmstripPoolKey(
      activeRequest.src,
      activeRequest.clipId,
    );
    latestRequestByPoolKey.set(poolKey, activeRequest.cacheKey);
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
      else {
        canvas.dataset.renderedTiles = `${activeRequest.tiles}`;
        canvas.dataset.renderedRangeKey = activeRequest.cacheKey;
      }
      return;
    }

    const renderedWidth = activeRequest.rasterWidthPx;
    const renderedHeight = activeRequest.rasterHeightPx;
    if (typeof window.CanvasRenderingContext2D === "undefined") {
      setFailed(true);
      return;
    }
    const hadRenderedRaster = Boolean(canvas.dataset.renderedRangeKey);
    canvas.dataset.loadingRangeKey = activeRequest.cacheKey;

    const failTimer = window.setTimeout(() => {
      if (!cancelled && !hadRenderedRaster) setFailed(true);
    }, 6000);

    void enqueueClipDecode(
      activeRequest.src,
      activeRequest.clipId,
      async (video) => {
        const isStale = () =>
          cancelled ||
          latestRequestByPoolKey.get(poolKey) !==
            activeRequest.cacheKey;
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
        rendered.width = Math.round(renderedWidth * dpr);
        rendered.height = Math.round(renderedHeight * dpr);
        const ctx = rendered.getContext("2d");
        if (!ctx) {
          throw new Error("filmstrip canvas unavailable");
        }
        ctx.scale(dpr, dpr);
        ctx.fillStyle = "#e4e4e7";
        ctx.fillRect(0, 0, renderedWidth, renderedHeight);
        const averageTileWidth = renderedWidth / activeRequest.tiles;
        const crop = filmstripCoverCrop({
          sourceWidth: video.videoWidth,
          sourceHeight: video.videoHeight,
          targetWidth: averageTileWidth,
          targetHeight: renderedHeight,
        });

        for (let i = 0; i < sampleTimes.length; i += 1) {
          if (isStale()) return;
          await seekVideo(video, sampleTimes[i]);
          await waitForDrawableFrame(video);
          if (isStale()) return;
          if (video.videoWidth <= 0 || video.videoHeight <= 0) {
            throw new Error("filmstrip source has no drawable video frame");
          }
          const tileLeft = Math.round((i / activeRequest.tiles) * renderedWidth);
          const tileRight = Math.round(
            ((i + 1) / activeRequest.tiles) * renderedWidth,
          );
          const tileWidth = Math.max(1, tileRight - tileLeft);
          ctx.drawImage(
            video,
            crop.sx,
            crop.sy,
            crop.sw,
            crop.sh,
            tileLeft,
            0,
            tileWidth,
            renderedHeight,
          );
        }

        if (isStale()) return;
        cacheRaster(activeRequest.cacheKey, rendered);
        if (!copyCanvas(rendered, canvas)) {
          throw new Error("filmstrip canvas unavailable");
        }
        canvas.dataset.renderedTiles = `${activeRequest.tiles}`;
        canvas.dataset.renderedRangeKey = activeRequest.cacheKey;
        delete canvas.dataset.loadingRangeKey;
      },
    )
      .catch(() => {
        if (!cancelled && !hadRenderedRaster) setFailed(true);
      })
      .finally(() => {
        if (!cancelled) window.clearTimeout(failTimer);
      });

    return () => {
      cancelled = true;
      window.clearTimeout(failTimer);
      if (latestRequestByPoolKey.get(poolKey) === activeRequest.cacheKey) {
        latestRequestByPoolKey.delete(poolKey);
      }
      if (canvas.dataset.loadingRangeKey === activeRequest.cacheKey) {
        delete canvas.dataset.loadingRangeKey;
      }
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
      className="h-full w-full rounded bg-zinc-200 object-cover [contain:paint]"
      style={{ imageRendering: "auto" }}
    />
  );
}
