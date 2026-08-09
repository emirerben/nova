import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import CanvasKitInit from "canvaskit-wasm";
import catalogJson from "../../../../../packages/motion-runtime/creator-blocks.catalog.json";
import schemaJson from "../../../../../packages/motion-runtime/motion-scene.schema.json";
import { createMotionResources, drawMotionFrame } from "@nova/motion-runtime/canvaskit";
import {
  CREATOR_BLOCK_CATALOG,
  MOTION_RUNTIME_HASH,
  activeMotionFrameCount,
  activeMotionInstances,
  createCreatorBlockInstance,
  routeTraceFrame,
  creatorBlockFrame,
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

  it("rejects arbitrary scene data and budgets active union rather than empty gaps", () => {
    expect(
      validateMotionInstances([{ ...scene, svg: "<script>alert(1)</script>" }]),
    ).toEqual(
      expect.objectContaining({
        ok: false,
        errors: expect.arrayContaining(["motion_scenes.0.svg is not supported"]),
      }),
    );
    const separated = [
      scene,
      { ...scene, id: "later", start_frame: 300, end_frame_exclusive: 330 },
    ];
    expect(validateMotionInstances(separated).ok).toBe(true);
    expect(activeMotionFrameCount(separated)).toBe(90);
    expect(
      validateMotionInstances([
        { ...scene, id: "long", end_frame_exclusive: 240 },
        { ...scene, id: "over-budget", start_frame: 300, end_frame_exclusive: 301 },
      ]).ok,
    ).toBe(false);
  });

  it("publishes exactly eight strict Creator Blocks", () => {
    expect(CREATOR_BLOCK_CATALOG).toHaveLength(8);
    expect(new Set(CREATOR_BLOCK_CATALOG.map((entry) => entry.preset_id)).size).toBe(8);
    expect(
      validateMotionInstances([
        {
          ...scene,
          preset_id: "kinetic_word",
          params: { text: "HELLO" },
        },
      ]).ok,
    ).toBe(true);
    expect(
      validateMotionInstances([
        {
          ...scene,
          preset_id: "kinetic_word",
          params: { text: "HELLO", svg: "<script />" },
        },
      ]).ok,
    ).toBe(false);
  });

  it("keeps the shared catalog, schema vocabulary, and TypeScript union in lockstep", () => {
    expect(CREATOR_BLOCK_CATALOG).toEqual(catalogJson.presets);
    const schemaText = JSON.stringify(schemaJson);
    for (const entry of CREATOR_BLOCK_CATALOG) {
      expect(schemaText).toContain(`\"const\":\"${entry.preset_id}\"`);
      expect(validateMotionInstances([createCreatorBlockInstance({
        id: `contract-${entry.preset_id}`,
        presetId: entry.preset_id,
        startFrame: 0,
        endFrameExclusive: entry.default_duration_frames,
        assets: ["a", "b", "c"].map((asset_id) => ({
          asset_id,
          gcs_path: `users/u/plan/i/pool/${asset_id}.png`,
        })),
      })]).ok).toBe(true);
      expect(entry.ai_exposed).toBe(true);
      const definition = (schemaJson.$defs as Record<string, any>)[entry.preset_id];
      const paramsSchema = definition.properties.params;
      expect(new Set(entry.parameters.map((parameter) => parameter.key))).toEqual(
        new Set(Object.keys(paramsSchema.properties)),
      );
      for (const parameter of entry.parameters) {
        const property = paramsSchema.properties[parameter.key];
        expect(paramsSchema.required.includes(parameter.key)).toBe(parameter.required);
        expect(property.maxLength ?? property.items?.maxLength).toBe(parameter.max_length);
        expect(property.minItems).toBe(parameter.min_items);
        expect(property.maxItems).toBe(parameter.max_items);
      }
    }
  });

  it("accepts Unicode-safe Creator Block copy", () => {
    const unicodeScenes = [
      { preset_id: "kinetic_word", params: { text: "İYİ 🧿 東京" } },
      { preset_id: "tag_stack", params: { labels: ["Merhaba", "こんにちは", "✨"] } },
      { preset_id: "flow_field", params: { headline: "AKIŞ 東京", kicker: "hareket 🧿" } },
    ].map((value, index) => ({ ...scene, id: `unicode-${index}`, ...value }));
    expect(validateMotionInstances(unicodeScenes).ok).toBe(true);
  });

  it("renders only sparse contiguous intervals in the Deno sequence entrypoint", () => {
    const parityDir = mkdtempSync(resolve(tmpdir(), "nova-motion-sequence-"));
    const outputDir = resolve(parityDir, "frames");
    const requestPath = resolve(parityDir, "request.json");
    const runtimeRoot = resolve(process.cwd(), "../../packages/motion-runtime");
    const denoInfo = spawnSync("deno", ["info", "--json"], { encoding: "utf8" });
    expect(denoInfo.status).toBe(0);
    const denoDir = String(JSON.parse(denoInfo.stdout).denoDir);
    writeFileSync(requestPath, JSON.stringify({
      width: 160,
      height: 284,
      runtime_hash: MOTION_RUNTIME_HASH,
      instances: [
        { ...scene, id: "first", start_frame: 2, end_frame_exclusive: 5 },
        { ...scene, id: "overlap", start_frame: 4, end_frame_exclusive: 7 },
        { ...scene, id: "later", start_frame: 12, end_frame_exclusive: 14 },
      ],
      output_dir: outputDir,
    }));
    try {
      const deno = spawnSync("deno", [
        "run",
        "--cached-only",
        "--no-config",
        "--node-modules-dir=none",
        `--allow-read=${runtimeRoot},${parityDir},${denoDir}`,
        `--allow-write=${parityDir}`,
        resolve(runtimeRoot, "server/render-sequence.ts"),
        requestPath,
      ], { encoding: "utf8" });
      expect(deno.status).toBe(0);
      expect(JSON.parse(deno.stdout)).toEqual(expect.objectContaining({
        segments: [
          { start_frame: 2, end_frame_exclusive: 7 },
          { start_frame: 12, end_frame_exclusive: 14 },
        ],
        frame_count: 7,
      }));
      expect(readFileSync(resolve(outputDir, "segment_000/frame_000004.png")).length).toBeGreaterThan(0);
      expect(readFileSync(resolve(outputDir, "segment_001/frame_000001.png")).length).toBeGreaterThan(0);
    } finally {
      rmSync(parityDir, { recursive: true, force: true });
    }
  }, 30_000);

  it.each(CREATOR_BLOCK_CATALOG)(
    "$label evaluator remains finite at entrance, hold, loop, exit, minimum, and maximum duration",
    (entry) => {
      for (const duration of [1, entry.default_duration_frames, 240]) {
        const instance = createCreatorBlockInstance({
          id: `bounds-${entry.preset_id}-${duration}`,
          presetId: entry.preset_id,
          startFrame: 12,
          endFrameExclusive: 12 + duration,
          assets: ["a", "b", "c"].map((asset_id) => ({
            asset_id,
            gcs_path: `users/u/plan/i/pool/${asset_id}.png`,
          })),
        });
        expect(validateMotionInstances([instance]).ok).toBe(true);
        for (const frame of [12, 12 + Math.floor(duration * 0.25), 12 + Math.floor(duration * 0.7), 11 + duration]) {
          expect(Object.values(creatorBlockFrame(instance, frame)).every(Number.isFinite)).toBe(true);
        }
      }
    },
  );

  it("rejects every Creator Block parameter boundary fail-closed", () => {
    const invalidParams: Array<[string, Record<string, unknown>]> = [
      ["kinetic_word", { text: "x".repeat(33) }],
      ["tag_stack", { labels: ["only one"] }],
      ["flow_field", { headline: "x".repeat(41), kicker: "ok" }],
      ["cloud_break", { lines: ["one"] }],
      ["offer_swap", { primary_text: "x".repeat(25), alternate_text: "ok" }],
      ["card_stack", { assets: [{ asset_id: "a", gcs_path: "users/u/a.png" }] }],
      ["film_strip", { assets: [
        { asset_id: "a", gcs_path: "users/u/a.png" },
        { asset_id: "b", gcs_path: "users/u/b.png" },
      ] }],
      ["donut_text", { left_text: "x".repeat(21), right_text: "ok" }],
    ];
    for (const [preset_id, params] of invalidParams) {
      expect(validateMotionInstances([{ ...scene, preset_id, params }]).ok).toBe(false);
    }
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
      "motion-v3:ck0.40.0:b2556106:2abfa191:creator-blocks-v2",
    );
  }, 30_000);

  it("keeps settled Creator Blocks inside an aspect-relative safe frame at maximum content", async () => {
    const CanvasKit = await CanvasKitInit({
      locateFile: () => resolve(process.cwd(), "node_modules/canvaskit-wasm/bin/canvaskit.wasm"),
    });
    const font = new Uint8Array(readFileSync(resolve(process.cwd(), "public/fonts/Inter-Bold.ttf")));
    const fixtureSurface = CanvasKit.MakeSurface(96, 96)!;
    const fixturePaint = new CanvasKit.Paint();
    fixturePaint.setColor(CanvasKit.Color(39, 56, 47, 1));
    fixtureSurface.getCanvas().drawRect(CanvasKit.XYWHRect(0, 0, 96, 96), fixturePaint);
    fixtureSurface.flush();
    const fixtureImage = fixtureSurface.makeImageSnapshot();
    const fixtureBytes = fixtureImage.encodeToBytes(CanvasKit.ImageFormat.PNG, 100)!;
    fixtureImage.delete();
    fixturePaint.delete();
    fixtureSurface.delete();
    const resources = createMotionResources(CanvasKit, {
      font,
      images: Object.fromEntries(
        Array.from({ length: 8 }, (_, index) => [`image-${index}`, fixtureBytes]),
      ),
    });
    const maxParams: Record<string, Record<string, unknown>> = {
      kinetic_word: { text: "W".repeat(32) },
      tag_stack: { labels: Array.from({ length: 6 }, (_, i) => `${i}${"W".repeat(23)}`) },
      flow_field: { headline: "W".repeat(40), kicker: "W".repeat(24) },
      cloud_break: { lines: Array.from({ length: 5 }, (_, i) => `${i}${"W".repeat(27)}`) },
      offer_swap: { primary_text: "W".repeat(24), alternate_text: "M".repeat(24) },
      card_stack: { assets: Array.from({ length: 6 }, (_, i) => ({ asset_id: `image-${i}`, gcs_path: `users/u/p/i/pool/${i}.png` })) },
      film_strip: { assets: Array.from({ length: 8 }, (_, i) => ({ asset_id: `image-${i}`, gcs_path: `users/u/p/i/pool/${i}.png` })) },
      donut_text: { left_text: "W".repeat(20), right_text: "M".repeat(20) },
    };
    const pixelInfo = (width: number, height: number) => ({
      width,
      height,
      colorType: CanvasKit.ColorType.RGBA_8888,
      alphaType: CanvasKit.AlphaType.Unpremul,
      colorSpace: CanvasKit.ColorSpace.SRGB,
    });
    try {
      for (const entry of CREATOR_BLOCK_CATALOG) {
        const instance = {
          ...createCreatorBlockInstance({
            id: `quality-${entry.preset_id}`,
            presetId: entry.preset_id,
            startFrame: 0,
            endFrameExclusive: entry.default_duration_frames,
            assets: Array.from({ length: 8 }, (_, i) => ({ asset_id: `image-${i}`, gcs_path: `users/u/p/i/pool/${i}.png` })),
          }),
          params: maxParams[entry.preset_id],
        } as unknown as MotionPresetInstanceV1;
        for (const [width, height] of [[540, 960], [960, 540]] as const) {
          const surface = CanvasKit.MakeSurface(width, height)!;
          const frame = Math.floor(entry.default_duration_frames * 0.42);
          drawMotionFrame(CanvasKit, surface.getCanvas(), [instance], frame, width, height, resources);
          surface.flush();
          const pixels = surface.getCanvas().readPixels(0, 0, pixelInfo(width, height)) as Uint8Array;
          let ink = 0;
          let minX: number = width;
          let maxX: number = -1;
          let minY: number = height;
          let maxY: number = -1;
          for (let y = 0; y < height; y += 1) {
            for (let x = 0; x < width; x += 1) {
              if (pixels[(y * width + x) * 4 + 3] > 12) {
                ink += 1;
                minX = Math.min(minX, x);
                maxX = Math.max(maxX, x);
                minY = Math.min(minY, y);
                maxY = Math.max(maxY, y);
              }
            }
          }
          const safeMargin = Math.floor(Math.min(width, height) * 0.025);
          expect({ preset: entry.preset_id, width, height, ink }).toEqual(
            expect.objectContaining({ ink: expect.any(Number) }),
          );
          expect(ink).toBeGreaterThan(40);
          if (
            minX < safeMargin || maxX >= width - safeMargin ||
            minY < safeMargin || maxY >= height - safeMargin
          ) {
            throw new Error(
              `${entry.preset_id} ${width}x${height} escaped safe frame: ` +
              `${minX},${minY}..${maxX},${maxY}; margin=${safeMargin}`,
            );
          }
          expect(minX).toBeGreaterThanOrEqual(safeMargin);
          expect(maxX).toBeLessThan(width - safeMargin);
          expect(minY).toBeGreaterThanOrEqual(safeMargin);
          expect(maxY).toBeLessThan(height - safeMargin);
          surface.delete();
        }
      }
    } finally {
      resources.delete();
    }
  }, 30_000);

  it("keeps Signal Stack rows separated and Flow Field's full headline visible", async () => {
    const CanvasKit = await CanvasKitInit({
      locateFile: () => resolve(process.cwd(), "node_modules/canvaskit-wasm/bin/canvaskit.wasm"),
    });
    const resources = createMotionResources(CanvasKit, {
      font: new Uint8Array(readFileSync(resolve(process.cwd(), "public/fonts/Inter-Bold.ttf"))),
    });
    const width = 540;
    const height = 960;
    const renderAlphaRows = (instance: MotionPresetInstanceV1, frame: number) => {
      const surface = CanvasKit.MakeSurface(width, height)!;
      drawMotionFrame(CanvasKit, surface.getCanvas(), [instance], frame, width, height, resources);
      surface.flush();
      const pixels = surface.getCanvas().readPixels(0, 0, {
        width,
        height,
        colorType: CanvasKit.ColorType.RGBA_8888,
        alphaType: CanvasKit.AlphaType.Unpremul,
        colorSpace: CanvasKit.ColorSpace.SRGB,
      }) as Uint8Array;
      const rows = Array.from({ length: height }, (_, y) => {
        for (let x = 0; x < width; x += 1) {
          if (pixels[(y * width + x) * 4 + 3] > 12) return true;
        }
        return false;
      });
      surface.delete();
      return rows;
    };
    try {
      const signal = createCreatorBlockInstance({
        id: "signal-quality",
        presetId: "tag_stack",
        startFrame: 0,
        endFrameExclusive: 120,
      }) as Extract<MotionPresetInstanceV1, { preset_id: "tag_stack" }>;
      signal.params.labels = ["ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX"];
      const signalRows = renderAlphaRows(signal, 50);
      const runs: Array<[number, number]> = [];
      signalRows.forEach((hasInk, row) => {
        if (hasInk && !signalRows[row - 1]) runs.push([row, row]);
        else if (hasInk) runs[runs.length - 1][1] = row;
      });
      expect(runs).toHaveLength(6);
      const gaps = runs.slice(1).map((run, index) => run[0] - runs[index][1] - 1);
      expect(Math.min(...gaps)).toBeGreaterThanOrEqual(8);

      const flow = createCreatorBlockInstance({
        id: "flow-quality",
        presetId: "flow_field",
        startFrame: 0,
        endFrameExclusive: 90,
      });
      const flowRows = renderAlphaRows(flow, 40);
      const lowerHeadlineInk = flowRows
        .slice(Math.floor(height / 2) - 6, Math.floor(height / 2) + 42)
        .filter(Boolean).length;
      expect(lowerHeadlineInk).toBeGreaterThanOrEqual(18);
    } finally {
      resources.delete();
    }
  }, 30_000);

  it("pins all eight Creator Blocks across portrait and landscape frames with trusted resources", async () => {
    const CanvasKit = await CanvasKitInit({
      locateFile: () => resolve(process.cwd(), "node_modules/canvaskit-wasm/bin/canvaskit.wasm"),
    });
    const font = new Uint8Array(readFileSync(resolve(process.cwd(), "public/fonts/Inter-Bold.ttf")));
    const fontHash = createHash("sha256").update(font).digest("hex");
    expect(fontHash).toBe(
      "b37284b5701b6b168dfc770aa1a4ac492106422fd3ba76bc7641e37434e8019c",
    );
    expect(
      createHash("sha256")
        .update(readFileSync(resolve(process.cwd(), "../api/assets/fonts/Inter-Bold.ttf")))
        .digest("hex"),
    ).toBe(fontHash);

    const fixtureSurface = CanvasKit.MakeSurface(96, 96)!;
    const fixturePaint = new CanvasKit.Paint();
    fixturePaint.setColor(CanvasKit.Color(39, 56, 47, 1));
    fixtureSurface.getCanvas().drawRect(CanvasKit.XYWHRect(0, 0, 96, 96), fixturePaint);
    fixturePaint.setColor(CanvasKit.Color(199, 255, 61, 1));
    fixtureSurface.getCanvas().drawCircle(48, 48, 28, fixturePaint);
    fixtureSurface.flush();
    const fixtureImage = fixtureSurface.makeImageSnapshot();
    const fixtureBytes = fixtureImage.encodeToBytes(CanvasKit.ImageFormat.PNG, 100)!;
    fixtureImage.delete();
    fixturePaint.delete();
    fixtureSurface.delete();

    const resources = createMotionResources(CanvasKit, {
      font,
      images: { "image-1": fixtureBytes, "image-2": fixtureBytes, "image-3": fixtureBytes },
    });
    const parityDir = mkdtempSync(resolve(tmpdir(), "nova-motion-parity-"));
    const fixturePath = resolve(parityDir, "fixture.png");
    const requestPath = resolve(parityDir, "request.json");
    const denoOutputPath = resolve(parityDir, "deno.png");
    writeFileSync(fixturePath, fixtureBytes);
    const denoInfo = spawnSync("deno", ["info", "--json"], { encoding: "utf8" });
    expect(denoInfo.status).toBe(0);
    const denoDir = String(JSON.parse(denoInfo.stdout).denoDir);
    const runtimeRoot = resolve(process.cwd(), "../../packages/motion-runtime");
    const fontPath = resolve(process.cwd(), "public/fonts/Inter-Bold.ttf");
    const hashes: string[] = [];
    try {
      for (const entry of CREATOR_BLOCK_CATALOG) {
        const instance = createCreatorBlockInstance({
          id: `golden-${entry.preset_id}`,
          presetId: entry.preset_id,
          startFrame: 0,
          endFrameExclusive: entry.default_duration_frames,
          assets: ["image-1", "image-2", "image-3"].map((asset_id) => ({
            asset_id,
            gcs_path: `users/user/plan/item/pool/${asset_id}.png`,
          })),
        });
        for (const [width, height] of [[540, 960], [960, 540]] as const) {
          const sampledFrames = [
            Math.max(1, Math.floor(entry.default_duration_frames * 0.08)),
            Math.floor(entry.default_duration_frames / 2),
            entry.default_duration_frames - 2,
          ];
          for (const frame of sampledFrames) {
            const surface = CanvasKit.MakeSurface(width, height)!;
            drawMotionFrame(CanvasKit, surface.getCanvas(), [instance], frame, width, height, resources);
            surface.flush();
            const image = surface.makeImageSnapshot();
            const png = image.encodeToBytes(CanvasKit.ImageFormat.PNG, 100)!;
            const nodeHash = createHash("sha256").update(png).digest("hex");
            hashes.push(nodeHash);
            if (process.env.CREATOR_BLOCK_AUDIT_DIR) {
              mkdirSync(process.env.CREATOR_BLOCK_AUDIT_DIR, { recursive: true });
              writeFileSync(
                resolve(
                  process.env.CREATOR_BLOCK_AUDIT_DIR,
                  `${entry.preset_id}-${width}x${height}-f${frame}.png`,
                ),
                png,
              );
            }
            writeFileSync(requestPath, JSON.stringify({
              width,
              height,
              frame,
              runtime_hash: MOTION_RUNTIME_HASH,
              instances: [instance],
              output_path: denoOutputPath,
              font_path: fontPath,
              asset_paths: {
                "image-1": fixturePath,
                "image-2": fixturePath,
                "image-3": fixturePath,
              },
            }));
            const deno = spawnSync("deno", [
              "run",
              "--cached-only",
              "--no-config",
              "--node-modules-dir=none",
              `--allow-read=${runtimeRoot},${parityDir},${denoDir},${resolve(fontPath, "..")}`,
              `--allow-write=${parityDir}`,
              resolve(runtimeRoot, "server/render-frame.ts"),
              requestPath,
            ], { encoding: "utf8" });
            expect(deno.status).toBe(0);
            expect(createHash("sha256").update(readFileSync(denoOutputPath)).digest("hex")).toBe(nodeHash);
            image.delete();
            surface.delete();
          }
        }
      }
    } finally {
      resources.delete();
      rmSync(parityDir, { recursive: true, force: true });
    }
    expect(createHash("sha256").update(hashes.join("\n")).digest("hex")).toBe(
      "fa22e29864411964b467c30988f84f46a95b913bdee894b28909a09b543addf5",
    );
    expect(hashes).toHaveLength(48);
  }, 60_000);
});
