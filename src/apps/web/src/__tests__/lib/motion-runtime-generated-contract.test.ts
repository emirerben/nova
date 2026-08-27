import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import aiCatalog from "../../../../../packages/motion-runtime/creator-blocks.ai.json";
import schema from "../../../../../packages/motion-runtime/motion-scene.schema.json";
import {
  COMPATIBLE_CREATOR_MOTION_RUNTIME_HASHES,
  CREATOR_BLOCK_CATALOG,
  MOTION_MAX_WEIGHTED_ACTIVE_FRAMES,
  MOTION_RUNTIME_HASH,
  activeMotionComplexity,
  createCreatorBlockInstance,
  creatorBlockControl,
  creatorBlockDurationFramesV2,
  retimeCreatorBlockManualSpan,
  retimeCreatorBlockSpeed,
  upgradeCreatorBlockInstanceToV2,
  validateMotionInstances,
  type CardStackInstanceV1,
  type KineticWordInstanceV1,
} from "../../../../../packages/motion-runtime/src/index.ts";

const palette = { primary: "#0C0C0E", accent: "#C7FF3D" };

describe("generated Creator Block v2 contract", () => {
  it("publishes nine current preset-v2 entries and an AI snapshot generated from them", () => {
    expect(CREATOR_BLOCK_CATALOG).toHaveLength(9);
    expect(CREATOR_BLOCK_CATALOG.every((entry) => entry.preset_version === 2)).toBe(true);
    expect(aiCatalog.presets.map((entry) => entry.preset_id)).toEqual(
      CREATOR_BLOCK_CATALOG.map((entry) => entry.preset_id),
    );
    expect(aiCatalog.presets.find((entry) => entry.preset_id === "evolving_type")).toEqual(
      expect.objectContaining({
        preset_version: 2,
        default_duration_frames: 159,
        defaults: expect.objectContaining({
          icon_count: 4,
          text_stagger_ms: 45,
          icon_stagger_ms: 70,
          morph_amplitude: 0.65,
          density: "medium",
          layout: "compact",
          order: "forward",
        }),
      }),
    );
  });

  it("projects media parameters to Copilot asset_ids without changing runtime assets", () => {
    for (const presetId of ["card_stack", "film_strip"] as const) {
      const aiEntry = aiCatalog.presets.find((entry) => entry.preset_id === presetId);
      expect(aiEntry?.parameters).toEqual([
        expect.objectContaining({ key: "asset_ids", type: "asset_list" }),
      ]);
      expect(aiEntry?.defaults).toEqual({ asset_ids: [] });
      expect(aiEntry?.parameters.some((parameter) => parameter.key === "assets")).toBe(false);

      const runtimeEntry = CREATOR_BLOCK_CATALOG.find((entry) => entry.preset_id === presetId);
      expect(runtimeEntry?.parameters).toEqual([
        expect.objectContaining({ key: "assets", type: "asset_list" }),
      ]);
      expect(runtimeEntry?.defaults).toEqual({ assets: [] });
    }
  });

  it("keeps generated schema output drift-free and accepts immutable v1 plus v2", () => {
    const runtimeRoot = resolve(process.cwd(), "../../packages/motion-runtime");
    const check = spawnSync(
      process.execPath,
      [resolve(runtimeRoot, "scripts/generate-contract.mjs"), "--check"],
      { encoding: "utf8" },
    );
    expect({ status: check.status, stderr: check.stderr }).toEqual({ status: 0, stderr: "" });
    expect(schema.$id).toBe("https://nova.video/schemas/motion-scene-v3.json");
    expect(schema.$defs.kinetic_word_v1.properties.preset_version.const).toBe(1);
    expect(schema.$defs.kinetic_word.properties.preset_version.const).toBe(2);

    const legacy: KineticWordInstanceV1 = {
      id: "legacy",
      preset_id: "kinetic_word",
      preset_version: 1,
      start_frame: 0,
      end_frame_exclusive: 75,
      palette,
      intensity: 0.72,
      params: { text: "UNCHANGED" },
    };
    expect(validateMotionInstances([legacy]).ok).toBe(true);
    expect(validateMotionInstances([createCreatorBlockInstance({
      id: "current",
      presetId: "kinetic_word",
      startFrame: 0,
      endFrameExclusive: 75,
    })]).ok).toBe(true);
    expect(validateMotionInstances([{ ...legacy, preset_version: 3 }]).ok).toBe(false);
    const invalidStep = createCreatorBlockInstance({
      id: "invalid-step",
      presetId: "kinetic_word",
      startFrame: 0,
      endFrameExclusive: 75,
    });
    invalidStep.motion.speed = 1.013;
    expect(validateMotionInstances([invalidStep]).ok).toBe(false);
  });

  it("upgrades explicitly without changing content, palette, or timing", () => {
    const legacy: KineticWordInstanceV1 = {
      id: "legacy-upgrade",
      preset_id: "kinetic_word",
      preset_version: 1,
      start_frame: 17,
      end_frame_exclusive: 92,
      palette,
      intensity: 0.41,
      params: { text: "PRESERVE ME" },
    };
    const upgraded = upgradeCreatorBlockInstanceToV2(legacy);
    expect(upgraded).toEqual(expect.objectContaining({
      id: legacy.id,
      preset_version: 2,
      start_frame: legacy.start_frame,
      end_frame_exclusive: legacy.end_frame_exclusive,
      palette: legacy.palette,
      intensity: legacy.intensity,
      params: legacy.params,
      motion: expect.objectContaining({ version: 2, speed: 1 }),
    }));
  });

  it("retimes choreography only, keeps start fixed, and uses safe per-preset speed ranges", () => {
    const evolving = createCreatorBlockInstance({
      id: "evolving",
      presetId: "evolving_type",
      startFrame: 30,
      endFrameExclusive: 189,
    });
    expect(creatorBlockDurationFramesV2(evolving)).toBe(159);
    expect(creatorBlockControl("evolving_type", "speed")?.minimum).toBe(0.75);
    const slow = retimeCreatorBlockSpeed(evolving, 0.25, 300);
    expect(slow.start_frame).toBe(30);
    expect(slow.motion.speed).toBe(0.75);
    expect(slow.end_frame_exclusive - slow.start_frame).toBeLessThanOrEqual(240);
    const boundary = retimeCreatorBlockSpeed(evolving, 4, 90);
    expect(boundary.start_frame).toBe(30);
    expect(boundary.end_frame_exclusive).toBe(90);
  });

  it("uses hold before speed for manual v2 trims and clamps impossible spans", () => {
    const evolving = createCreatorBlockInstance({
      id: "manual-evolving",
      presetId: "evolving_type",
      startFrame: 30,
      endFrameExclusive: 189,
    });
    const extended = retimeCreatorBlockManualSpan(evolving, 30, 209, 300);
    expect(extended).toEqual(expect.objectContaining({
      preset_version: 2,
      start_frame: 30,
      end_frame_exclusive: 209,
      motion: expect.objectContaining({ speed: 1, hold_frames: 50 }),
    }));
    const shortened = retimeCreatorBlockManualSpan(evolving, 30, 100, 300);
    expect(shortened.preset_version).toBe(2);
    if (shortened.preset_version !== 2) throw new Error("expected v2 scene");
    expect(shortened.motion.speed).toBeGreaterThan(1);
    expect(shortened.motion.hold_frames).toBeGreaterThanOrEqual(0);
    expect(shortened.end_frame_exclusive).toBe(100);

    const clamped = retimeCreatorBlockManualSpan(evolving, 30, 31, 300);
    expect(clamped.preset_version).toBe(2);
    if (clamped.preset_version !== 2) throw new Error("expected v2 scene");
    expect(clamped.motion.speed).toBe(4);
    expect(clamped.motion.hold_frames).toBe(0);
    expect(clamped.end_frame_exclusive).toBe(76);
  });

  it("preserves legacy timing semantics and v2 motion on same-span moves", () => {
    const legacy: KineticWordInstanceV1 = {
      id: "manual-legacy",
      preset_id: "kinetic_word",
      preset_version: 1,
      start_frame: 10,
      end_frame_exclusive: 85,
      palette,
      intensity: 0.72,
      params: { text: "LEGACY" },
    };
    expect(retimeCreatorBlockManualSpan(legacy, 20, 95, 300)).toEqual({
      ...legacy,
      start_frame: 20,
      end_frame_exclusive: 95,
    });
    const current = createCreatorBlockInstance({
      id: "manual-current",
      presetId: "kinetic_word",
      startFrame: 10,
      endFrameExclusive: 85,
    });
    expect(retimeCreatorBlockManualSpan(current, 20, 95, 300)).toEqual({
      ...current,
      start_frame: 20,
      end_frame_exclusive: 95,
    });
  });

  it("keeps every exposed speed and hold range inside the 240-frame cap", () => {
    for (const entry of CREATOR_BLOCK_CATALOG) {
      const assets = Array.from({ length: entry.min_assets }, (_, index) => ({
        asset_id: `${entry.preset_id}-${index}`,
        gcs_path: `users/u/plan/pool/${entry.preset_id}-${index}.png`,
      }));
      const scene = createCreatorBlockInstance({
        id: `range-${entry.preset_id}`,
        presetId: entry.preset_id,
        startFrame: 0,
        endFrameExclusive: entry.default_duration_frames,
        assets,
      });
      scene.motion.speed = Number(creatorBlockControl(entry, "speed")?.minimum);
      scene.motion.hold_frames = Number(creatorBlockControl(entry, "hold_frames")?.maximum);
      scene.end_frame_exclusive = creatorBlockDurationFramesV2(scene);
      expect(scene.end_frame_exclusive).toBeLessThanOrEqual(240);
      expect(validateMotionInstances([scene], 240).ok).toBe(true);
    }
  });

  it("rejects eight overlapping expensive scenes with the weighted budget", () => {
    const scenes = Array.from({ length: 8 }, (_, index) => createCreatorBlockInstance({
      id: `expensive-${index}`,
      presetId: "evolving_type",
      startFrame: 0,
      endFrameExclusive: 159,
    }));
    expect(activeMotionComplexity(scenes)).toBeGreaterThan(MOTION_MAX_WEIGHTED_ACTIVE_FRAMES);
    expect(validateMotionInstances(scenes)).toEqual(expect.objectContaining({ ok: false }));
  });

  it("keeps persisted v1 media scenes at legacy complexity weight", () => {
    const legacy = Array.from({ length: 8 }, (_, index): CardStackInstanceV1 => ({
      id: `legacy-card-${index}`,
      preset_id: "card_stack",
      preset_version: 1,
      start_frame: 0,
      end_frame_exclusive: 120,
      palette,
      intensity: 0.72,
      params: {
        assets: [
          { asset_id: `a-${index}`, gcs_path: `users/u/plan/pool/a-${index}.png` },
          { asset_id: `b-${index}`, gcs_path: `users/u/plan/pool/b-${index}.png` },
        ],
      },
    }));
    expect(activeMotionComplexity(legacy)).toBe(8 * 120);
    expect(validateMotionInstances(legacy).ok).toBe(true);
  });

  it("publishes the v2/v3/v4 persisted compatibility set with v4 current", () => {
    expect(COMPATIBLE_CREATOR_MOTION_RUNTIME_HASHES).toHaveLength(3);
    expect(COMPATIBLE_CREATOR_MOTION_RUNTIME_HASHES).toContain(MOTION_RUNTIME_HASH);
    expect(MOTION_RUNTIME_HASH).toContain("motion-v4:");
  });
});
