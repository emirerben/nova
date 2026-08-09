"use client";

import { useEffect, useRef, useState, type RefObject } from "react";
import type { CanvasKit, Surface } from "canvaskit-wasm";
import {
  createMotionResources,
  drawMotionFrame,
  type MotionResources,
} from "@nova/motion-runtime/canvaskit";
import {
  creatorBlockAssetRefs,
  MOTION_FPS,
  MOTION_RUNTIME_HASH,
  type MotionPresetInstanceV1,
} from "@nova/motion-runtime";
import {
  isBoundedCreatorImageAsset,
  type PoolAsset,
} from "@/lib/plan-api";
import { creatorBlockPreviewFrame } from "@/lib/motion-preview";

let canvasKitPromise: Promise<CanvasKit> | null = null;
let creatorFontPromise: Promise<Uint8Array> | null = null;
const boundedAssetBytes = new Map<string, Uint8Array>();
const BOUNDED_ASSET_CACHE_BYTES = 64 * 1024 * 1024;
let boundedAssetCacheSize = 0;
const catalogSubscribers = new Set<(now: number) => void>();
let catalogRaf = 0;
let catalogLastFrame = 0;

function subscribeCatalogFrame(draw: (now: number) => void): () => void {
  catalogSubscribers.add(draw);
  const tick = (now: number) => {
    if (now - catalogLastFrame >= 1000 / 15 && document.visibilityState !== "hidden") {
      catalogLastFrame = now;
      catalogSubscribers.forEach((subscriber) => subscriber(now));
    }
    catalogRaf = window.requestAnimationFrame(tick);
  };
  if (!catalogRaf) catalogRaf = window.requestAnimationFrame(tick);
  return () => {
    catalogSubscribers.delete(draw);
    if (catalogSubscribers.size === 0 && catalogRaf) {
      window.cancelAnimationFrame(catalogRaf);
      catalogRaf = 0;
      catalogLastFrame = 0;
    }
  };
}

async function loadBoundedAsset(asset: PoolAsset, signal: AbortSignal): Promise<Uint8Array> {
  if (!isBoundedCreatorImageAsset(asset)) {
    throw new Error(`Creator Block image ${asset.id} lacks trusted bounded dimensions`);
  }
  const key = `${asset.id}:${asset.gcs_path}:${asset.width}x${asset.height}:${asset.display_url}`;
  const cached = boundedAssetBytes.get(key);
  if (cached) {
    boundedAssetBytes.delete(key);
    boundedAssetBytes.set(key, cached);
    return cached;
  }
  if (!asset.display_url) throw new Error(`Missing preview URL for ${asset.id}`);
  const response = await fetch(asset.display_url, { signal });
  if (!response.ok) throw new Error(`Could not load ${asset.id}`);
  const blob = await response.blob();
  if (blob.size <= 0 || blob.size > 25 * 1024 * 1024) {
    throw new Error(`Creator Block image ${asset.id} exceeds the preview byte limit`);
  }
  let bytes: Uint8Array;
  if (typeof createImageBitmap === "function") {
    const bitmap = await createImageBitmap(blob);
    try {
      if (Math.max(bitmap.width, bitmap.height) > 12_000) {
        throw new Error(`Creator Block image ${asset.id} exceeds the preview dimension limit`);
      }
      if (Math.max(bitmap.width, bitmap.height) > 2048) {
        const scale = 2048 / Math.max(bitmap.width, bitmap.height);
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(bitmap.width * scale));
        canvas.height = Math.max(1, Math.round(bitmap.height * scale));
        const context = canvas.getContext("2d");
        if (!context) throw new Error("Could not normalize Creator Block preview image");
        context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
        const normalized = await new Promise<Blob>((resolve, reject) =>
          canvas.toBlob(
            (value) => (value ? resolve(value) : reject(new Error("Preview image encoding failed"))),
            "image/png",
          ),
        );
        bytes = new Uint8Array(await normalized.arrayBuffer());
      } else {
        bytes = new Uint8Array(await blob.arrayBuffer());
      }
    } finally {
      bitmap.close();
    }
  } else if (Math.max(asset.width!, asset.height!) <= 2048) {
    bytes = new Uint8Array(await blob.arrayBuffer());
  } else {
    throw new Error("This browser cannot safely normalize the Creator Block preview image");
  }
  boundedAssetBytes.set(key, bytes);
  boundedAssetCacheSize += bytes.byteLength;
  while (boundedAssetCacheSize > BOUNDED_ASSET_CACHE_BYTES && boundedAssetBytes.size > 1) {
    const oldestKey = boundedAssetBytes.keys().next().value as string | undefined;
    if (!oldestKey) break;
    const oldest = boundedAssetBytes.get(oldestKey);
    boundedAssetBytes.delete(oldestKey);
    boundedAssetCacheSize -= oldest?.byteLength ?? 0;
  }
  return bytes;
}

function loadCanvasKit(): Promise<CanvasKit> {
  if (!canvasKitPromise) {
    canvasKitPromise = import("canvaskit-wasm").then(({ default: initialize }) =>
      initialize({
        locateFile: () => "/_motion/canvaskit.wasm",
      }),
    );
  }
  return canvasKitPromise;
}

function loadCreatorFont(): Promise<Uint8Array> {
  if (!creatorFontPromise) {
    creatorFontPromise = fetch("/fonts/Inter-Bold.ttf").then(async (response) => {
      if (!response.ok) throw new Error("Could not load Creator Block font");
      return new Uint8Array(await response.arrayBuffer());
    });
  }
  return creatorFontPromise;
}

export function CreatorBlockCatalogPreview({
  instance,
  assets,
  onReady,
}: {
  instance: MotionPresetInstanceV1;
  assets: PoolAsset[];
  onReady?: () => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const onReadyRef = useRef(onReady);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    onReadyRef.current = onReady;
  }, [onReady]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      ([entry]) => setVisible(entry?.isIntersecting === true),
      { rootMargin: "80px" },
    );
    observer.observe(canvas);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!visible || !canvasRef.current) return;
    let cancelled = false;
    const controller = new AbortController();
    let unsubscribeFrame: (() => void) | null = null;
    let surface: Surface | null = null;
    let resources: MotionResources | null = null;
    void loadCanvasKit().then(async (kit) => {
      const assetsById = new Map(assets.map((asset) => [asset.id, asset]));
      const imageEntries = await Promise.all(creatorBlockAssetRefs(instance).map(async (ref) => {
        const asset = assetsById.get(ref.asset_id);
        if (!asset) throw new Error(`Missing preview asset ${ref.asset_id}`);
        return [ref.asset_id, await loadBoundedAsset(asset, controller.signal)] as const;
      }));
      resources = createMotionResources(kit, {
        font: await loadCreatorFont(),
        images: Object.fromEntries(imageEntries),
      });
      if (cancelled || !canvasRef.current) {
        resources.delete();
        resources = null;
        return;
      }
      surface = kit.MakeSWCanvasSurface(canvasRef.current);
      if (!surface) throw new Error("CanvasKit catalog surface failed");
      const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
      const draw = (frame: number) => {
        if (!surface) return;
        drawMotionFrame(kit, surface.getCanvas(), [instance], frame, 240, 150, resources ?? undefined);
        surface.flush();
        onReadyRef.current?.();
      };
      if (reduced) {
        draw(creatorBlockPreviewFrame(
          instance.start_frame,
          instance.end_frame_exclusive,
          true,
        ));
      } else {
        const startedAt = performance.now();
        draw(instance.start_frame);
        unsubscribeFrame = subscribeCatalogFrame((now) => {
          draw(creatorBlockPreviewFrame(
            instance.start_frame,
            instance.end_frame_exclusive,
            false,
            now - startedAt,
          ));
        });
      }
    }).catch(() => {
      // The CSS representative still remains visible underneath this canvas.
    });
    return () => {
      cancelled = true;
      controller.abort();
      unsubscribeFrame?.();
      surface?.delete();
      resources?.delete();
    };
  }, [assets, instance, visible]);

  return (
    <canvas
      ref={canvasRef}
      width={240}
      height={150}
      aria-hidden
      className="absolute inset-0 h-full w-full"
      data-creator-block-preview
    />
  );
}

export default function MotionCanvasLayer({
  instances,
  currentTime,
  playing,
  width,
  height,
  runtimeHash,
  videoRef,
  assets = [],
}: {
  instances: MotionPresetInstanceV1[];
  currentTime: number;
  playing: boolean;
  width: number;
  height: number;
  runtimeHash?: string | null;
  videoRef: RefObject<HTMLVideoElement>;
  assets?: PoolAsset[];
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const surfaceRef = useRef<Surface | null>(null);
  const kitRef = useRef<CanvasKit | null>(null);
  const resourcesRef = useRef<MotionResources | null>(null);
  const instancesRef = useRef(instances);
  const installedAssetKeyRef = useRef<string | null>(null);
  const requiredAssetKeyRef = useRef("");
  const drawAtRef = useRef<(seconds: number) => void>(() => undefined);
  const [failed, setFailed] = useState(false);
  const [ready, setReady] = useState(false);
  const [resourcesReady, setResourcesReady] = useState(false);
  const [surfaceEpoch, setSurfaceEpoch] = useState(0);
  const compatible = !runtimeHash || runtimeHash === MOTION_RUNTIME_HASH;
  const previewScale = Math.min(1, 640 / Math.max(width, height));
  const previewWidth = Math.max(1, Math.round(width * previewScale));
  const previewHeight = Math.max(1, Math.round(height * previewScale));
  const requiredAssetIds = Array.from(
    new Set(instances.flatMap(creatorBlockAssetRefs).map((asset) => asset.asset_id)),
  ).sort();
  const assetKey = requiredAssetIds
    .map((id) => {
      const asset = assets.find((candidate) => candidate.id === id);
      return `${id}:${asset?.gcs_path ?? "missing"}:${asset?.width ?? "?"}x${asset?.height ?? "?"}:${asset?.display_url ?? "missing"}`;
    })
    .join("|");
  requiredAssetKeyRef.current = assetKey;

  useEffect(() => {
    instancesRef.current = instances;
  }, [instances]);

  useEffect(() => {
    if (!compatible || instances.length === 0 || !canvasRef.current) return;
    let cancelled = false;
    setReady(false);
    setResourcesReady(false);
    void loadCanvasKit()
      .then((kit) => {
        if (cancelled || !canvasRef.current) return;
        surfaceRef.current?.delete();
        const surface = kit.MakeSWCanvasSurface(canvasRef.current);
        if (!surface) throw new Error("CanvasKit software surface failed");
        kitRef.current = kit;
        surfaceRef.current = surface;
        setFailed(false);
        setReady(true);
        setSurfaceEpoch((value) => value + 1);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      surfaceRef.current?.delete();
      resourcesRef.current?.delete();
      resourcesRef.current = null;
      surfaceRef.current = null;
      kitRef.current = null;
    };
  }, [compatible, instances.length, previewHeight, previewWidth]);

  useEffect(() => {
    if (!compatible || instances.length === 0) return;
    let cancelled = false;
    setResourcesReady(false);
    const controller = new AbortController();
    const kit = kitRef.current;
    if (!kit) return;
    const assetsById = new Map(assets.map((asset) => [asset.id, asset]));
    void Promise.all(
      requiredAssetIds.map(async (assetId) => {
        const asset = assetsById.get(assetId);
        if (!asset) throw new Error(`Missing preview asset ${assetId}`);
        return [assetId, await loadBoundedAsset(asset, controller.signal)] as const;
      }),
    )
      .then(async (imageEntries) => {
        const nextResources = createMotionResources(kit, {
          font: await loadCreatorFont(),
          images: Object.fromEntries(imageEntries),
        });
        if (cancelled) {
          nextResources.delete();
          return;
        }
        resourcesRef.current?.delete();
        resourcesRef.current = nextResources;
        installedAssetKeyRef.current = assetKey;
        setFailed(false);
        setResourcesReady(true);
        setReady((value) => !value);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
    // assetKey is the stable identity; signed display URLs intentionally do not rebuild resources.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assetKey, compatible, instances.length, surfaceEpoch]);

  useEffect(() => {
    drawAtRef.current = (seconds: number) => {
      const kit = kitRef.current;
      const surface = surfaceRef.current;
      if (!kit || !surface || !compatible) return;
      if (
        instancesRef.current.some((instance) => instance.preset_id !== "route_trace") &&
        (!resourcesReady || installedAssetKeyRef.current !== requiredAssetKeyRef.current)
      ) {
        return;
      }
      const frame = Math.max(0, Math.floor(seconds * MOTION_FPS + 1e-6));
      try {
        drawMotionFrame(
          kit,
          surface.getCanvas(),
          instancesRef.current,
          frame,
          previewWidth,
          previewHeight,
          resourcesRef.current ?? undefined,
        );
        surface.flush();
      } catch {
        setFailed(true);
      }
    };
  }, [compatible, previewHeight, previewWidth, ready, resourcesReady]);

  useEffect(() => {
    if (!playing) drawAtRef.current(currentTime);
  }, [currentTime, playing, ready]);

  useEffect(() => {
    if (!playing || !compatible) return;

    const video = videoRef.current;
    if (!video) return;

    if (typeof video.requestVideoFrameCallback === "function") {
      let callbackId = 0;
      const drawDecodedFrame: VideoFrameRequestCallback = (_now, metadata) => {
        drawAtRef.current(metadata.mediaTime);
        callbackId = video.requestVideoFrameCallback(drawDecodedFrame);
      };
      callbackId = video.requestVideoFrameCallback(drawDecodedFrame);
      return () => video.cancelVideoFrameCallback(callbackId);
    }

    let raf = 0;
    const drawMediaTime = () => {
      drawAtRef.current(video.currentTime);
      raf = window.requestAnimationFrame(drawMediaTime);
    };
    raf = window.requestAnimationFrame(drawMediaTime);
    return () => window.cancelAnimationFrame(raf);
  }, [compatible, playing, ready, videoRef]);

  if (instances.length === 0) return null;
  return (
    <>
      <canvas
        ref={canvasRef}
        data-motion-preview
        width={previewWidth}
        height={previewHeight}
        aria-hidden
        className="pointer-events-none absolute inset-0 h-full w-full"
        style={{ zIndex: 15 }}
      />
      {(!compatible || failed) && (
      <div
        data-motion-preview-error
        className="pointer-events-none absolute left-3 top-3 rounded-full bg-amber-950/85 px-2.5 py-1 text-[10px] font-semibold text-amber-100"
        style={{ zIndex: 15 }}
      >
        Refresh for accurate motion preview
      </div>
      )}
    </>
  );
}
