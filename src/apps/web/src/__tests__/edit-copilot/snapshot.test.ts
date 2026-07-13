import { buildCopilotSnapshot } from "@/lib/edit-copilot/snapshot";
import type { DraftSlot } from "@/app/generative/timeline-math";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

function bar(over: Partial<TextElementBar> = {}): TextElementBar {
  return {
    id: "bar-1",
    text: "morning market",
    start_s: 0,
    end_s: 3,
    role: "generative_intro",
    ...over,
  };
}

function slot(over: Partial<DraftSlot> = {}): DraftSlot {
  return {
    key: "slot-1",
    slotId: "slot-1",
    clipIndex: 0,
    inS: 1,
    durationS: 3,
    durationBeats: null,
    removed: false,
    momentDescription: "coffee pour",
    ...over,
  };
}

describe("buildCopilotSnapshot", () => {
  it("excludes narrated captions but preserves the flag", () => {
    const snapshot = buildCopilotSnapshot(
      [
        bar({ id: "text", text: "visible" }),
        bar({ id: "caption", role: "narrated_caption", text: "caption" }),
      ],
      [slot()],
      [{ source_duration_s: 8 }],
      { text_elements: true, timeline: true },
    );

    expect(snapshot.text_bars).toHaveLength(1);
    expect(snapshot.text_bars[0].id).toBe("text");
    expect(snapshot.has_narrated_captions).toBe(true);
  });

  it("renders effective style values and output windows", () => {
    const snapshot = buildCopilotSnapshot(
      [
        bar({
          size_class: "small",
          color: undefined,
          letter_spacing: 10,
          line_spacing: undefined,
          max_width_frac: undefined,
        }),
      ],
      [
        slot({ key: "a", slotId: "a", durationS: 2 }),
        slot({ key: "b", slotId: "b", clipIndex: 1, inS: 0.5, durationS: 4 }),
      ],
      [{ source_duration_s: 8 }, { source_duration_s: 10 }],
      { text_elements: true, timeline: true },
    );

    expect(snapshot.text_bars[0]).toMatchObject({
      size_px: 36,
      color: "#FFFFFF",
      font_family: "PlayfairDisplay-Bold",
      alignment: "center",
      max_width_frac: 0.9,
    });
    expect(snapshot.slots.map((s) => [s.output_start_s, s.output_end_s])).toEqual([
      [0, 2],
      [2, 6],
    ]);
    expect(snapshot.total_duration_s).toBe(6);
    expect(snapshot.remaining_duration_s).toBe(54);
    expect(snapshot.allowed_op_families).toEqual(["text", "clip"]);
  });

  it("removes disabled operation families from the snapshot", () => {
    const snapshot = buildCopilotSnapshot(
      [bar()],
      [slot()],
      [{ source_duration_s: 8 }],
      { text_elements: false, timeline: true },
    );

    expect(snapshot.allowed_op_families).toEqual(["clip"]);
  });
});
