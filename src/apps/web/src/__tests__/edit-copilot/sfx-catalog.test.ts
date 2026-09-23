import { describe, expect, it } from "@jest/globals";
import { orderSfxCatalogForRequest, sfxMatchScore, SFX_CATEGORIES } from "@/lib/edit-copilot/sfx-catalog";
import { copilotRequestTexts } from "@/lib/edit-copilot/useEditCopilot";
import type { SoundEffectSummary } from "@/lib/sfx-api";

function fx(
  id: string,
  name: string,
  category: string | null = null,
  terms: string[] = [],
  over: Partial<SoundEffectSummary> = {},
): SoundEffectSummary {
  return {
    id,
    name,
    duration_s: 0.5,
    published_at: null,
    archived_at: null,
    status: "ready",
    source_filename: null,
    category,
    search_terms: terms,
    ...over,
  };
}

/** Rows are written in upload order; GET /sound-effects serves newest first. */
function apiOrder(rows: SoundEffectSummary[]): SoundEffectSummary[] {
  return [...rows].reverse();
}

// Same fixture as tests/services/test_sfx_catalog.py, in upload order.
const LIBRARY = [
  fx("buzz", "Wrong buzzer", "rejection", ["buzzer", "wrong answer", "quiz"]),
  fx("buzz-long", "Wrong buzzer long", "rejection", ["buzzer", "wrong answer"]),
  fx("buzz-2", "Wrong buzzer double", "rejection", ["buzzer"]),
  fx("ding", "Correct ding", "approval", ["ding", "bell", "correct answer"]),
  fx("cheer", "Crowd cheer", "approval", ["cheer", "crowd"]),
  fx("roll", "Drum roll", "suspense", ["drum roll"]),
  fx("whoosh", "Whoosh fast", "transition", ["whoosh"]),
  fx("rewind", "Tape rewind", "transition", ["rewind", "cassette"]),
  fx("boom", "Bass boom", "impact", ["vine boom", "boom"]),
  fx("boing", "Boing", "comedy", ["boing", "bounce"]),
  fx("pop", "Soft pop", "ui", ["pop"]),
  fx("whistle", "Referee whistle", "sports", ["whistle", "referee"]),
  fx("cash", "Cash register", "money", ["ka ching"]),
  fx("smart-pop", "Smart soft pop", null, [], { role_tags: ["visual_enter_soft"] }),
  fx("fah", "Fah"),
];

const ids = (effects: SoundEffectSummary[]) => effects.map((effect) => effect.id);

describe("sfxMatchScore mirrors services.sfx_catalog.match_score", () => {
  const buzzer = LIBRARY[0];

  it.each<[string, SoundEffectSummary, number]>([
    // name hits 3 each, term hit 1.5, "wrong answer" phrase +2
    ["buzzer for the wrong answer", buzzer, 9.5],
    ["the wrong answer", buzzer, 6.5],
    ["quiz", buzzer, 1.5],
    // the category word is worth +1
    ["rejection buzzer", buzzer, 4],
    // primary keyword (first search term) +1 breaks the name-hit tie
    ["a pop", fx("soft", "Soft pop", "ui", ["pop", "appear"]), 4],
    ["a pop", fx("check", "Checkmark pop", "approval", ["check", "pop"]), 3],
    ["ding ding", fx("double", "Double ding", "approval", ["ding", "ding ding"]), 5],
  ])("scores %j against %s", (query, effect, expected) => {
    expect(sfxMatchScore(query, effect)).toBe(expected);
  });

  it("matches whole words only and ignores request filler", () => {
    const tape = fx("rewind", "Tape rewind", "transition", ["rewind"]);
    expect(sfxMatchScore("tap", tape)).toBe(0);
    expect(sfxMatchScore("tape", tape)).toBeGreaterThan(0);
    expect(sfxMatchScore("add a sound effect please", buzzer)).toBe(0);
  });
});

describe("orderSfxCatalogForRequest", () => {
  it("puts the effects the request names first, name hits and earlier uploads ahead", () => {
    const ordered = orderSfxCatalogForRequest(apiOrder(LIBRARY), {
      requestTexts: ["buzzer for the wrong answer"],
    });
    expect(ids(ordered).slice(0, 3)).toEqual(["buzz", "buzz-long", "buzz-2"]);
    expect(ordered).toHaveLength(LIBRARY.length);
    expect(new Set(ids(ordered)).size).toBe(LIBRARY.length);
  });

  it("without a request, leads with one headline effect per category", () => {
    const ordered = ids(orderSfxCatalogForRequest(apiOrder(LIBRARY)));
    expect(ordered[0]).toBe("buzz");
    const byId = new Map(LIBRARY.map((effect) => [effect.id, effect]));
    const firstRound = ordered.slice(0, SFX_CATEGORIES.length + 1);
    expect(new Set(firstRound.map((id) => byId.get(id)!.category ?? "other")))
      .toEqual(new Set([...SFX_CATEGORIES, "other"]));
    // Role-tagged smart-* core outranks an untiered legacy upload in "other".
    expect(ordered.indexOf("smart-pop")).toBeLessThan(ordered.indexOf("fah"));
  });

  it("keeps referenced effects first, in the given order, then request matches", () => {
    const ordered = orderSfxCatalogForRequest(apiOrder(LIBRARY), {
      keepEffectIds: ["whistle", "not-in-library", null, "ding", "whistle"],
      requestTexts: ["add a buzzer here"],
    });
    expect(ids(ordered).slice(0, 5)).toEqual(["whistle", "ding", "buzz", "buzz-long", "buzz-2"]);
    expect(ordered).toHaveLength(LIBRARY.length);
  });

  it("ranks earlier user messages after the current one", () => {
    const followUp = orderSfxCatalogForRequest(apiOrder(LIBRARY), {
      requestTexts: ["at the end", "add a drum roll"],
    });
    expect(ids(followUp)[0]).toBe("roll");

    const both = orderSfxCatalogForRequest(apiOrder(LIBRARY), {
      requestTexts: ["a ding", "a buzzer"],
    });
    expect(ids(both).slice(0, 4)).toEqual(["ding", "buzz", "buzz-long", "buzz-2"]);
  });

  it("uses upload order as the curated tie-break and puts base variants first", () => {
    const rows = [
      fx("buzz", "Wrong buzzer", "rejection", ["buzzer"]),
      fx("buzz-long", "Wrong buzzer long", "rejection", ["buzzer"]),
      fx("aww", "Crowd aww", "rejection", ["aww"]),
      fx("ding", "Correct ding", "approval", ["ding"]),
      fx("double", "Double ding", "approval", ["ding", "ding ding"]),
    ];
    expect(ids(orderSfxCatalogForRequest(apiOrder(rows))).slice(0, 4))
      .toEqual(["buzz", "ding", "aww", "double"]);
    expect(ids(orderSfxCatalogForRequest(apiOrder(rows), { requestTexts: ["a ding"] }))[0]).toBe("ding");
    expect(ids(orderSfxCatalogForRequest(apiOrder(rows), { requestTexts: ["ding ding"] }))[0]).toBe("double");
  });

  it("prefers the primary keyword, then the level-matched library over quiet smart-* core", () => {
    const rows = [
      // Uploaded first, so only the tier keeps it behind the library effect.
      fx("smart", "Smart soft pop", null, [], { role_tags: ["visual_enter_soft"] }),
      fx("check", "Checkmark pop", "approval", ["check", "pop"]),
      fx("soft", "Soft pop", "ui", ["pop", "appear"]),
    ];
    expect(ids(orderSfxCatalogForRequest(apiOrder(rows), { requestTexts: ["a pop"] })))
      .toEqual(["soft", "check", "smart"]);
  });

  it("degrades to name matching and newest-last order for legacy rows without metadata", () => {
    const legacy = apiOrder([fx("a", "Whoosh"), fx("b", "Buzzer"), fx("c", "Ding")].map(
      (effect) => ({ ...effect, category: undefined, search_terms: undefined }),
    ));
    expect(ids(orderSfxCatalogForRequest(legacy, { requestTexts: ["buzzer"] }))).toEqual(["b", "a", "c"]);
    expect(ids(orderSfxCatalogForRequest(legacy))).toEqual(["a", "b", "c"]);
  });
});

describe("copilotRequestTexts", () => {
  it("leads with the current message, then the latest two earlier user messages", () => {
    expect(copilotRequestTexts("at the end", [
      { role: "user", content: "make the title bigger" },
      { role: "assistant", content: "Done." },
      { role: "user", content: "add a buzzer" },
      { role: "assistant", content: "Where should it go?" },
      { role: "user", content: "   " },
    ])).toEqual(["at the end", "add a buzzer", "make the title bigger"]);
    expect(copilotRequestTexts("add a buzzer", [])).toEqual(["add a buzzer"]);
  });
});
