import { resolveSfxPreviewUrls, sfxUrlKey } from "@/lib/sfx-preview-urls";
import type { SoundEffectPlacement } from "@/lib/plan-api";
import type { SoundEffectSummary } from "@/lib/sfx-api";

function glossaryEffect(over: Partial<SoundEffectSummary> = {}): SoundEffectSummary {
  return {
    id: "gfx-1",
    name: "Fah",
    duration_s: 1.2,
    published_at: "2026-01-01T00:00:00Z",
    archived_at: null,
    status: "ready",
    source_filename: "fah.mp3",
    preview_audio_url: "https://cdn.example.com/fah.mp3",
    ...over,
  };
}

function placement(over: Partial<SoundEffectPlacement> = {}): SoundEffectPlacement {
  return {
    id: "p-1",
    src_gcs_path: "",
    at_s: 2.0,
    gain: 1.0,
    ...over,
  };
}

describe("resolveSfxPreviewUrls", () => {
  // Regression for the prod bug: backend GET /sound-effects returns
  // `preview_audio_url`, the frontend used to read `preview_url`, so glossary
  // SFX never got a playable URL and stayed silent in the preview.
  it("resolves a glossary placement from preview_audio_url, keyed by placement id", () => {
    const p = placement({ id: "p-1", src_gcs_path: "", sound_effect_id: "gfx-1" });
    const { glossaryUrls, userUploadPaths } = resolveSfxPreviewUrls(
      [p],
      [glossaryEffect({ id: "gfx-1" })],
      {},
    );
    expect(glossaryUrls["p-1"]).toBe("https://cdn.example.com/fah.mp3");
    expect(userUploadPaths).toHaveLength(0);
  });

  it("does NOT resolve when the glossary effect has no preview_audio_url", () => {
    const p = placement({ sound_effect_id: "gfx-1" });
    const { glossaryUrls } = resolveSfxPreviewUrls(
      [p],
      [glossaryEffect({ id: "gfx-1", preview_audio_url: null })],
      {},
    );
    expect(Object.keys(glossaryUrls)).toHaveLength(0);
  });

  it("routes a user-uploaded placement to userUploadPaths for async signing", () => {
    const p = placement({ id: "p-2", src_gcs_path: "users/u1/boom.mp3", sound_effect_id: undefined });
    const { glossaryUrls, userUploadPaths } = resolveSfxPreviewUrls([p], [], {});
    expect(Object.keys(glossaryUrls)).toHaveLength(0);
    expect(userUploadPaths).toEqual([p]);
  });

  it("skips placements that already have a resolved URL", () => {
    const p = placement({ id: "p-1", sound_effect_id: "gfx-1" });
    const { glossaryUrls } = resolveSfxPreviewUrls(
      [p],
      [glossaryEffect({ id: "gfx-1" })],
      { "p-1": "https://cdn.example.com/already.mp3" },
    );
    expect(Object.keys(glossaryUrls)).toHaveLength(0);
  });

  it("keys by src_gcs_path when present, else placement id (matches useSfxPreview lookup)", () => {
    expect(sfxUrlKey(placement({ id: "p-1", src_gcs_path: "" }))).toBe("p-1");
    expect(sfxUrlKey(placement({ id: "p-1", src_gcs_path: "users/u1/x.mp3" }))).toBe("users/u1/x.mp3");
  });
});
