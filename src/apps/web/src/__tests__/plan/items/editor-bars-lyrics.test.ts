import { describe, expect, it } from "@jest/globals";
import {
  barsToPreviewTextElements,
  barsToTextElements,
  buildLyricLineOverrides,
  seedBarsFromLyricSeeds,
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

describe("barsToTextElements includeLyrics option (lyrics-optional elements model)", () => {
  const normal: TextElement = {
    id: "txt-1",
    text: "Title",
    start_s: 0,
    end_s: 1,
    role: "generative_intro",
  };

  it("defaults to excluding lyric bars (legacy, byte-identical to pre-feature)", () => {
    const bars = seedBarsFromVariant(variant([normal, originalLyric]), { includeLyrics: true });
    const originals = new Map([[normal.id, normal], [originalLyric.id, originalLyric]]);
    expect(barsToTextElements(bars, originals)).toEqual([expect.objectContaining({ id: "txt-1" })]);
    expect(barsToTextElements(bars, originals, {})).toEqual([
      expect.objectContaining({ id: "txt-1" }),
    ]);
  });

  it("includes lyric bars when includeLyrics is true (elements model commit path)", () => {
    const bars = seedBarsFromVariant(variant([normal, originalLyric]), { includeLyrics: true });
    const originals = new Map([[normal.id, normal], [originalLyric.id, originalLyric]]);
    const elements = barsToTextElements(bars, originals, { includeLyrics: true });
    expect(elements.map((el) => el.id).sort()).toEqual(["lyric_L0", "txt-1"]);
    expect(elements.find((el) => el.id === "lyric_L0")).toMatchObject({ role: "lyric_line" });
  });
});

describe("editor bar transition metadata", () => {
  it("preserves sequence fade tails through both save and preview serialization", () => {
    const sequence: TextElement = {
      id: "sequence-1",
      text: "Fade exactly",
      start_s: 1,
      end_s: 3,
      role: "generative_sequence",
      effect: "static",
      fade_out_ms: 350,
      glow_color: "#7CFF8A",
      glow_strength: 0.8,
    };
    const bars = seedBarsFromVariant(variant([sequence]));
    const originals = new Map([[sequence.id, sequence]]);

    expect(barsToTextElements(bars, originals)[0].fade_out_ms).toBe(350);
    expect(barsToPreviewTextElements(bars, originals)[0].fade_out_ms).toBe(350);
    expect(barsToTextElements(bars, originals)[0]).toMatchObject({
      glow_color: "#7CFF8A",
      glow_strength: 0.8,
    });
    expect(barsToPreviewTextElements(bars, originals)[0]).toMatchObject({
      glow_color: "#7CFF8A",
      glow_strength: 0.8,
    });
  });

  it("serializes a new bar without a fade tail as null", () => {
    const sequence: TextElement = {
      id: "sequence-new",
      text: "Hard cut",
      start_s: 0,
      end_s: 1,
      role: "generative_sequence",
    };

    expect(
      barsToTextElements(seedBarsFromVariant(variant([sequence])), new Map())?.[0].fade_out_ms,
    ).toBeNull();
  });
});

describe("editor bar horizontal geometry", () => {
  it("preserves alignment, x position, and box width through save and reload", () => {
    const element: TextElement = {
      id: "positioned-text",
      text: "Right aligned in a left box",
      start_s: 0,
      end_s: 2,
      role: "generative_intro",
      alignment: "right",
      position: "custom",
      x_frac: 0.4,
      y_frac: 0.45,
      max_width_frac: 0.4,
    };
    const originals = new Map([[element.id, element]]);
    const saved = barsToTextElements(seedBarsFromVariant(variant([element])), originals);
    const reloaded = seedBarsFromVariant(variant(saved));

    expect(saved[0]).toMatchObject({
      alignment: "right",
      position: "custom",
      x_frac: 0.4,
      max_width_frac: 0.4,
    });
    expect(reloaded[0]).toMatchObject({
      alignment: "right",
      position: "custom",
      x_frac: 0.4,
      max_width_frac: 0.4,
    });
  });
});

describe("seedBarsFromLyricSeeds (GET .../lyric-seeds response → working bars)", () => {
  it("converts TextElement-shaped seeds into bars, preserving role/timing/style", () => {
    const seeds: TextElement[] = [
      {
        id: "lyr-L0",
        text: "First line",
        start_s: 4.2,
        end_s: 6.8,
        role: "lyric_line",
        color: "#FFFFFF",
        highlight_color: "#A3E635",
      },
    ];
    expect(seedBarsFromLyricSeeds(seeds)).toEqual([
      expect.objectContaining({
        id: "lyr-L0",
        text: "First line",
        start_s: 4.2,
        end_s: 6.8,
        role: "lyric_line",
        color: "#FFFFFF",
        highlight_color: "#A3E635",
      }),
    ]);
  });

  it('normalizes a bare "karaoke" effect to "karaoke-line" (the literal every renderer/style path matches on)', () => {
    const seeds: TextElement[] = [
      {
        id: "lyr-L0",
        text: "Word timed",
        start_s: 0,
        end_s: 2,
        role: "lyric_line",
        effect: "karaoke" as unknown as TextElement["effect"],
      },
    ];
    expect(seedBarsFromLyricSeeds(seeds)[0].effect).toBe("karaoke-line");
  });

  it("defaults word-timed lines with no explicit effect to karaoke-line", () => {
    const seeds: TextElement[] = [
      {
        id: "lyr-L0",
        text: "Word timed",
        start_s: 0,
        end_s: 2,
        role: "lyric_line",
        word_timings: [{ word: "Word", start_s: 0, end_s: 0.5 }],
      },
    ];
    expect(seedBarsFromLyricSeeds(seeds)[0].effect).toBe("karaoke-line");
  });

  it("leaves a non-karaoke explicit effect untouched", () => {
    const seeds: TextElement[] = [
      {
        id: "lyr-L0",
        text: "Static line",
        start_s: 0,
        end_s: 2,
        role: "lyric_line",
        effect: "fade-in",
      },
    ];
    expect(seedBarsFromLyricSeeds(seeds)[0].effect).toBe("fade-in");
  });
});
