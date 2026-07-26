import {
  MOTION_RUNTIME_HASH,
  type MotionPresetInstanceV1,
  validateMotionInstances,
} from "../src/contract.ts";
import { drawMotionFrame } from "../src/canvaskit.ts";
import { loadServerCanvasKit } from "./canvaskit-init.ts";

interface Request {
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

const firstFrame = Math.min(...request.instances.map((item) => item.start_frame));
const lastFrameExclusive = Math.max(
  ...request.instances.map((item) => item.end_frame_exclusive),
);
await Deno.mkdir(request.output_dir, { recursive: true });

const CanvasKit = await loadServerCanvasKit();
const surface = CanvasKit.MakeSurface(request.width, request.height);
if (!surface) fail("canvaskit_surface_failed");
try {
  for (let frame = firstFrame; frame < lastFrameExclusive; frame += 1) {
    drawMotionFrame(
      CanvasKit,
      surface.getCanvas(),
      request.instances,
      frame,
      request.width,
      request.height,
    );
    surface.flush();
    const image = surface.makeImageSnapshot();
    try {
      const png = image.encodeToBytes(CanvasKit.ImageFormat.PNG, 100);
      if (!png) fail(`png_encode_failed:${frame}`);
      const localIndex = String(frame - firstFrame).padStart(6, "0");
      await Deno.writeFile(`${request.output_dir}/frame_${localIndex}.png`, png);
    } finally {
      image.delete();
    }
  }
} finally {
  surface.delete();
}

console.log(
  JSON.stringify({
    first_frame: firstFrame,
    last_frame_exclusive: lastFrameExclusive,
    frame_count: lastFrameExclusive - firstFrame,
    runtime_hash: MOTION_RUNTIME_HASH,
  }),
);
