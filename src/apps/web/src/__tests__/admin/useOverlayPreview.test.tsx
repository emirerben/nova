import { act, renderHook, waitFor } from "@testing-library/react";
import type { RecipeTextOverlay } from "@/app/admin/templates/[id]/components/recipe-types";
import { useOverlayPreview } from "@/app/admin/templates/[id]/components/useOverlayPreview";
import * as adminApi from "@/lib/admin-api";

jest.mock("@/lib/admin-api", () => ({
  fetchOverlayPreview: jest.fn(),
}));

const mockedFetch = adminApi.fetchOverlayPreview as jest.MockedFunction<
  typeof adminApi.fetchOverlayPreview
>;

function makeOverlay(overrides: Partial<RecipeTextOverlay> = {}): RecipeTextOverlay {
  return {
    role: "hook",
    text: "Test",
    position: "center",
    effect: "pop-in",
    font_style: "sans",
    text_size: "medium",
    text_color: "#FFFFFF",
    start_s: 0.0,
    end_s: 3.0,
    start_s_override: null,
    end_s_override: null,
    has_darkening: false,
    has_narrowing: false,
    sample_text: "Test",
    font_cycle_accel_at_s: null,
    ...overrides,
  };
}

beforeAll(() => {
  // jsdom doesn't implement these natively but the hook needs them.
  if (!global.URL.createObjectURL) {
    global.URL.createObjectURL = jest.fn(
      () => `blob:mock-${Math.random().toString(36).slice(2)}`,
    );
  }
  if (!global.URL.revokeObjectURL) {
    global.URL.revokeObjectURL = jest.fn();
  }
});

beforeEach(() => {
  jest.useFakeTimers();
  mockedFetch.mockReset();
});

afterEach(() => {
  jest.useRealTimers();
});

describe("useOverlayPreview", () => {
  test("debounces fetch by 400ms before firing", async () => {
    mockedFetch.mockResolvedValue(new Blob(["x"], { type: "image/png" }));

    const { result } = renderHook(() =>
      useOverlayPreview({
        slotOverlays: [makeOverlay()],
        slotDurationS: 5.0,
        timeInSlotS: 1.0,
        previewSubject: "PERU",
      }),
    );

    // Before debounce window: no fetch yet.
    expect(mockedFetch).not.toHaveBeenCalled();

    act(() => {
      jest.advanceTimersByTime(399);
    });
    expect(mockedFetch).not.toHaveBeenCalled();

    act(() => {
      jest.advanceTimersByTime(2);
    });
    expect(mockedFetch).toHaveBeenCalledTimes(1);

    // Drain the resolved promise so loading flips off.
    await act(async () => {
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.pngUrl).not.toBeNull());
  });

  test("does not refetch when only sub-50ms cursor jitter changes", async () => {
    mockedFetch.mockResolvedValue(new Blob(["x"], { type: "image/png" }));

    const overlays = [makeOverlay()];
    const { rerender } = renderHook(
      ({ t }: { t: number }) =>
        useOverlayPreview({
          slotOverlays: overlays,
          slotDurationS: 5.0,
          timeInSlotS: t,
          previewSubject: "P",
        }),
      { initialProps: { t: 1.0 } },
    );

    act(() => jest.advanceTimersByTime(401));
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockedFetch).toHaveBeenCalledTimes(1);

    // Tiny cursor jitter — under 50ms granularity. Should be a cache hit.
    rerender({ t: 1.01 });
    act(() => jest.advanceTimersByTime(401));
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockedFetch).toHaveBeenCalledTimes(1);
  });

  test("refetches when cursor moves past granularity threshold", async () => {
    mockedFetch.mockResolvedValue(new Blob(["x"], { type: "image/png" }));

    const { rerender } = renderHook(
      ({ t }: { t: number }) =>
        useOverlayPreview({
          slotOverlays: [makeOverlay()],
          slotDurationS: 5.0,
          timeInSlotS: t,
          previewSubject: "P",
        }),
      { initialProps: { t: 1.0 } },
    );

    act(() => jest.advanceTimersByTime(401));
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockedFetch).toHaveBeenCalledTimes(1);

    // Move cursor by 200ms — distinctly different bucket.
    rerender({ t: 1.2 });
    act(() => jest.advanceTimersByTime(401));
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockedFetch).toHaveBeenCalledTimes(2);
  });

  test("aborts in-flight request when input changes", async () => {
    let resolveFn!: (b: Blob) => void;
    let abortedFirst = false;
    mockedFetch.mockImplementation((_params, init) => {
      init?.signal?.addEventListener("abort", () => {
        abortedFirst = true;
      });
      return new Promise<Blob>((resolve) => {
        resolveFn = resolve;
      });
    });

    const { rerender } = renderHook(
      ({ t }: { t: number }) =>
        useOverlayPreview({
          slotOverlays: [makeOverlay()],
          slotDurationS: 5.0,
          timeInSlotS: t,
          previewSubject: "P",
        }),
      { initialProps: { t: 1.0 } },
    );

    act(() => jest.advanceTimersByTime(401));
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockedFetch).toHaveBeenCalledTimes(1);

    // While the first request hangs, change input → should abort it.
    rerender({ t: 2.0 });
    act(() => jest.advanceTimersByTime(401));
    await act(async () => {
      await Promise.resolve();
    });
    expect(abortedFirst).toBe(true);
    expect(mockedFetch).toHaveBeenCalledTimes(2);

    // Resolve the original (now-aborted) request — it must not affect state.
    resolveFn(new Blob(["stale"], { type: "image/png" }));
  });

  test("disabled hook does not fetch", () => {
    mockedFetch.mockResolvedValue(new Blob(["x"], { type: "image/png" }));

    renderHook(() =>
      useOverlayPreview({
        slotOverlays: [makeOverlay()],
        slotDurationS: 5.0,
        timeInSlotS: 1.0,
        previewSubject: "P",
        enabled: false,
      }),
    );

    act(() => jest.advanceTimersByTime(2000));
    expect(mockedFetch).not.toHaveBeenCalled();
  });

  test("hash change clears stale pngUrl; error surfaces message", async () => {
    // First call succeeds.
    mockedFetch.mockResolvedValueOnce(new Blob(["x"], { type: "image/png" }));

    const { result, rerender } = renderHook(
      ({ t }: { t: number }) =>
        useOverlayPreview({
          slotOverlays: [makeOverlay()],
          slotDurationS: 5.0,
          timeInSlotS: t,
          previewSubject: "P",
        }),
      { initialProps: { t: 1.0 } },
    );

    act(() => jest.advanceTimersByTime(401));
    await act(async () => {
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.pngUrl).not.toBeNull());

    // Second call fails. Hash change clears pngUrl immediately so the
    // component falls back to DOM rendering instead of showing a stale
    // frame; the error is surfaced for the optional "stale" indicator.
    mockedFetch.mockRejectedValueOnce(new Error("boom"));
    rerender({ t: 2.0 });
    expect(result.current.pngUrl).toBeNull();
    act(() => jest.advanceTimersByTime(401));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    await waitFor(() => expect(result.current.error).toBe("boom"));
    expect(result.current.pngUrl).toBeNull();
  });
});
