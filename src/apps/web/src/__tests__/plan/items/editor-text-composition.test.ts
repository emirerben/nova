import {
  buildTimedTextSequence,
  TEXT_ELEMENTS_API_MAX,
  TEXT_ELEMENT_MAX_CHARS,
  splitTextForTimedSequence,
} from "@/app/plan/items/[id]/_editor/editor-text-composition";

describe("timed text composition", () => {
  it("preserves every authored lyric line", () => {
    const lyrics = [
      "Quiero ver la cuarta estrella",
      "Brillar en la camiseta",
      "Soy argento de la cuna",
      "Hasta el cajón",
      "Por Malvinas, por el Diego",
      "Por la última de Leo",
      "Argentina, quiero verte bicampeón",
    ];

    expect(splitTextForTimedSequence(lyrics.join("\n"))).toEqual(lyrics);
  });

  it("places every authored line in consecutive visible windows", () => {
    expect(buildTimedTextSequence("first\nsecond\nthird", 0, 12)).toEqual([
      { text: "first", start_s: 0, end_s: 4 },
      { text: "second", start_s: 4, end_s: 8 },
      { text: "third", start_s: 8, end_s: 12 },
    ]);
  });

  it("rebases a near-end composition so no bar is invisible", () => {
    const sequence = buildTimedTextSequence("one\ntwo\nthree", 11.8, 12);

    expect(sequence).not.toBeNull();
    expect(sequence?.[0].start_s).toBeCloseTo(10.5);
    expect(sequence?.at(-1)?.end_s).toBe(12);
    expect(sequence?.every((item) => item.end_s > item.start_s)).toBe(true);
  });

  it("chunks a large prose paste linearly into four-word beats", () => {
    const prose = Array.from({ length: 40 }, (_unused, index) => `word${index}`).join(" ");

    const chunks = splitTextForTimedSequence(prose);

    expect(chunks).toHaveLength(10);
    expect(chunks[0]).toBe("word0 word1 word2 word3");
    expect(chunks.at(-1)).toBe("word36 word37 word38 word39");
  });

  it("rejects compositions that would create an unsafe number of bars", () => {
    const lines = Array.from(
      { length: TEXT_ELEMENTS_API_MAX + 1 },
      (_unused, index) => `line ${index}`,
    );

    expect(buildTimedTextSequence(lines.join("\n"), 0, 12)).toBeNull();
  });

  it("subdivides oversized authored lines without exceeding the API text limit", () => {
    const oversized = `${"a".repeat(TEXT_ELEMENT_MAX_CHARS - 2)} bb`;

    const chunks = splitTextForTimedSequence(`${oversized}\nclosing line`);

    expect(chunks).toEqual(["a".repeat(TEXT_ELEMENT_MAX_CHARS - 2), "bb", "closing line"]);
    expect(chunks.every((chunk) => chunk.length <= TEXT_ELEMENT_MAX_CHARS)).toBe(true);
  });
});
