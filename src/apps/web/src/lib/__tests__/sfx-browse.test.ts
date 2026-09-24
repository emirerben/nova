import {
  groupSfxEffects,
  hasSfxCategories,
  SFX_CATEGORY_ORDER,
  sfxDurationLabel,
  sfxQueryMatcher,
  sfxWords,
} from "@/lib/sfx-browse";
import type { SoundEffectSummary } from "@/lib/sfx-api";

function effect(
  id: string,
  name: string,
  category: string | null = null,
  search_terms: string[] = [],
): SoundEffectSummary {
  return {
    id,
    name,
    duration_s: 1,
    published_at: null,
    archived_at: null,
    status: "ready",
    source_filename: null,
    category,
    search_terms,
  };
}

const LIBRARY = [
  effect("buzz", "Wrong buzzer", "rejection", ["wrong", "fail", "buzzer"]),
  effect("drum", "Drum roll + crash", "suspense", ["drum roll", "reveal", "tension"]),
  effect("tape", "Tape rewind", "transition", ["rewind", "scene change"]),
  effect("tap", "Soft tap", "ui", ["tap", "click", "ui"]),
  effect("ding", "Correct ding", "approval", ["correct", "yes", "win"]),
  effect("cash", "Cash register", "money", ["money", "payday", "win"]),
  effect("legacy", "Airhorn", null),
];

const ids = (list: SoundEffectSummary[]) => list.map((e) => e.id);
const matching = (query: string) => ids(LIBRARY.filter(sfxQueryMatcher(query)));

describe("sfxWords", () => {
  it("lowercases, splits on non-word characters and folds plurals like the API", () => {
    expect(sfxWords("Drum roll + CRASH")).toEqual(["drum", "roll", "crash"]);
    expect(sfxWords("Buzzers hits")).toEqual(["buzzer", "hit"]);
    // Short words and -ss words keep their s (API `_stem`).
    expect(sfxWords("yes boss")).toEqual(["yes", "boss"]);
  });

  it("returns no words for null, undefined or empty text", () => {
    expect(sfxWords(null)).toEqual([]);
    expect(sfxWords(undefined)).toEqual([]);
    expect(sfxWords("")).toEqual([]);
  });
});

describe("sfxQueryMatcher", () => {
  it("matches whole words in the name, case-insensitively", () => {
    expect(matching("WRONG BUZZER")).toEqual(["buzz"]);
    expect(matching("crash")).toEqual(["drum"]);
  });

  it("matches whole words in search_terms, including multi-word terms", () => {
    expect(matching("fail")).toEqual(["buzz"]);
    expect(matching("drum roll")).toEqual(["drum"]);
    expect(matching("win")).toEqual(["ding", "cash"]);
  });

  it("never matches inside a word", () => {
    expect(matching("tap")).toEqual(["tap"]); // not "Tape rewind"
    expect(matching("buzz")).toEqual([]);
    expect(matching("ape")).toEqual([]);
  });

  it("requires every query word, and folds plurals", () => {
    expect(matching("wrong crash")).toEqual([]);
    expect(matching("buzzers")).toEqual(["buzz"]);
  });

  it("lowercases locale-independently (a Turkish browser maps I to ı)", () => {
    const original = String.prototype.toLocaleLowerCase;
    String.prototype.toLocaleLowerCase = function (this: string) {
      return original.call(this, "tr-TR");
    };
    try {
      expect(matching("UI")).toEqual(["tap"]);
    } finally {
      String.prototype.toLocaleLowerCase = original;
    }
  });

  it("treats a blank or punctuation-only query as match-all", () => {
    expect(matching("")).toHaveLength(LIBRARY.length);
    expect(matching("  + ")).toHaveLength(LIBRARY.length);
  });

  it("tolerates legacy effects without search_terms", () => {
    const legacy = { ...effect("x", "Airhorn blast"), search_terms: null };
    expect(sfxQueryMatcher("airhorn")(legacy)).toBe(true);
  });

  it("ignores filler words the way the API resolver does", () => {
    expect(matching("buzzer sound")).toEqual(["buzz"]);
    expect(matching("a wrong buzzer sfx please")).toEqual(["buzz"]);
    // Filler is checked before the plural fold: "this" is filler, not "thi".
    expect(matching("this buzzer")).toEqual(["buzz"]);
  });

  it("uses filler words as-is when the query has nothing else", () => {
    const noise = effect("noise", "White noise", "transition");
    expect(sfxQueryMatcher("noise")(noise)).toBe(true);
    expect(sfxQueryMatcher("noise")(LIBRARY[0])).toBe(false);
  });

  it("keeps non-ASCII letters inside words and folds accents", () => {
    expect(sfxWords("Olé şok!")).toEqual(["ole", "sok"]);
    // Decomposed (NFD) text reads the same as composed text.
    expect(sfxWords("S\u0327ok")).toEqual(["sok"]);
    const ok = effect("ok", "OK chime", "approval", ["ok"]);
    // "şok" must not collapse to "ok" and match unrelated effects.
    expect(sfxQueryMatcher("şok")(ok)).toBe(false);
    expect(sfxQueryMatcher("S\u0327ok")(ok)).toBe(false);
    expect(sfxQueryMatcher("ok")(ok)).toBe(true);
    expect(sfxQueryMatcher("ole")(effect("ole", "Olé crowd", "sports"))).toBe(true);
  });

  it("finds Latin words typed with Turkish dotted/dotless i", () => {
    const impact = effect("impact", "Impact hit", "impact");
    expect(sfxQueryMatcher("İmpact")(impact)).toBe(true);
    expect(sfxQueryMatcher("ımpact")(impact)).toBe(true);
  });

  it("folds -es plurals on either side", () => {
    const punch = effect("punch", "Punch hit", "impact", ["crashes"]);
    expect(sfxQueryMatcher("punches")(punch)).toBe(true);
    expect(sfxQueryMatcher("crash")(punch)).toBe(true);
    expect(sfxQueryMatcher("glitches")(effect("g", "Glitch", "transition"))).toBe(true);
  });

  it("does not throw on a malformed search_terms payload", () => {
    const bad = { ...effect("x", "Airhorn"), search_terms: "airhorn" as unknown as string[] };
    expect(() => sfxQueryMatcher("airhorn")(bad)).not.toThrow();
    expect(sfxQueryMatcher("airhorn")(bad)).toBe(true);
  });

  it("re-indexes an effect whose terms were replaced", () => {
    const e = effect("r", "Riser", "suspense", ["build"]);
    expect(sfxQueryMatcher("tension")(e)).toBe(false);
    e.search_terms = ["tension"];
    expect(sfxQueryMatcher("tension")(e)).toBe(true);
  });
});

describe("sfxDurationLabel", () => {
  it("formats seconds to one decimal and returns null without a duration", () => {
    expect(sfxDurationLabel({ ...effect("a", "A"), duration_s: 0.84 })).toBe("0.8s");
    expect(sfxDurationLabel({ ...effect("a", "A"), duration_s: null })).toBeNull();
  });
});

describe("groupSfxEffects", () => {
  it("orders groups by the fixed category order with Other last", () => {
    const groups = groupSfxEffects(LIBRARY);
    expect(groups.map((g) => g.key)).toEqual([
      "transition",
      "approval",
      "rejection",
      "suspense",
      "money",
      "ui",
      "other",
    ]);
    expect(groups.map((g) => g.label)).toEqual([
      "Transitions",
      "Approval",
      "Rejection",
      "Suspense",
      "Money",
      "Text & UI",
      "Other",
    ]);
  });

  it("puts unknown categories under Other and sorts each group A→Z", () => {
    const groups = groupSfxEffects([
      effect("b", "Whoosh 2", "transition"),
      effect("a", "whoosh 10", "transition"),
      effect("c", "Air swipe", "transition"),
      effect("z", "Mystery", "horror"),
    ]);
    expect(groups.map((g) => [g.key, ids(g.effects)])).toEqual([
      ["transition", ["c", "b", "a"]],
      ["other", ["z"]],
    ]);
  });

  it("matches a half-typed word exactly as typed", () => {
    const whip = effect("whip", "Whip pan", "transition");
    const whistle = effect("whistle", "Referee whistle", "sports");
    const keys = (query: string) =>
      groupSfxEffects([whip, whistle], query).flatMap((g) => ids(g.effects));
    // "whis" must not fold to "whi" and pull in "Whip pan".
    expect(keys("whis")).toEqual(["whistle"]);
  });

  it("keeps results while a filler word is half-typed", () => {
    const keys = (query: string) =>
      groupSfxEffects(LIBRARY, query).flatMap((g) => ids(g.effects));
    expect(keys("wrong buzzer sou")).toEqual(["buzz"]);
    expect(keys("wrong buzzer e")).toEqual(["buzz"]);
    // A lone half-word is not dropped.
    expect(keys("sou")).toEqual([]);
  });

  it("falls back to word starts for the last word only when nothing matches whole", () => {
    const keys = (query: string) =>
      groupSfxEffects(LIBRARY, query).flatMap((g) => ids(g.effects));
    expect(keys("buz")).toEqual(["buzz"]);
    expect(keys("wrong buz")).toEqual(["buzz"]);
    expect(keys("ta")).toEqual(["tape", "tap"]);
    // Whole-word hits exist: no widening.
    expect(keys("tap")).toEqual(["tap"]);
    // Only the last word may be partial.
    expect(keys("wro buzzer")).toEqual([]);
    expect(keys("kazoo")).toEqual([]);
    // The matcher itself stays whole-word unless asked.
    expect(matching("buz")).toEqual([]);
    expect(LIBRARY.filter(sfxQueryMatcher("buz", { prefixLast: true })).map((e) => e.id)).toEqual([
      "buzz",
    ]);
  });

  it("filters before grouping and drops empty groups", () => {
    expect(groupSfxEffects(LIBRARY, "win").map((g) => [g.key, ids(g.effects)])).toEqual([
      ["approval", ["ding"]],
      ["money", ["cash"]],
    ]);
    expect(groupSfxEffects(LIBRARY, "nothing here")).toEqual([]);
  });

  it("returns the caller's effect objects untouched (preview URLs survive)", () => {
    const withUrl = { ...effect("p", "Pop", "ui"), preview_audio_url: "https://cdn/pop.m4a" };
    expect(groupSfxEffects([withUrl])[0].effects[0]).toBe(withUrl);
  });

  it("hasSfxCategories is false only for legacy/unknown-only libraries", () => {
    expect(hasSfxCategories(LIBRARY)).toBe(true);
    expect(hasSfxCategories([effect("a", "Airhorn"), effect("z", "Mystery", "horror")])).toBe(false);
    expect(hasSfxCategories([])).toBe(false);
  });

  it("orders exactly the nine categories the KRI-173 API ships", () => {
    // Pins the web list; the API side of the contract is
    // src/apps/api/tests/services/test_sfx_catalog_web_parity.py, which fails
    // when SFX_CATEGORIES and SFX_CATEGORY_ORDER drift apart.
    expect([...SFX_CATEGORY_ORDER].sort()).toEqual(
      ["approval", "comedy", "impact", "money", "rejection", "sports", "suspense", "transition", "ui"],
    );
  });
});
