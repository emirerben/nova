import { describe, expect, it } from "@jest/globals";
import {
  barsToTextElements,
  buildLyricLineOverrides,
  seedBarsFromVariant,
} from "@/app/plan/items/[id]/_editor/editor-bars";
import type { PlanItemVariant, TextElement } from "@/lib/plan-api";

const originalLyric: TextElement = {
  id: "lyric_L0",
  text: "Burned text",
  start_s: 2.4,
  end_s: 3.2,
  role: "lyric_line",
  color: "#FFFFFF",
  highlight_color: "#A3E635",
  font_family: "Inter",
  size_px: 64,
  source_params: {
    source: "lyric",
    key: "L0",
    identity: "lyric:L0",
    source_text: "Original track text",
  },
};

function variant(text_elements: TextElement[]): PlanItemVariant {
  return {
    variant_id: "v1",
    output_url: null,
    render_status: "ready",
    text_mode: "lyrics",
    style_set_id: null,
    intro_text_size_px: null,
    text_elements,
  } as PlanItemVariant;
}

describe("editor-bars lyric helpers", () => {
  it("can filter lyric projections out while seeding with the frontend flag off", () => {
    expect(seedBarsFromVariant(variant([originalLyric]), { includeLyrics: false })).toEqual([]);
    expect(seedBarsFromVariant(variant([originalLyric]), { includeLyrics: true })).toMatchObject([
      { id: "lyric_L0", role: "lyric_line" },
    ]);
  });

  it("excludes lyric bars from text_elements payloads", () => {
    const normal: TextElement = {
      id: "txt-1",
      text: "Title",
      start_s: 0,
      end_s: 1,
      role: "generative_intro",
    };
    const bars = seedBarsFromVariant(variant([normal, originalLyric]), { includeLyrics: true });
    expect(barsToTextElements(bars, new Map([[normal.id, normal], [originalLyric.id, originalLyric]]))).toEqual([
      expect.objectContaining({ id: "txt-1" }),
    ]);
  });

  it("builds only dirty lyric overrides and carries source-text fingerprints", () => {
    const originals = new Map([[originalLyric.id, originalLyric]]);
    const clean = seedBarsFromVariant(variant([originalLyric]), { includeLyrics: true });
    expect(buildLyricLineOverrides(clean, originals)).toEqual({});

    const dirtyText = clean.map((bar) => ({ ...bar, text: "New text" }));
    expect(buildLyricLineOverrides(dirtyText, originals)).toEqual({
      L0: {
        text: "New text",
        orig_text: "Original track text",
        orig_start_s: 2.4,
      },
    });

    const dirtyStyle = clean.map((bar) => ({ ...bar, color: "#000000" }));
    expect(buildLyricLineOverrides(dirtyStyle, originals)).toEqual({
      L0: {
        style: { color: "#000000" },
        orig_text: "Original track text",
        orig_start_s: 2.4,
      },
    });
  });
});
