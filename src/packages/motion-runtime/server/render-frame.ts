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
  frame: number;
  runtime_hash: string;
  instances: MotionPresetInstanceV1[];
  output_path: string;
}

function fail(message: string): never {
  console.error(message);
  Deno.exit(2);
}

const requestPath = Deno.args[0];
if (!requestPath) fail("Usage: render-frame.ts REQUEST.json");

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
if (!Number.isInteger(request.frame) || request.frame < 0) fail("invalid_frame");
const validation = validateMotionInstances(request.instances);
if (!validation.ok) fail(validation.errors.join("; "));

const CanvasKit = await loadServerCanvasKit();
const surface = CanvasKit.MakeSurface(request.width, request.height);
if (!surface) fail("canvaskit_surface_failed");
try {
  drawMotionFrame(
    CanvasKit,
    surface.getCanvas(),
    request.instances,
    request.frame,
    request.width,
    request.height,
  );
  surface.flush();
  const image = surface.makeImageSnapshot();
  try {
    const png = image.encodeToBytes(CanvasKit.ImageFormat.PNG, 100);
    if (!png) fail("png_encode_failed");
    await Deno.writeFile(request.output_path, png);
  } finally {
    image.delete();
  }
} finally {
  surface.delete();
}
