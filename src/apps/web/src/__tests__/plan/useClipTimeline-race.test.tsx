import { act, renderHook, waitFor } from "@testing-library/react";
import { useClipTimeline } from "@/app/plan/_components/useClipTimeline";
import { getTimeline, type TimelineResponse } from "@/lib/generative-api";

jest.mock("@/lib/generative-api", () => ({
  ...jest.requireActual("@/lib/generative-api"),
  getTimeline: jest.fn(),
}));

const mockGetTimeline = getTimeline as jest.MockedFunction<typeof getTimeline>;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function timeline(preset: "golden_hour" | "faded_analog"): TimelineResponse {
  return {
    editable: true,
    reason: null,
    beat_grid: [],
    total_duration_s: 2,
    has_user_edits: false,
    edit_wide_look_presets: ["none", preset],
    slots: [
      {
        slot_id: `slot-${preset}`,
        clip_index: 0,
        source_gcs_path: "source.mp4",
        source_duration_s: 2,
        in_s: 0,
        duration_s: 2,
        duration_beats: null,
        order: 0,
        moment_energy: null,
        moment_description: null,
        look_preset: preset,
      },
    ],
    clips: [{ clip_index: 0, signed_url: null, duration_s: 2, used: true }],
  };
}

describe("useClipTimeline request ownership", () => {
  it("ignores a late response from the previously selected variant", async () => {
    const first = deferred<TimelineResponse>();
    const second = deferred<TimelineResponse>();
    mockGetTimeline
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    const { result, rerender } = renderHook(
      ({ variantId }) => useClipTimeline("item-1", variantId, "plan-item"),
      { initialProps: { variantId: "variant-a" } },
    );
    rerender({ variantId: "variant-b" });

    await act(async () => second.resolve(timeline("faded_analog")));
    await waitFor(() => expect(result.current.loadState).toBe("ready"));
    expect(result.current.editWideLookPresets).toEqual(["none", "faded_analog"]);

    await act(async () => first.resolve(timeline("golden_hour")));
    expect(result.current.editWideLookPresets).toEqual(["none", "faded_analog"]);
    expect(result.current.state.slots[0].lookPreset).toBe("faded_analog");
  });

  it("hides the previous variant's capability while its replacement loads", async () => {
    const second = deferred<TimelineResponse>();
    mockGetTimeline
      .mockResolvedValueOnce(timeline("golden_hour"))
      .mockReturnValueOnce(second.promise);

    const { result, rerender } = renderHook(
      ({ variantId }) => useClipTimeline("item-1", variantId, "plan-item"),
      { initialProps: { variantId: "variant-a" } },
    );
    await waitFor(() => expect(result.current.loadState).toBe("ready"));
    expect(result.current.editWideLookPresets).toEqual(["none", "golden_hour"]);

    rerender({ variantId: "variant-b" });
    await waitFor(() => expect(result.current.loadState).toBe("loading"));
    expect(result.current.editWideLookPresets).toEqual([]);

    await act(async () => second.resolve(timeline("faded_analog")));
    await waitFor(() => expect(result.current.loadState).toBe("ready"));
  });
});
