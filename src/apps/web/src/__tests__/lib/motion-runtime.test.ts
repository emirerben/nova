import { createHash } from "node:crypto";
import { resolve } from "node:path";
import CanvasKitInit from "canvaskit-wasm";
import { drawMotionFrame } from "@nova/motion-runtime/canvaskit";
import {
  MOTION_RUNTIME_HASH,
  activeMotionInstances,
  routeTraceFrame,
  validateMotionInstances,
  type MotionPresetInstanceV1,
} from "@nova/motion-runtime";

const scene: MotionPresetInstanceV1 = {
  id: "route-trace-demo",
  preset_id: "route_trace",
  preset_version: 1,
  start_frame: 0,
  end_frame_exclusive: 60,
  palette: { primary: "#8B5CF6", accent: "#D9FF43" },
  intensity: 0.8,
};

describe("shared motion runtime", () => {
  it("uses exact integer inclusive-start/exclusive-end sampling", () => {
    expect(activeMotionInstances([scene], 0)).toHaveLength(1);
    expect(activeMotionInstances([scene], 59)).toHaveLength(1);
    expect(activeMotionInstances([scene], 60)).toHaveLength(0);
    expect(routeTraceFrame(scene, 0).progress).toBe(0);
    expect(routeTraceFrame(scene, 59).progress).toBe(1);
  });

  it("rejects arbitrary scene data and excessive active spans", () => {
    expect(
      validateMotionInstances([{ ...scene, svg: "<script>alert(1)</script>" }]),
    ).toEqual(
      expect.objectContaining({
        ok: false,
        errors: expect.arrayContaining(["motion_scenes.0.svg is not supported"]),
      }),
    );
    expect(
      validateMotionInstances([
        scene,
        {
          ...scene,
          id: "later",
          start_frame: 300,
          end_frame_exclusive: 330,
        },
      ]).ok,
    ).toBe(false);
  });

  it("pins the software CanvasKit frame used by Deno export", async () => {
    const CanvasKit = await CanvasKitInit({
      locateFile: () =>
        resolve(process.cwd(), "node_modules/canvaskit-wasm/bin/canvaskit.wasm"),
    });
    const surface = CanvasKit.MakeSurface(1080, 1920);
    expect(surface).not.toBeNull();
    try {
      drawMotionFrame(CanvasKit, surface!.getCanvas(), [scene], 30, 1080, 1920);
      surface!.flush();
      const image = surface!.makeImageSnapshot();
      try {
        const png = image.encodeToBytes(CanvasKit.ImageFormat.PNG, 100);
        expect(png).not.toBeNull();
        expect(createHash("sha256").update(png!).digest("hex")).toBe(
          "d287ae2bc0d86899b47210e73596d0e979dbf583e8559d069a841b53aa662cbc",
        );
      } finally {
        image.delete();
      }
    } finally {
      surface!.delete();
    }
    expect(MOTION_RUNTIME_HASH).toBe(
      "motion-v1:ck0.40.0:b2556106:2abfa191:route-trace-v1",
    );
  }, 30_000);
});
