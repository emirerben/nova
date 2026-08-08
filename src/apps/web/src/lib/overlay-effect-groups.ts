import type { CameraEffect, MediaOverlay, SoundEffectPlacement } from "@/lib/plan-api";

export interface OverlayEffectState {
  overlays: MediaOverlay[];
  soundEffects: SoundEffectPlacement[];
  cameraEffects: CameraEffect[];
}

export function isGeneratedEffectSource(source: string | null | undefined): boolean {
  return source === "smart_captions" || source === "overlay_suggestion" || source === "edit_ai";
}

/** Remove one card and only the generated effects explicitly linked to it. */
export function removeOverlayEffectGroup(
  state: OverlayEffectState,
  overlayId: string,
): OverlayEffectState {
  const target = state.overlays.find((overlay) => overlay.id === overlayId);
  if (!target) return state;
  const groupId = target.effect_group_id;
  const cascade = Boolean(groupId && isGeneratedEffectSource(target.source));
  return {
    overlays: state.overlays.filter((overlay) => overlay.id !== overlayId),
    soundEffects: cascade
      ? state.soundEffects.filter(
          (effect) =>
            effect.effect_group_id !== groupId || !isGeneratedEffectSource(effect.source),
        )
      : state.soundEffects,
    cameraEffects: cascade
      ? state.cameraEffects.filter(
          (effect) =>
            effect.effect_group_id !== groupId || !isGeneratedEffectSource(effect.source),
        )
      : state.cameraEffects,
  };
}
