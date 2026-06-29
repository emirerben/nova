/**
 * Unit tests for the SFX dirtiness decision (lib/sfx-dirty.ts) — the logic that
 * replaced the old sticky `sfxDirty` flag. These pin the "does Download need a
 * fresh bake?" predicate that drives both the bake-on-download and the
 * "downloads will include your changes" hint.
 */
import {
  sfxPlacementsEqual,
  bakedSfx,
  sfxNeedsBake,
  sfxPersistDirty,
  type SfxBakeState,
} from "@/lib/sfx-dirty";
import type { SoundEffectPlacement } from "@/lib/plan-api";

function p(over: Partial<SoundEffectPlacement> = {}): SoundEffectPlacement {
  return {
    id: "p1",
    sound_effect_id: null,
    src_gcs_path: "sound-effects/boom/audio.mp3",
    at_s: 4,
    gain: 1,
    trim_start_s: null,
    trim_end_s: null,
    duration_s: 0.5,
    label: "Boom",
    ...over,
  };
}

describe("sfxPlacementsEqual", () => {
  it("empty lists are equal", () => {
    expect(sfxPlacementsEqual([], [])).toBe(true);
  });

  it("identical single placement is equal", () => {
    expect(sfxPlacementsEqual([p()], [p()])).toBe(true);
  });

  it("differing at_s is not equal", () => {
    expect(sfxPlacementsEqual([p({ at_s: 4 })], [p({ at_s: 5 })])).toBe(false);
  });

  it("differing gain is not equal", () => {
    expect(sfxPlacementsEqual([p({ gain: 1 })], [p({ gain: 0.5 })])).toBe(false);
  });

  it("differing trim is not equal", () => {
    expect(
      sfxPlacementsEqual([p({ trim_start_s: 0 })], [p({ trim_start_s: 0.2 })]),
    ).toBe(false);
  });

  it("differing source file is not equal", () => {
    expect(
      sfxPlacementsEqual([p({ src_gcs_path: "a" })], [p({ src_gcs_path: "b" })]),
    ).toBe(false);
  });

  it("different placement count is not equal", () => {
    expect(sfxPlacementsEqual([p()], [p(), p({ id: "p2" })])).toBe(false);
  });

  it("is order-independent (same set, different order)", () => {
    const a = [p({ id: "p1", at_s: 1 }), p({ id: "p2", at_s: 9 })];
    const b = [p({ id: "p2", at_s: 9 }), p({ id: "p1", at_s: 1 })];
    expect(sfxPlacementsEqual(a, b)).toBe(true);
  });

  it("ignores non-render fields (label, duration)", () => {
    expect(
      sfxPlacementsEqual([p({ label: "X", duration_s: 1 })], [p({ label: "Y", duration_s: 2 })]),
    ).toBe(true);
  });
});

describe("bakedSfx", () => {
  it("returns [] when nothing has been baked (pre_sfx_video_path null)", () => {
    expect(bakedSfx({ sound_effects: [p()], pre_sfx_video_path: null })).toEqual([]);
  });

  it("returns the persisted set once a bake has happened", () => {
    const sfx = [p()];
    expect(bakedSfx({ sound_effects: sfx, pre_sfx_video_path: "gs://x_pre_sfx" })).toBe(sfx);
  });

  it("returns [] for a null variant", () => {
    expect(bakedSfx(null)).toEqual([]);
  });
});

describe("sfxNeedsBake", () => {
  it("is false for a null variant", () => {
    expect(sfxNeedsBake([p()], null)).toBe(false);
  });

  it("is true when placements exist but were never baked (saved-but-never-rendered)", () => {
    const v: SfxBakeState = { sound_effects: [p()], pre_sfx_video_path: null };
    expect(sfxNeedsBake([p()], v)).toBe(true);
  });

  it("is false when placements match the baked set", () => {
    const sfx = [p()];
    const v: SfxBakeState = { sound_effects: sfx, pre_sfx_video_path: "gs://x_pre_sfx" };
    expect(sfxNeedsBake([p()], v)).toBe(false);
  });

  it("is true after an edit following a prior bake (THE regression: Download must re-bake)", () => {
    // A bake happened (pre_sfx set, sound_effects=baked), then the user moved
    // the effect. The old !pre_sfx_video_path guard would have skipped the
    // re-bake and exported a stale file. Inline compare catches it.
    const v: SfxBakeState = { sound_effects: [p({ at_s: 4 })], pre_sfx_video_path: "gs://x_pre_sfx" };
    expect(sfxNeedsBake([p({ at_s: 8 })], v)).toBe(true);
  });

  it("is true when all effects were removed after a prior bake", () => {
    const v: SfxBakeState = { sound_effects: [p()], pre_sfx_video_path: "gs://x_pre_sfx" };
    expect(sfxNeedsBake([], v)).toBe(true);
  });

  it("is false with no placements and nothing baked", () => {
    expect(sfxNeedsBake([], { sound_effects: null, pre_sfx_video_path: null })).toBe(false);
  });
});

describe("sfxPersistDirty", () => {
  it("is false for a null variant", () => {
    expect(sfxPersistDirty([p()], null)).toBe(false);
  });

  it("is true when placements differ from the saved set (debounce not flushed)", () => {
    const v: SfxBakeState = { sound_effects: [p({ at_s: 4 })], pre_sfx_video_path: null };
    expect(sfxPersistDirty([p({ at_s: 8 })], v)).toBe(true);
  });

  it("is false when placements match the saved set, even before any bake", () => {
    // Persisted but never baked: persist-clean (no flush needed) yet still
    // needs a bake — the two predicates are intentionally distinct.
    const v: SfxBakeState = { sound_effects: [p()], pre_sfx_video_path: null };
    expect(sfxPersistDirty([p()], v)).toBe(false);
    expect(sfxNeedsBake([p()], v)).toBe(true);
  });
});
