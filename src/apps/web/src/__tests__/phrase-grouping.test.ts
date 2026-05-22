import {
  expandPhraseEditToMemberTexts,
  groupOverlayRowsIntoPhrases,
  type OverlayRow,
} from "@/app/admin/templates/[id]/components/phrase-grouping";

function makeRow(
  slot: number,
  overlay: number,
  text: string,
  start_s = 0,
  end_s = 1,
): OverlayRow {
  return {
    slot_index: slot,
    overlay_index: overlay,
    original_sample_text: text,
    current_sample_text: text,
    start_s,
    end_s,
    role: null,
  };
}

describe("groupOverlayRowsIntoPhrases", () => {
  it("returns empty for empty input", () => {
    expect(groupOverlayRowsIntoPhrases([])).toEqual([]);
  });

  it("groups cumulative-reveal overlays in one slot into a single phrase", () => {
    const rows: OverlayRow[] = [
      makeRow(0, 0, "the", 0, 0.3),
      makeRow(0, 1, "the work", 0.3, 0.6),
      makeRow(0, 2, "the work to", 0.6, 0.9),
      makeRow(0, 3, "the work to get there", 0.9, 1.5),
    ];
    const groups = groupOverlayRowsIntoPhrases(rows);
    expect(groups).toHaveLength(1);
    expect(groups[0].pattern).toBe("cumulative");
    expect(groups[0].member_row_indices).toEqual([0, 1, 2, 3]);
    expect(groups[0].display_text).toBe("the work to get there");
    expect(groups[0].start_s).toBe(0);
    expect(groups[0].end_s).toBe(1.5);
  });

  it("does not cross slot boundaries", () => {
    const rows: OverlayRow[] = [
      makeRow(0, 0, "hi"),
      makeRow(0, 1, "hi there"),
      makeRow(1, 0, "second"),
      makeRow(1, 1, "second slot"),
    ];
    const groups = groupOverlayRowsIntoPhrases(rows);
    expect(groups).toHaveLength(2);
    expect(groups[0].slot_index).toBe(0);
    expect(groups[0].member_row_indices).toEqual([0, 1]);
    expect(groups[1].slot_index).toBe(1);
    expect(groups[1].member_row_indices).toEqual([2, 3]);
  });

  it("closes a cumulative run when the next row does not extend the previous", () => {
    const rows: OverlayRow[] = [
      makeRow(0, 0, "the"),
      makeRow(0, 1, "the work"),
      makeRow(0, 2, "completely different"),
    ];
    const groups = groupOverlayRowsIntoPhrases(rows);
    expect(groups).toHaveLength(2);
    expect(groups[0].member_row_indices).toEqual([0, 1]);
    expect(groups[0].pattern).toBe("cumulative");
    expect(groups[1].member_row_indices).toEqual([2]);
    expect(groups[1].pattern).toBe("singleton");
  });

  it("groups per-word atomized overlays (pre-cumulative shape) into one phrase", () => {
    const rows: OverlayRow[] = [
      makeRow(0, 0, "It's"),
      makeRow(0, 1, "not"),
      makeRow(0, 2, "just"),
    ];
    const groups = groupOverlayRowsIntoPhrases(rows);
    expect(groups).toHaveLength(1);
    expect(groups[0].pattern).toBe("per_word");
    expect(groups[0].display_text).toBe("It's not just");
  });

  it("leaves multi-word standalone Layer-1 overlays as singletons", () => {
    const rows: OverlayRow[] = [
      makeRow(0, 0, "Welcome to the show"),
      makeRow(1, 0, "Outro line"),
    ];
    const groups = groupOverlayRowsIntoPhrases(rows);
    expect(groups).toHaveLength(2);
    expect(groups[0].pattern).toBe("singleton");
    expect(groups[0].display_text).toBe("Welcome to the show");
    expect(groups[1].pattern).toBe("singleton");
    expect(groups[1].display_text).toBe("Outro line");
  });

  it("absorbs trailing-empty overlays into the preceding phrase group", () => {
    // Simulates state after the admin edits "the work to get there" down to
    // just "Hello" — the last 3 cumulative members now hold "".
    const rows: OverlayRow[] = [
      { ...makeRow(0, 0, "the"), current_sample_text: "Hello" },
      { ...makeRow(0, 1, "the work"), current_sample_text: "" },
      { ...makeRow(0, 2, "the work to"), current_sample_text: "" },
      { ...makeRow(0, 3, "the work to get there"), current_sample_text: "" },
    ];
    const groups = groupOverlayRowsIntoPhrases(rows);
    expect(groups).toHaveLength(1);
    expect(groups[0].member_row_indices).toEqual([0, 1, 2, 3]);
    expect(groups[0].display_text).toBe("Hello");
  });

  it("does not fuse two separate phrases through a stray middle empty", () => {
    const rows: OverlayRow[] = [
      makeRow(0, 0, "first"),
      makeRow(0, 1, "first phrase"),
      makeRow(0, 2, ""),
      makeRow(0, 3, "second"),
      makeRow(0, 4, "second phrase"),
    ];
    const groups = groupOverlayRowsIntoPhrases(rows);
    expect(groups).toHaveLength(2);
    // First group absorbs the middle empty as its trailing-empty padding.
    expect(groups[0].member_row_indices).toEqual([0, 1, 2]);
    expect(groups[0].display_text).toBe("first phrase");
    expect(groups[1].member_row_indices).toEqual([3, 4]);
    expect(groups[1].display_text).toBe("second phrase");
  });

  it("marks the group dirty if any underlying row has been edited", () => {
    const rows: OverlayRow[] = [
      { ...makeRow(0, 0, "the"), current_sample_text: "the" },
      {
        ...makeRow(0, 1, "the work"),
        original_sample_text: "the work",
        current_sample_text: "the bench",
      },
    ];
    const groups = groupOverlayRowsIntoPhrases(rows);
    expect(groups).toHaveLength(1);
    expect(groups[0].dirty).toBe(true);
  });
});

describe("expandPhraseEditToMemberTexts (cumulative)", () => {
  const baseGroup = {
    slot_index: 0,
    pattern: "cumulative" as const,
    display_text: "",
    start_s: 0,
    end_s: 1,
    role: null,
    dirty: false,
  };

  it("same word count rebuilds cumulative stages 1:1", () => {
    const group = { ...baseGroup, member_row_indices: [0, 1, 2] };
    expect(expandPhraseEditToMemberTexts(group, "Not just luck")).toEqual([
      "Not",
      "Not just",
      "Not just luck",
    ]);
  });

  it("fewer words than slots pads trailing members with empty string", () => {
    const group = { ...baseGroup, member_row_indices: [0, 1, 2] };
    expect(expandPhraseEditToMemberTexts(group, "Hello")).toEqual([
      "Hello",
      "",
      "",
    ]);
  });

  it("more words than slots compresses surplus into the last member", () => {
    const group = { ...baseGroup, member_row_indices: [0, 1, 2] };
    expect(expandPhraseEditToMemberTexts(group, "A B C D E")).toEqual([
      "A",
      "A B",
      "A B C D E",
    ]);
  });

  it("empty input hides every member overlay", () => {
    const group = { ...baseGroup, member_row_indices: [0, 1, 2] };
    expect(expandPhraseEditToMemberTexts(group, "   ")).toEqual(["", "", ""]);
  });

  it("singleton member just gets the verbatim text", () => {
    const group = { ...baseGroup, member_row_indices: [0] };
    expect(expandPhraseEditToMemberTexts(group, "Welcome to the show")).toEqual([
      "Welcome to the show",
    ]);
  });
});

describe("expandPhraseEditToMemberTexts (per_word)", () => {
  const baseGroup = {
    slot_index: 0,
    pattern: "per_word" as const,
    display_text: "",
    start_s: 0,
    end_s: 1,
    role: null,
    dirty: false,
  };

  it("same word count: one word per member", () => {
    const group = { ...baseGroup, member_row_indices: [0, 1, 2] };
    expect(expandPhraseEditToMemberTexts(group, "It's not just")).toEqual([
      "It's",
      "not",
      "just",
    ]);
  });

  it("fewer words pads with empty strings", () => {
    const group = { ...baseGroup, member_row_indices: [0, 1, 2] };
    expect(expandPhraseEditToMemberTexts(group, "Hello")).toEqual([
      "Hello",
      "",
      "",
    ]);
  });

  it("more words: surplus collapses into the last member", () => {
    const group = { ...baseGroup, member_row_indices: [0, 1, 2] };
    expect(expandPhraseEditToMemberTexts(group, "A B C D E")).toEqual([
      "A",
      "B",
      "C D E",
    ]);
  });
});
