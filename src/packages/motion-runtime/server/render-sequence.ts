import {
  MOTION_RUNTIME_HASH,
  type MotionPresetInstanceV1,
  validateMotionInstances,
} from "../src/contract.ts";
import { drawMotionFrame } from "../src/canvaskit.ts";
import { loadServerCanvasKit } from "./canvaskit-init.ts";
import { loadMotionResources, type ResourceRequest } from "./resources.ts";

interface Request extends ResourceRequest {
  width: number;
  height: number;
  runtime_hash: string;
  instances: MotionPresetInstanceV1[];
  output_dir: string;
}

function fail(message: string): never {
  console.error(message);
  Deno.exit(2);
}

const requestPath = Deno.args[0];
if (!requestPath) fail("Usage: render-sequence.ts REQUEST.json");
const request = JSON.parse(await Deno.readTextFile(requestPath)) as Request;
if (request.runtime_hash !== MOTION_RUNTIME_HASH) fail("motion_runtime_mismatch");
if (
  !Number.isInteger(request.width) ||
  !Number.isInteger(request.height) ||
  request.width <= 0 ||
  request.height <= 0 ||
  request.width * request.height > 2_073_600
) {
  fail("invalid_output_dimensions");
}
const validation = validateMotionInstances(request.instances);
if (!validation.ok || request.instances.length === 0) {
  fail(validation.ok ? "empty_motion_scene" : validation.errors.join("; "));
}

const intervals = request.instances
  .map((item) => [item.start_frame, item.end_frame_exclusive] as [number, number])
  .sort((a, b) => a[0] - b[0]);
const segments: Array<{ start_frame: number; end_frame_exclusive: number }> = [];
for (const [start, end] of intervals) {
  const previous = segments.at(-1);
  if (previous && start <= previous.end_frame_exclusive) previous.end_frame_exclusive = Math.max(previous.end_frame_exclusive, end);
  else segments.push({ start_frame: start, end_frame_exclusive: end });
}
await Deno.mkdir(request.output_dir, { recursive: true });

const CanvasKit = await loadServerCanvasKit();
const surface = CanvasKit.MakeSurface(request.width, request.height);
if (!surface) fail("canvaskit_surface_failed");
const resources = await loadMotionResources(CanvasKit, request, request.instances).catch((error) => fail(String(error)));
try {
  for (const [segmentIndex, segment] of segments.entries()) {
    const segmentDir = `${request.output_dir}/segment_${String(segmentIndex).padStart(3, "0")}`;
    await Deno.mkdir(segmentDir, { recursive: true });
    for (let frame = segment.start_frame; frame < segment.end_frame_exclusive; frame += 1) {
      drawMotionFrame(CanvasKit, surface.getCanvas(), request.instances, frame, request.width, request.height, resources);
      surface.flush();
      const image = surface.makeImageSnapshot();
      try {
        const png = image.encodeToBytes(CanvasKit.ImageFormat.PNG, 100);
        if (!png) fail(`png_encode_failed:${frame}`);
        const localIndex = String(frame - segment.start_frame).padStart(6, "0");
        await Deno.writeFile(`${segmentDir}/frame_${localIndex}.png`, png);
      } finally {
        image.delete();
      }
    }
  }
} finally {
  resources?.delete();
  surface.delete();
}

console.log(
  JSON.stringify({
    segments,
    frame_count: segments.reduce((total, segment) => total + segment.end_frame_exclusive - segment.start_frame, 0),
    runtime_hash: MOTION_RUNTIME_HASH,
  }),
);
