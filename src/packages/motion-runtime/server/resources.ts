import type { MotionPresetInstance } from "../src/contract.ts";
import { createMotionResources, type MotionResources } from "../src/canvaskit.ts";
import type { ServerCanvasKit } from "./canvaskit-init.ts";

export interface ResourceRequest {
  font_path?: string;
  asset_paths?: Record<string, string>;
}

export async function loadMotionResources(
  CanvasKit: ServerCanvasKit,
  request: ResourceRequest,
  instances: readonly MotionPresetInstance[],
): Promise<MotionResources | undefined> {
  if (instances.every((item) => item.preset_id === "route_trace")) return undefined;
  if (!request.font_path) throw new Error("creator_block_font_missing");
  const images: Record<string, Uint8Array> = {};
  for (const [assetId, path] of Object.entries(request.asset_paths ?? {})) {
    images[assetId] = await Deno.readFile(path);
  }
  return createMotionResources(CanvasKit, {
    font: await Deno.readFile(request.font_path),
    images,
  });
}
