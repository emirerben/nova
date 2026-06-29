/**
 * SFX dirtiness — the decision logic behind "does Download need a fresh SFX
 * bake?" Pulled out of the plan-item page so it is pure and unit-testable.
 *
 * The page used to carry a sticky `sfxDirty` flag synced across components and
 * reset on variant switch. That was replaced by computing dirtiness inline at
 * Download time from the placements vs what's actually baked into the variant —
 * "nothing changed → instant download" becomes true by construction, with no
 * flag to desync. See plan-eng-review D5.
 */
import type { SoundEffectPlacement } from "@/lib/plan-api";

/** Minimal shape of the variant fields this module reads. */
export interface SfxBakeState {
  /** Placements persisted on the server. */
  sound_effects?: SoundEffectPlacement[] | null;
  /** Set once an SFX bake has run; null means nothing is baked into output_url. */
  pre_sfx_video_path?: string | null;
}

/**
 * Render-equivalence of two placement lists: same set of effects with the same
 * render-affecting fields (file, position, gain, trim). Order-independent
 * (keyed by id) since order doesn't affect the FFmpeg mix.
 */
export function sfxPlacementsEqual(
  a: SoundEffectPlacement[],
  b: SoundEffectPlacement[],
): boolean {
  if (a.length !== b.length) return false;
  const norm = (p: SoundEffectPlacement) =>
    [
      p.id,
      p.src_gcs_path,
      p.sound_effect_id ?? "",
      p.at_s,
      p.gain,
      p.trim_start_s ?? "",
      p.trim_end_s ?? "",
    ].join("|");
  const sa = a.map(norm).sort();
  const sb = b.map(norm).sort();
  return sa.every((v, i) => v === sb[i]);
}

/**
 * The placement set currently baked into the variant's output_url. Only
 * `sound_effects` once a bake has happened (`pre_sfx_video_path` set); before
 * the first bake, nothing is baked.
 */
export function bakedSfx(variant: SfxBakeState | null | undefined): SoundEffectPlacement[] {
  if (!variant) return [];
  return variant.pre_sfx_video_path ? (variant.sound_effects ?? []) : [];
}

/** True when the current placements differ from what's baked into output_url. */
export function sfxNeedsBake(
  placements: SoundEffectPlacement[],
  variant: SfxBakeState | null | undefined,
): boolean {
  if (!variant) return false;
  return !sfxPlacementsEqual(placements, bakedSfx(variant));
}

/**
 * True when the current placements differ from what's SAVED on the server.
 * When set, Download must flush the save before any bake — both the SFX mix and
 * the post-overlay SFX reapply read the persisted placements, not the request.
 */
export function sfxPersistDirty(
  placements: SoundEffectPlacement[],
  variant: SfxBakeState | null | undefined,
): boolean {
  if (!variant) return false;
  return !sfxPlacementsEqual(placements, variant.sound_effects ?? []);
}
