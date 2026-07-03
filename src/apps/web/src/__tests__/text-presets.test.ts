import { describe, expect, it } from "@jest/globals";
import {
  filterTextPresetsByCategory,
  TEXT_PRESETS,
  toggleTextPresetFavorite,
} from "@/lib/text-presets";

describe("text preset categories", () => {
  it("filters preset categories into favorites, trending, and basic", () => {
    const favoriteId = TEXT_PRESETS[0].id;

    const favorites = filterTextPresetsByCategory(TEXT_PRESETS, "favorite", [favoriteId]);
    expect(favorites.map((preset) => preset.id)).toEqual([favoriteId]);

    const trending = filterTextPresetsByCategory(TEXT_PRESETS, "trending", []);
    expect(trending.length).toBeGreaterThan(0);
    expect(trending.every((preset) => preset.trending)).toBe(true);

    const basic = filterTextPresetsByCategory(TEXT_PRESETS, "basic", []);
    expect(basic.length).toBeGreaterThan(0);
    expect(basic.every((preset) => !preset.trending)).toBe(true);
  });

  it("toggles preset favorites without duplicating ids", () => {
    const presetId = TEXT_PRESETS[0].id;

    expect(toggleTextPresetFavorite([], presetId)).toEqual([presetId]);
    expect(toggleTextPresetFavorite([presetId], presetId)).toEqual([]);
    expect(toggleTextPresetFavorite([presetId, presetId], "clean-caption")).toEqual([
      presetId,
      "clean-caption",
    ]);
  });
});
