import CanvasKitModule from "npm:canvaskit-wasm@0.40.0";
import type { MotionCanvas, MotionCanvasKit } from "../src/canvaskit.ts";

interface ServerImage {
  encodeToBytes(format: unknown, quality?: number): Uint8Array | null;
  delete(): void;
}

interface ServerSurface {
  getCanvas(): MotionCanvas;
  flush(): void;
  makeImageSnapshot(): ServerImage;
  delete(): void;
}

export interface ServerCanvasKit extends MotionCanvasKit {
  ImageFormat: { PNG: unknown };
  MakeSurface(width: number, height: number): ServerSurface | null;
}

type CanvasKitInitializer = () => Promise<ServerCanvasKit>;

export async function loadServerCanvasKit(): Promise<ServerCanvasKit> {
  const initialize = CanvasKitModule as unknown as CanvasKitInitializer;
  return initialize();
}
