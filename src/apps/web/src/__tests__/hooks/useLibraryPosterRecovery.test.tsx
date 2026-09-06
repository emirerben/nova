import { act, renderHook, waitFor } from "@testing-library/react";
import { useState } from "react";
import {
  POSTER_ERROR_REFRESH_DEBOUNCE_MS,
  POSTER_RECOVERY_DELAYS_MS,
  POSTER_REFRESH_TIMEOUT_MS,
  POSTER_REFRESH_TRANSPORT_DELAYS_MS,
  useLibraryPosterRecovery,
} from "@/hooks/useLibraryPosterRecovery";
import { refreshMyJobPosters, type LibraryJob } from "@/lib/me-api";

jest.mock("@/lib/me-api", () => ({
  ...jest.requireActual("@/lib/me-api"),
  refreshMyJobPosters: jest.fn(),
}));

function job(overrides: Partial<LibraryJob> = {}): LibraryJob {
  return {
    id: "job-1",
    mode: "generative",
    status: "ready",
    raw_status: "completed",
    output_url: "https://example.test/video.mp4",
    poster_url: null,
    poster_identity: "variant-1:generation-1",
    poster_status: "repairing",
    output_variant_id: "variant-1",
    tiktok_publishable: false,
    tiktok_publication: null,
    created_at: "2026-09-05T00:00:00Z",
    content_plan_item_id: "item-1",
    feedback_signal: null,
    ...overrides,
  };
}

function useHarness(initialJobs: LibraryJob[]) {
  const [jobs, setJobs] = useState(initialJobs);
  return {
    jobs,
    recovery: useLibraryPosterRecovery({ enabled: true, jobs, setJobs }),
  };
}

describe("useLibraryPosterRecovery", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.mocked(refreshMyJobPosters).mockReset();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it("repairs a ready job with missing poster metadata and reconciles the Gallery row", async () => {
    jest.mocked(refreshMyJobPosters).mockResolvedValue({
      jobs: [
        {
          id: "job-1",
          poster_url: "https://example.test/poster.jpg",
          poster_identity: "variant-1:generation-1",
          poster_status: "ready",
        },
      ],
    });
    const { result } = renderHook(() => useHarness([job()]));

    await act(async () => {
      jest.advanceTimersByTime(POSTER_RECOVERY_DELAYS_MS[0]);
      await Promise.resolve();
    });

    await waitFor(() =>
      expect(result.current.jobs[0].poster_url).toBe(
        "https://example.test/poster.jpg",
      ),
    );
    expect(refreshMyJobPosters).toHaveBeenCalledWith(
      ["job-1"],
      expect.any(AbortSignal),
      [],
    );
  });

  it("reports a browser-broken poster to the repair endpoint after the debounce", async () => {
    jest.mocked(refreshMyJobPosters).mockResolvedValue({
      jobs: [
        {
          id: "job-1",
          poster_url: "https://example.test/repaired.jpg",
          poster_identity: "variant-1:generation-1",
          poster_status: "ready",
        },
      ],
    });
    const existing = job({
      poster_url: "https://example.test/expired.jpg",
      poster_status: "ready",
    });
    const { result } = renderHook(() => useHarness([existing]));

    act(() =>
      result.current.recovery.onPosterLoadError(
        existing.id,
        existing.poster_identity ?? null,
      ),
    );
    await act(async () => {
      jest.advanceTimersByTime(POSTER_ERROR_REFRESH_DEBOUNCE_MS);
      await Promise.resolve();
    });

    await waitFor(() => expect(refreshMyJobPosters).toHaveBeenCalled());
    expect(refreshMyJobPosters).toHaveBeenCalledWith(
      ["job-1"],
      expect.any(AbortSignal),
      ["job-1"],
    );
  });

  it("does not immediately refresh repeatedly for one stable poster identity", async () => {
    const existing = job({
      poster_url: "https://example.test/expired.jpg",
      poster_status: "ready",
    });
    jest.mocked(refreshMyJobPosters).mockResolvedValue({
      jobs: [
        {
          id: existing.id,
          poster_url: existing.poster_url ?? null,
          poster_identity: existing.poster_identity ?? null,
          poster_status: "ready",
        },
      ],
    });
    const { result } = renderHook(() => useHarness([existing]));

    act(() =>
      result.current.recovery.onPosterLoadError(
        existing.id,
        existing.poster_identity ?? null,
      ),
    );
    await act(async () => {
      jest.advanceTimersByTime(POSTER_ERROR_REFRESH_DEBOUNCE_MS);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(refreshMyJobPosters).toHaveBeenCalledTimes(1);

    act(() =>
      result.current.recovery.onPosterLoadError(
        existing.id,
        existing.poster_identity ?? null,
      ),
    );
    await act(async () => {
      jest.advanceTimersByTime(POSTER_ERROR_REFRESH_DEBOUNCE_MS);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(refreshMyJobPosters).toHaveBeenCalledTimes(1);
  });

  it("backs off after a rejected refresh instead of spinning", async () => {
    jest.mocked(refreshMyJobPosters).mockRejectedValue(new Error("offline"));
    const { result } = renderHook(() => useHarness([job()]));

    await act(async () => {
      jest.advanceTimersByTime(POSTER_RECOVERY_DELAYS_MS[0]);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(refreshMyJobPosters).toHaveBeenCalledTimes(1);

    await act(async () => {
      jest.advanceTimersByTime(POSTER_REFRESH_TRANSPORT_DELAYS_MS[1] - 1);
      await Promise.resolve();
    });
    expect(refreshMyJobPosters).toHaveBeenCalledTimes(1);

    await act(async () => {
      jest.advanceTimersByTime(1);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(refreshMyJobPosters).toHaveBeenCalledTimes(2);
    expect(result.current.recovery.refreshUnavailableJobIds).toEqual(new Set());
  });

  it("aborts an in-flight refresh when its timeout expires", async () => {
    let requestSignal: AbortSignal | undefined;
    jest.mocked(refreshMyJobPosters).mockImplementation((_jobIds, signal) => {
      requestSignal = signal;
      return new Promise(() => undefined);
    });
    const { unmount } = renderHook(() => useHarness([job()]));

    await act(async () => {
      jest.advanceTimersByTime(POSTER_RECOVERY_DELAYS_MS[0]);
      await Promise.resolve();
    });
    expect(requestSignal?.aborted).toBe(false);

    await act(async () => {
      jest.advanceTimersByTime(POSTER_REFRESH_TIMEOUT_MS);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(requestSignal?.aborted).toBe(true);
    unmount();
  });

  it("marks a partial response unavailable after bounded transport retries", async () => {
    jest.mocked(refreshMyJobPosters).mockResolvedValue({ jobs: [] });
    const { result } = renderHook(() => useHarness([job()]));

    await act(async () => {
      jest.advanceTimersByTime(POSTER_RECOVERY_DELAYS_MS[0]);
      await Promise.resolve();
      await Promise.resolve();
    });
    for (const delay of POSTER_REFRESH_TRANSPORT_DELAYS_MS.slice(1)) {
      await act(async () => {
        jest.advanceTimersByTime(delay);
        await Promise.resolve();
        await Promise.resolve();
      });
    }

    expect(refreshMyJobPosters).toHaveBeenCalledTimes(
      POSTER_REFRESH_TRANSPORT_DELAYS_MS.length,
    );
    expect(result.current.recovery.refreshUnavailableJobIds).toEqual(
      new Set(["job-1"]),
    );
  });

  it("settles a persistent repairing poster after the recovery ladder", async () => {
    jest.mocked(refreshMyJobPosters).mockResolvedValue({
      jobs: [
        {
          id: "job-1",
          poster_url: null,
          poster_identity: "variant-1:generation-1",
          poster_status: "repairing",
        },
      ],
    });
    const { result } = renderHook(() => useHarness([job()]));

    await act(async () => {
      jest.advanceTimersByTime(POSTER_RECOVERY_DELAYS_MS[0]);
      await Promise.resolve();
      await Promise.resolve();
    });
    for (const delay of POSTER_RECOVERY_DELAYS_MS.slice(1)) {
      await act(async () => {
        jest.advanceTimersByTime(delay);
        await Promise.resolve();
        await Promise.resolve();
      });
    }

    expect(refreshMyJobPosters).toHaveBeenCalledTimes(
      POSTER_RECOVERY_DELAYS_MS.length,
    );
    expect(result.current.recovery.exhaustedJobIds).toEqual(new Set(["job-1"]));
    expect(result.current.recovery.refreshUnavailableJobIds).toEqual(new Set());
  });

  it("clears a browser failure when the same poster identity later loads", async () => {
    const existing = job({
      poster_url: "https://example.test/expired.jpg",
      poster_status: "ready",
    });
    jest.mocked(refreshMyJobPosters).mockResolvedValue({ jobs: [] });
    const { result } = renderHook(() => useHarness([existing]));

    act(() =>
      result.current.recovery.onPosterLoadError(
        existing.id,
        existing.poster_identity ?? null,
      ),
    );
    await act(async () => {
      jest.advanceTimersByTime(POSTER_ERROR_REFRESH_DEBOUNCE_MS);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(refreshMyJobPosters).toHaveBeenCalledTimes(1);

    act(() =>
      result.current.recovery.onPosterLoadSuccess(
        existing.id,
        existing.poster_identity ?? null,
      ),
    );
    await act(async () => {
      jest.advanceTimersByTime(POSTER_REFRESH_TRANSPORT_DELAYS_MS.at(-1) ?? 0);
      await Promise.resolve();
    });
    expect(refreshMyJobPosters).toHaveBeenCalledTimes(1);
  });
});
