import type { CameraEffect, MediaOverlay, SoundEffectPlacement } from "@/lib/plan-api";

export interface OverlayEffectState {
  overlays: MediaOverlay[];
  soundEffects: SoundEffectPlacement[];
  cameraEffects: CameraEffect[];
}

export function isGeneratedEffectSource(source: string | null | undefined): boolean {
  return source === "smart_captions" || source === "overlay_suggestion" || source === "edit_ai";
}

export function removeGeneratedEffectGroup(
  soundEffects: SoundEffectPlacement[],
  cameraEffects: CameraEffect[],
  source: string | null | undefined,
  groupId: string | null | undefined,
): Pick<OverlayEffectState, "soundEffects" | "cameraEffects"> {
  const cascade = Boolean(groupId && isGeneratedEffectSource(source));
  return {
    soundEffects: cascade
      ? soundEffects.filter(
          (effect) => effect.effect_group_id !== groupId || !isGeneratedEffectSource(effect.source),
        )
      : soundEffects,
    cameraEffects: cascade
      ? cameraEffects.filter(
          (effect) => effect.effect_group_id !== groupId || !isGeneratedEffectSource(effect.source),
        )
      : cameraEffects,
  };
}

/** Remove one card and only the generated effects explicitly linked to it. */
export function removeOverlayEffectGroup(
  state: OverlayEffectState,
  overlayId: string,
): OverlayEffectState {
  const target = state.overlays.find((overlay) => overlay.id === overlayId);
  if (!target) return state;
  const linkedEffects = removeGeneratedEffectGroup(
    state.soundEffects,
    state.cameraEffects,
    target.source,
    target.effect_group_id,
  );
  return {
    overlays: state.overlays.filter((overlay) => overlay.id !== overlayId),
    ...linkedEffects,
  };
}
