import { editTimeline } from "@/lib/generative-api";

describe("editTimeline", () => {
  afterEach(() => {
    Reflect.deleteProperty(global, "fetch");
  });

  it("posts the guided revision and render baseline CAS pair", async () => {
    const fetchMock = jest.fn().mockResolvedValue({ ok: true } as Response);
    Object.defineProperty(global, "fetch", {
      value: fetchMock,
      writable: true,
      configurable: true,
    });
    const slots = [
      {
        slot_id: "segment-1",
        clip_index: 0,
        in_s: 1,
        duration_beats: null,
        duration_s: 4.5,
        removed: false,
      },
    ];

    await editTimeline("item-1", "guided", slots, "plan-item", {
      revision_number: 7,
      base_generation: "render-gen-7",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/plan/plan-items/item-1/variants/guided/timeline",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          slots,
          revision_number: 7,
          base_generation: "render-gen-7",
        }),
      }),
    );
  });
});
