import {
  removeGeneratedEffectGroup,
  removeOverlayEffectGroup,
} from "@/lib/overlay-effect-groups";
import type { CameraEffect, MediaOverlay, SoundEffectPlacement } from "@/lib/plan-api";

const overlay = (values: Partial<MediaOverlay> = {}): MediaOverlay => ({
  id: "overlay-1",
  kind: "image",
  src_gcs_path: "users/u/card.png",
  position: "center",
  x_frac: 0.5,
  y_frac: 0.5,
  scale: 0.4,
  start_s: 1,
  end_s: 3,
  z: 0,
  ...values,
});

const sfx = (values: Partial<SoundEffectPlacement> = {}): SoundEffectPlacement => ({
  id: "sfx-1",
  src_gcs_path: "sound-effects/pop.mp3",
  at_s: 1,
  gain: 1,
  ...values,
});

const camera = (values: Partial<CameraEffect> = {}): CameraEffect => ({
  id: "camera-1",
  start_s: 1,
  end_s: 2,
  intensity: 0.04,
  easing: "sine_pulse",
  source: "smart_captions",
  ...values,
});

it("does not guess linkage for legacy ungrouped overlays", () => {
  const state = {
    overlays: [overlay()],
    soundEffects: [sfx({ source: "smart_captions" })],
    cameraEffects: [camera()],
  };

  const result = removeOverlayEffectGroup(state, "overlay-1");

  expect(result.overlays).toEqual([]);
  expect(result.soundEffects).toBe(state.soundEffects);
  expect(result.cameraEffects).toBe(state.cameraEffects);
});

it("does not treat unknown provenance as generated", () => {
  const group = "import-1";
  const state = {
    overlays: [overlay({ source: "uploaded", effect_group_id: group })],
    soundEffects: [sfx({ source: "uploaded", effect_group_id: group })],
    cameraEffects: [camera({ source: "uploaded", effect_group_id: group })],
  };

  const result = removeOverlayEffectGroup(state, "overlay-1");

  expect(result.soundEffects).toBe(state.soundEffects);
  expect(result.cameraEffects).toBe(state.cameraEffects);
});

it("removes generated siblings but preserves manual effects in the same group", () => {
  const group = "event-1";
  const result = removeOverlayEffectGroup(
    {
      overlays: [overlay({ source: "smart_captions", effect_group_id: group })],
      soundEffects: [
        sfx({ id: "generated", source: "smart_captions", effect_group_id: group }),
        sfx({ id: "manual", source: "manual", effect_group_id: group }),
      ],
      cameraEffects: [camera({ effect_group_id: group })],
    },
    "overlay-1",
  );

  expect(result.soundEffects.map((item) => item.id)).toEqual(["manual"]);
  expect(result.cameraEffects).toEqual([]);
});

it("removes generated siblings for a converted media layer", () => {
  const group = "event-2";
  const generatedSfx = sfx({ source: "overlay_suggestion", effect_group_id: group });
  const generatedCamera = camera({ source: "edit_ai", effect_group_id: group });
  const result = removeGeneratedEffectGroup(
    [generatedSfx],
    [generatedCamera],
    "overlay_suggestion",
    group,
  );

  expect(result.soundEffects).toEqual([]);
  expect(result.cameraEffects).toEqual([]);
});
