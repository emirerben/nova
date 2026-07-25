"use client";

import { useEffect, useRef, useState, type RefObject } from "react";
import type { CanvasKit, Surface } from "canvaskit-wasm";
import { drawMotionFrame } from "@nova/motion-runtime/canvaskit";
import {
  MOTION_FPS,
  MOTION_RUNTIME_HASH,
  type MotionPresetInstanceV1,
} from "@nova/motion-runtime";

let canvasKitPromise: Promise<CanvasKit> | null = null;

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

export default function MotionCanvasLayer({
  instances,
  currentTime,
  playing,
  width,
  height,
  runtimeHash,
  videoRef,
}: {
  instances: MotionPresetInstanceV1[];
  currentTime: number;
  playing: boolean;
  width: number;
  height: number;
  runtimeHash?: string | null;
  videoRef: RefObject<HTMLVideoElement>;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const surfaceRef = useRef<Surface | null>(null);
  const kitRef = useRef<CanvasKit | null>(null);
  const [failed, setFailed] = useState(false);
  const [ready, setReady] = useState(false);
  const compatible = !runtimeHash || runtimeHash === MOTION_RUNTIME_HASH;

  useEffect(() => {
    if (!compatible || instances.length === 0 || !canvasRef.current) return;
    let cancelled = false;
    setReady(false);
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
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      surfaceRef.current?.delete();
      surfaceRef.current = null;
      kitRef.current = null;
    };
  }, [compatible, height, instances.length, width]);

  useEffect(() => {
    const kit = kitRef.current;
    const surface = surfaceRef.current;
    if (!kit || !surface || !compatible) return;
    const drawAt = (seconds: number) => {
      const frame = Math.max(0, Math.floor(seconds * MOTION_FPS + 1e-6));
      drawMotionFrame(kit, surface.getCanvas(), instances, frame, width, height);
      surface.flush();
    };
    drawAt(currentTime);
    if (!playing) return;

    const video = videoRef.current;
    if (!video) return;

    if (typeof video.requestVideoFrameCallback === "function") {
      let callbackId = 0;
      const drawDecodedFrame: VideoFrameRequestCallback = (_now, metadata) => {
        drawAt(metadata.mediaTime);
        callbackId = video.requestVideoFrameCallback(drawDecodedFrame);
      };
      callbackId = video.requestVideoFrameCallback(drawDecodedFrame);
      return () => video.cancelVideoFrameCallback(callbackId);
    }

    let raf = 0;
    const drawMediaTime = () => {
      drawAt(video.currentTime);
      raf = window.requestAnimationFrame(drawMediaTime);
    };
    raf = window.requestAnimationFrame(drawMediaTime);
    return () => window.cancelAnimationFrame(raf);
  }, [compatible, currentTime, height, instances, playing, ready, videoRef, width]);

  if (instances.length === 0) return null;
  if (!compatible || failed) {
    return (
      <div
        data-motion-preview-error
        className="pointer-events-none absolute left-3 top-3 rounded-full bg-amber-950/85 px-2.5 py-1 text-[10px] font-semibold text-amber-100"
        style={{ zIndex: 15 }}
      >
        Refresh for accurate motion preview
      </div>
    );
  }
  return (
    <canvas
      ref={canvasRef}
      data-motion-preview
      width={width}
      height={height}
      aria-hidden
      className="pointer-events-none absolute inset-0 h-full w-full"
      style={{ zIndex: 15 }}
    />
  );
}
