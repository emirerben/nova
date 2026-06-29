// sfx-preview-urls.ts — pure resolution of SFX placements → playable audio URLs
// for the live timeline preview (useSfxPreview).
//
// Glossary effects carry no src_gcs_path; their playable URL comes from the
// public GET /sound-effects payload field `preview_audio_url` (see
// app/routes/sound_effects.py). A field-name drift here silently kills SFX
// preview audio — the bug is invisible because the download bake reads the
// persisted placement server-side and works regardless.

import type { SoundEffectPlacement } from "@/lib/plan-api";
import type { SoundEffectSummary } from "@/lib/sfx-api";

/** The key under which a placement's playable URL is stored and looked up.
 *  Must stay in sync with useSfxPreview's lookup (src_gcs_path, then id). */
export function sfxUrlKey(p: SoundEffectPlacement): string {
  return p.src_gcs_path || p.id;
}

export interface ResolvedSfxUrls {
  /** Map keyed by sfxUrlKey → glossary preview URL, ready to merge into state. */
  glossaryUrls: Record<string, string>;
  /** Placements whose audio lives under users/ and needs a signed-URL fetch. */
  userUploadPaths: SoundEffectPlacement[];
}

/**
 * Resolve playable URLs for placements that don't already have one.
 * Glossary effects resolve synchronously from the loaded glossary; user
 * uploads are returned for the caller to fetch a signed URL asynchronously.
 */
export function resolveSfxPreviewUrls(
  placements: SoundEffectPlacement[],
  glossary: SoundEffectSummary[],
  existing: Record<string, string>,
): ResolvedSfxUrls {
  const glossaryUrls: Record<string, string> = {};
  const userUploadPaths: SoundEffectPlacement[] = [];

  for (const p of placements) {
    const key = sfxUrlKey(p);
    if (!key || existing[key] || glossaryUrls[key]) continue;

    const glossaryMatch = glossary.find((g) => g.id === p.sound_effect_id);
    if (glossaryMatch?.preview_audio_url) {
      glossaryUrls[key] = glossaryMatch.preview_audio_url;
    } else if (p.src_gcs_path.startsWith("users/")) {
      userUploadPaths.push(p);
    }
  }

  return { glossaryUrls, userUploadPaths };
}
