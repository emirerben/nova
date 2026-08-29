// @ts-nocheck
/**
 * WorkspaceHome (v0.44 "basic home") — create block + PAST EDITS grid.
 * Guards the redesign contract: /plan is openly the create-a-new-video page,
 * past edits render below via listMyJobs, SeedUploadCard still gates on
 * activation, and the ideas ledger is gone.
 */
import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import {
  POSTER_ERROR_REFRESH_DEBOUNCE_MS,
  POSTER_REFRESH_MAX_JOB_IDS,
  POSTER_REFRESH_TIMEOUT_MS,
  POSTER_REFRESH_TRANSPORT_DELAYS_MS,
  POSTER_RECOVERY_DELAYS_MS,
  WorkspaceHome,
} from "@/app/plan/_components/workspace/WorkspaceHome";
import { listMyJobs, refreshMyJobPosters } from "@/lib/me-api";
import { libraryPosterIdentity } from "@/lib/library-poster";

jest.mock("@/lib/me-api", () => ({
  listMyJobs: jest.fn(),
  refreshMyJobPosters: jest.fn(),
}));

jest.mock("@/components/library/LibraryTile", () => {
  const { libraryPosterIdentity } = jest.requireActual("@/lib/library-poster");
  return {
    __esModule: true,
    default: ({
      job,
      onDeleted,
      onPosterLoadError,
      onPosterLoadSuccess,
      posterRecoveryExhausted,
      posterRefreshUnavailable,
    }) => (
      <>
        <div
          data-testid={`tile-${job.id}`}
          data-poster-url={job.poster_url ?? ""}
          data-poster-status={job.poster_status ?? ""}
          data-poster-recovery-exhausted={String(Boolean(posterRecoveryExhausted))}
          data-poster-refresh-unavailable={String(Boolean(posterRefreshUnavailable))}
        >
          {job.status}
        </div>
        <button type="button" onClick={() => onDeleted?.(job.id)}>
          Delete {job.id}
        </button>
        <button
          type="button"
          onClick={() => onPosterLoadError?.(job.id, libraryPosterIdentity(job))}
        >
          Fail poster {job.id}
        </button>
        <button
          type="button"
          onClick={() => onPosterLoadSuccess?.(job.id, libraryPosterIdentity(job))}
        >
          Load poster {job.id}
        </button>
      </>
    ),
  };
});

// TikTokConnectionCard reports availability to its parent via onConnection
// (available:false when TikTok isn't reachable for this account, in which
// case the real component itself renders null). WorkspaceHome must mount it
// unconditionally (so the callback ever fires) but hide the surrounding
// "Integrations" heading until availability is confirmed true.
let mockTikTokAvailable = true;
jest.mock("@/components/library/TikTokConnectionCard", () => {
  const React = require("react");
  function MockTikTokConnectionCard({ onConnection }) {
    React.useEffect(() => {
      onConnection?.(mockTikTokAvailable ? { available: true } : { available: false });
    }, [onConnection]);
    return mockTikTokAvailable ? <div data-testid="tiktok-card" /> : null;
  }
  return {
    __esModule: true,
    default: MockTikTokConnectionCard,
  };
});

jest.mock("@/app/plan/_components/SeedUploadCard", () => ({
  __esModule: true,
  default: () => <div data-testid="seed-upload-card" />,
}));

const mockListMyJobs = listMyJobs as jest.MockedFunction<typeof listMyJobs>;
const mockRefreshMyJobPosters =
  refreshMyJobPosters as jest.MockedFunction<typeof refreshMyJobPosters>;

function makePlan(overrides = {}) {
  return {
    id: "plan-1",
    plan_status: "ready",
    activation_status: "none",
    items: [],
    ...overrides,
  };
}

function job(id, status = "ready") {
  const ordinal = Number(id.match(/\d+/)?.[0] ?? 0);
  return {
    id,
    status,
    poster_url: status === "ready" ? `https://example.test/${id}.poster.jpg` : null,
    poster_identity: `${id}:render-1`,
    content_plan_item_id: null,
    created_at: new Date(Date.UTC(2026, 7, 28, 10, 0, 0) - ordinal * 1_000).toISOString(),
  };
}

function renderHome(plan = makePlan()) {
  return render(
    <WorkspaceHome
      plan={plan}
      onRefresh={jest.fn()}
      onPlanChange={jest.fn()}
      onError={jest.fn()}
    />,
  );
}

describe("WorkspaceHome (basic home)", () => {
  beforeEach(() => {
    mockListMyJobs.mockReset().mockResolvedValue({ jobs: [], next_cursor: null });
    mockRefreshMyJobPosters.mockReset().mockResolvedValue({ jobs: [] });
    mockTikTokAvailable = true;
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it("leads with the create block linking to /plan/new", async () => {
    renderHome();
    expect(
      screen.getByRole("heading", { name: "Create a new video" }),
    ).toBeInTheDocument();
    const cta = screen.getByRole("link", { name: "Create a video" });
    expect(cta).toHaveAttribute("href", "/plan/new");
    await waitFor(() => expect(mockListMyJobs).toHaveBeenCalled());
  });

  it("renders past edits from listMyJobs, newest layout intact", async () => {
    mockListMyJobs.mockResolvedValue({
      jobs: [job("j1", "ready"), job("j2", "generating")],
      next_cursor: null,
    });
    renderHome();
    expect(await screen.findByTestId("tile-j1")).toBeInTheDocument();
    expect(screen.getByTestId("tile-j2")).toBeInTheDocument();
    expect(screen.getByText("Your videos")).toBeInTheDocument();
  });

  it("shows the quiet empty line with zero jobs — no ideas ledger anywhere", async () => {
    renderHome();
    expect(
      await screen.findByText("Your finished videos will appear here."),
    ).toBeInTheDocument();
    expect(screen.getByText("Create your first video to get started.")).toBeInTheDocument();
    expect(screen.queryByText(/Pitch your first idea/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Generate with AI/)).not.toBeInTheDocument();
  });

  it("mounts SeedUploadCard only while the plan is activating", async () => {
    const { unmount } = renderHome(makePlan({ activation_status: "seeding" }));
    expect(screen.getByTestId("seed-upload-card")).toBeInTheDocument();
    unmount();
    renderHome(makePlan({ activation_status: "activated" }));
    expect(screen.queryByTestId("seed-upload-card")).not.toBeInTheDocument();
    await waitFor(() => expect(mockListMyJobs).toHaveBeenCalled());
  });

  it("notes a still-generating plan without blocking creation", async () => {
    renderHome(makePlan({ plan_status: "generating" }));
    expect(
      await screen.findByText(/content plan is still being prepared/),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create a video" })).toBeInTheDocument();
  });

  it("pages through more jobs via the cursor", async () => {
    mockListMyJobs
      .mockResolvedValueOnce({ jobs: [job("j1")], next_cursor: "c2" })
      .mockResolvedValueOnce({ jobs: [job("j2")], next_cursor: null });
    renderHome();
    const more = await screen.findByRole("button", { name: "Load more videos" });
    fireEvent.click(more);
    expect(await screen.findByTestId("tile-j2")).toBeInTheDocument();
    expect(mockListMyJobs).toHaveBeenLastCalledWith({ cursor: "c2" });
    expect(
      screen.queryByRole("button", { name: "Load more videos" }),
    ).not.toBeInTheDocument();
  });

  it("removes a deleted video from the local grid without reloading the page", async () => {
    mockListMyJobs.mockResolvedValue({ jobs: [job("j1")], next_cursor: null });
    renderHome();

    expect(await screen.findByTestId("tile-j1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete j1" }));

    await waitFor(() => expect(screen.queryByTestId("tile-j1")).not.toBeInTheDocument());
    expect(screen.getByText("Your finished videos will appear here.")).toBeInTheDocument();
  });

  it("refreshes a missing ready poster with bounded backoff and adopts the JPEG", async () => {
    jest.useFakeTimers();
    const missing = { ...job("j1"), poster_url: null };
    const recovered = {
      id: missing.id,
      poster_url: "https://example.test/j1.poster.jpg?sig=fresh",
      poster_identity: missing.poster_identity,
      poster_status: "ready",
      // A batch response must never replace non-poster job metadata.
      status: "failed",
    };
    mockListMyJobs.mockResolvedValueOnce({ jobs: [missing], next_cursor: null });
    mockRefreshMyJobPosters.mockResolvedValueOnce({ jobs: [recovered] });

    renderHome();
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByTestId("tile-j1")).toHaveAttribute("data-poster-url", "");
    expect(mockListMyJobs).toHaveBeenCalledTimes(1);

    await act(async () => {
      jest.advanceTimersByTime(POSTER_RECOVERY_DELAYS_MS[0]);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mockListMyJobs).toHaveBeenCalledTimes(1);
    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(1);
    expect(mockRefreshMyJobPosters).toHaveBeenCalledWith(
      ["j1"],
      expect.any(AbortSignal),
    );
    expect(screen.getByTestId("tile-j1")).toHaveAttribute(
      "data-poster-url",
      recovered.poster_url,
    );
    expect(screen.getByTestId("tile-j1")).toHaveTextContent("ready");

    act(() => jest.advanceTimersByTime(60_000));
    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(1);
  });

  it("uses the shared rollout-safe identity fallback for legacy rows", async () => {
    jest.useFakeTimers();
    const legacy = {
      ...job("legacy"),
      poster_identity: null,
      output_variant_id: "variant-legacy",
    };
    expect(libraryPosterIdentity(legacy)).toBe("variant-legacy");
    expect(
      libraryPosterIdentity({
        ...legacy,
        output_variant_id: null,
      }),
    ).toBe(legacy.created_at);

    mockListMyJobs.mockResolvedValue({ jobs: [legacy], next_cursor: null });
    mockRefreshMyJobPosters.mockResolvedValue({
      jobs: [
        {
          id: legacy.id,
          poster_url: legacy.poster_url,
          poster_identity: null,
          poster_status: "ready",
        },
      ],
    });
    renderHome();
    expect(await screen.findByTestId("tile-legacy")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Fail poster legacy" }));
    fireEvent.click(screen.getByRole("button", { name: "Fail poster legacy" }));
    await act(async () => {
      jest.advanceTimersByTime(POSTER_ERROR_REFRESH_DEBOUNCE_MS);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(1);
    expect(mockRefreshMyJobPosters).toHaveBeenCalledWith(
      ["legacy"],
      expect.any(AbortSignal),
    );
  });

  it("hard-stops missing-poster recovery after the configured backoff budget", async () => {
    jest.useFakeTimers();
    const missing = { ...job("j1"), poster_url: null };
    mockListMyJobs.mockResolvedValue({ jobs: [missing], next_cursor: null });
    mockRefreshMyJobPosters.mockResolvedValue({
      jobs: [
        {
          id: missing.id,
          poster_url: null,
          poster_identity: missing.poster_identity,
          poster_status: "repairing",
        },
      ],
    });

    renderHome();
    await act(async () => {
      await Promise.resolve();
    });

    for (const delay of POSTER_RECOVERY_DELAYS_MS) {
      await act(async () => {
        jest.advanceTimersByTime(delay);
        await Promise.resolve();
        await Promise.resolve();
      });
    }

    expect(mockListMyJobs).toHaveBeenCalledTimes(1);
    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(
      POSTER_RECOVERY_DELAYS_MS.length,
    );
    expect(screen.getByTestId("tile-j1")).toHaveAttribute(
      "data-poster-recovery-exhausted",
      "true",
    );

    act(() => jest.advanceTimersByTime(120_000));
    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(
      POSTER_RECOVERY_DELAYS_MS.length,
    );
  });

  it("debounces image failures into one immediate refresh per stable poster identity", async () => {
    jest.useFakeTimers();
    const jobs = [job("j1"), job("j2")];
    mockListMyJobs.mockResolvedValue({ jobs, next_cursor: null });
    mockRefreshMyJobPosters.mockResolvedValue({
      jobs: jobs.map((item) => ({
        id: item.id,
        poster_url: item.poster_url,
        poster_identity: item.poster_identity,
        poster_status: "ready",
      })),
    });

    renderHome();
    await act(async () => {
      await Promise.resolve();
    });
    fireEvent.click(screen.getByRole("button", { name: "Fail poster j1" }));
    fireEvent.click(screen.getByRole("button", { name: "Fail poster j2" }));
    fireEvent.click(screen.getByRole("button", { name: "Fail poster j1" }));

    act(() => jest.advanceTimersByTime(POSTER_ERROR_REFRESH_DEBOUNCE_MS - 1));
    expect(mockRefreshMyJobPosters).not.toHaveBeenCalled();

    await act(async () => {
      jest.advanceTimersByTime(1);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mockListMyJobs).toHaveBeenCalledTimes(1);
    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(1);
    expect(new Set(mockRefreshMyJobPosters.mock.calls[0][0])).toEqual(
      new Set(["j1", "j2"]),
    );

    fireEvent.click(screen.getByRole("button", { name: "Fail poster j1" }));
    act(() => jest.advanceTimersByTime(POSTER_ERROR_REFRESH_DEBOUNCE_MS));
    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(1);
  });

  it("does not spend missing-poster retry attempts during an error-driven refresh", async () => {
    jest.useFakeTimers();
    const missing = { ...job("missing"), poster_url: null };
    const brokenImage = job("broken-image");
    mockListMyJobs.mockResolvedValue({
      jobs: [missing, brokenImage],
      next_cursor: null,
    });
    mockRefreshMyJobPosters.mockImplementation(async (jobIds) => ({
      jobs: jobIds.map((jobId) => {
        const item = jobId === missing.id ? missing : brokenImage;
        return {
          id: item.id,
          poster_url: item.poster_url,
          poster_identity: item.poster_identity,
          poster_status: item.poster_url ? "ready" : "repairing",
        };
      }),
    }));

    renderHome();
    await act(async () => {
      await Promise.resolve();
    });

    act(() => jest.advanceTimersByTime(1_000));
    fireEvent.click(
      screen.getByRole("button", { name: "Fail poster broken-image" }),
    );
    await act(async () => {
      jest.advanceTimersByTime(POSTER_ERROR_REFRESH_DEBOUNCE_MS);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(1);

    await act(async () => {
      jest.advanceTimersByTime(
        POSTER_RECOVERY_DELAYS_MS[0] -
          1_000 -
          POSTER_ERROR_REFRESH_DEBOUNCE_MS,
      );
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(2);
    expect(mockRefreshMyJobPosters.mock.calls[1][0]).toContain("missing");
  });

  it("starts a fresh recovery budget when the render identity changes", async () => {
    jest.useFakeTimers();
    const firstRender = { ...job("j1"), poster_url: null };
    const secondRender = {
      ...firstRender,
      poster_identity: "j1:render-2",
    };
    const posterMetadata = (item) => ({
      id: item.id,
      poster_url: item.poster_url,
      poster_identity: item.poster_identity,
      poster_status: "repairing",
    });
    mockListMyJobs.mockResolvedValueOnce({
      jobs: [firstRender],
      next_cursor: null,
    });
    mockRefreshMyJobPosters
      .mockResolvedValueOnce({ jobs: [posterMetadata(firstRender)] })
      .mockResolvedValueOnce({ jobs: [posterMetadata(firstRender)] })
      .mockResolvedValueOnce({ jobs: [posterMetadata(firstRender)] })
      .mockResolvedValueOnce({ jobs: [posterMetadata(firstRender)] })
      .mockResolvedValueOnce({ jobs: [posterMetadata(secondRender)] })
      .mockResolvedValue({ jobs: [posterMetadata(secondRender)] });

    renderHome();
    await act(async () => {
      await Promise.resolve();
    });
    for (const delay of POSTER_RECOVERY_DELAYS_MS) {
      await act(async () => {
        jest.advanceTimersByTime(delay);
        await Promise.resolve();
        await Promise.resolve();
      });
    }

    expect(screen.getByTestId("tile-j1")).toHaveAttribute(
      "data-poster-recovery-exhausted",
      "false",
    );
    expect(mockListMyJobs).toHaveBeenCalledTimes(1);
    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(5);

    await act(async () => {
      jest.advanceTimersByTime(POSTER_RECOVERY_DELAYS_MS[0]);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(6);
  });

  it("refreshes a missing poster in a loaded page beyond the first 60 rows", async () => {
    jest.useFakeTimers();
    const firstPage = Array.from({ length: 24 }, (_, index) => job(`j${index + 1}`));
    const secondPage = Array.from({ length: 24 }, (_, index) => job(`j${index + 25}`));
    const thirdPage = Array.from({ length: 13 }, (_, index) => job(`j${index + 49}`));
    thirdPage[12] = { ...thirdPage[12], poster_url: null };
    const secondPageCursor = firstPage[23].created_at;
    const thirdPageCursor = secondPage[23].created_at;
    const recoveredTail = {
      id: thirdPage[12].id,
      poster_url: "https://example.test/j61.poster.jpg?sig=fresh",
      poster_identity: thirdPage[12].poster_identity,
      poster_status: "ready",
    };
    mockListMyJobs
      .mockResolvedValueOnce({ jobs: firstPage, next_cursor: secondPageCursor })
      .mockResolvedValueOnce({ jobs: secondPage, next_cursor: thirdPageCursor })
      .mockResolvedValueOnce({ jobs: thirdPage, next_cursor: null });
    mockRefreshMyJobPosters.mockResolvedValueOnce({ jobs: [recoveredTail] });

    renderHome();
    fireEvent.click(
      await screen.findByRole("button", { name: "Load more videos" }),
    );
    expect(await screen.findByTestId("tile-j48")).toBeInTheDocument();
    fireEvent.click(
      await screen.findByRole("button", { name: "Load more videos" }),
    );
    expect(await screen.findByTestId("tile-j61")).toHaveAttribute(
      "data-poster-url",
      "",
    );

    await act(async () => {
      jest.advanceTimersByTime(POSTER_RECOVERY_DELAYS_MS[0]);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mockListMyJobs).toHaveBeenCalledTimes(3);
    expect(mockRefreshMyJobPosters).toHaveBeenCalledWith(
      ["j61"],
      expect.any(AbortSignal),
    );
    expect(screen.getByTestId("tile-j61")).toHaveAttribute(
      "data-poster-url",
      recoveredTail.poster_url,
    );
  });

  it("coalesces poster recovery across loaded pages into one batch request", async () => {
    jest.useFakeTimers();
    const firstPage = Array.from({ length: 24 }, (_, index) => job(`j${index + 1}`));
    const secondPage = Array.from({ length: 24 }, (_, index) => job(`j${index + 25}`));
    const secondPageCursor = firstPage[23].created_at;
    const recoveredFirst = {
      id: firstPage[0].id,
      poster_url: "https://example.test/j1.poster.jpg?sig=fresh",
      poster_identity: firstPage[0].poster_identity,
      poster_status: "ready",
    };
    mockListMyJobs
      .mockResolvedValueOnce({ jobs: firstPage, next_cursor: secondPageCursor })
      .mockResolvedValueOnce({ jobs: secondPage, next_cursor: null });
    mockRefreshMyJobPosters.mockResolvedValueOnce({
      jobs: [
        recoveredFirst,
        {
          id: secondPage[0].id,
          poster_url: "https://example.test/j25.poster.jpg?sig=fresh",
          poster_identity: secondPage[0].poster_identity,
          poster_status: "ready",
        },
      ],
    });

    renderHome();
    fireEvent.click(
      await screen.findByRole("button", { name: "Load more videos" }),
    );
    expect(await screen.findByTestId("tile-j25")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Fail poster j1" }));
    fireEvent.click(screen.getByRole("button", { name: "Fail poster j25" }));
    await act(async () => {
      await Promise.resolve();
    });

    await act(async () => {
      jest.advanceTimersByTime(POSTER_ERROR_REFRESH_DEBOUNCE_MS);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mockListMyJobs).toHaveBeenCalledTimes(2);
    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(1);
    expect(new Set(mockRefreshMyJobPosters.mock.calls[0][0])).toEqual(
      new Set(["j1", "j25"]),
    );
    expect(screen.getByTestId("tile-j1")).toHaveAttribute(
      "data-poster-url",
      recoveredFirst.poster_url,
    );
    expect(screen.getByTestId("tile-j25")).toHaveAttribute(
      "data-poster-url",
      "https://example.test/j25.poster.jpg?sig=fresh",
    );
  });

  it("keeps bounded recovery active until a broken non-null poster actually loads", async () => {
    jest.useFakeTimers();
    const first = {
      ...job("j1"),
      poster_url: "https://example.test/j1.poster.jpg?sig=A",
    };
    const stillBroken = {
      id: first.id,
      poster_url: "https://example.test/j1.poster.jpg?sig=B",
      poster_identity: first.poster_identity,
      poster_status: "ready",
    };
    const recovered = {
      id: first.id,
      poster_url: "https://example.test/j1.poster.jpg?sig=C",
      poster_identity: first.poster_identity,
      poster_status: "ready",
    };
    mockListMyJobs.mockResolvedValueOnce({ jobs: [first], next_cursor: null });
    mockRefreshMyJobPosters
      .mockResolvedValueOnce({ jobs: [stillBroken] })
      .mockResolvedValueOnce({ jobs: [recovered] });

    renderHome();
    expect(await screen.findByTestId("tile-j1")).toHaveAttribute(
      "data-poster-url",
      first.poster_url,
    );
    fireEvent.click(screen.getByRole("button", { name: "Fail poster j1" }));

    await act(async () => {
      jest.advanceTimersByTime(POSTER_ERROR_REFRESH_DEBOUNCE_MS);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTestId("tile-j1")).toHaveAttribute(
      "data-poster-url",
      stillBroken.poster_url,
    );

    fireEvent.click(screen.getByRole("button", { name: "Fail poster j1" }));
    await act(async () => {
      jest.advanceTimersByTime(
        POSTER_RECOVERY_DELAYS_MS[0] - POSTER_ERROR_REFRESH_DEBOUNCE_MS,
      );
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTestId("tile-j1")).toHaveAttribute(
      "data-poster-url",
      recovered.poster_url,
    );

    fireEvent.click(screen.getByRole("button", { name: "Load poster j1" }));
    act(() => jest.advanceTimersByTime(120_000));
    expect(mockListMyJobs).toHaveBeenCalledTimes(1);
    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(2);
  });

  it("bounds transport failures without consuming the confirmed-missing budget", async () => {
    jest.useFakeTimers();
    const missing = { ...job("j1"), poster_url: null };
    mockListMyJobs.mockResolvedValue({ jobs: [missing], next_cursor: null });
    mockRefreshMyJobPosters.mockRejectedValue(new Error("network unavailable"));

    renderHome();
    await act(async () => {
      await Promise.resolve();
    });

    for (const delay of POSTER_REFRESH_TRANSPORT_DELAYS_MS) {
      await act(async () => {
        jest.advanceTimersByTime(delay);
        await Promise.resolve();
        await Promise.resolve();
      });
    }

    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(
      POSTER_REFRESH_TRANSPORT_DELAYS_MS.length,
    );
    expect(screen.getByTestId("tile-j1")).toHaveAttribute(
      "data-poster-recovery-exhausted",
      "false",
    );
    expect(screen.getByTestId("tile-j1")).toHaveAttribute(
      "data-poster-refresh-unavailable",
      "true",
    );

    act(() => jest.advanceTimersByTime(120_000));
    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(
      POSTER_REFRESH_TRANSPORT_DELAYS_MS.length,
    );
  });

  it("aborts a timed-out refresh and ignores a late result from a non-cooperative promise", async () => {
    jest.useFakeTimers();
    const missing = { ...job("j1"), poster_url: null };
    let resolveLate;
    mockListMyJobs.mockResolvedValue({ jobs: [missing], next_cursor: null });
    mockRefreshMyJobPosters
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveLate = resolve;
          }),
      )
      .mockRejectedValue(new Error("network still unavailable"));

    renderHome();
    await act(async () => {
      await Promise.resolve();
    });
    await act(async () => {
      jest.advanceTimersByTime(POSTER_RECOVERY_DELAYS_MS[0]);
      await Promise.resolve();
    });
    const signal = mockRefreshMyJobPosters.mock.calls[0][1];
    expect(signal?.aborted).toBe(false);

    await act(async () => {
      jest.advanceTimersByTime(POSTER_REFRESH_TIMEOUT_MS);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(signal?.aborted).toBe(true);
    expect(screen.getByTestId("tile-j1")).toHaveAttribute("data-poster-url", "");

    await act(async () => {
      resolveLate?.({
        jobs: [
          {
            id: "j1",
            poster_url: "https://example.test/too-late.jpg",
            poster_identity: missing.poster_identity,
            poster_status: "ready",
          },
        ],
      });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTestId("tile-j1")).toHaveAttribute("data-poster-url", "");

    await act(async () => {
      jest.advanceTimersByTime(POSTER_REFRESH_TRANSPORT_DELAYS_MS[1] - 1);
      await Promise.resolve();
    });
    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(1);

    await act(async () => {
      jest.advanceTimersByTime(1);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(2);
  });

  it("caps a batch at the server limit and schedules overflow on the next tick", async () => {
    jest.useFakeTimers();
    const missingJobs = Array.from(
      { length: POSTER_REFRESH_MAX_JOB_IDS + 1 },
      (_, index) => ({ ...job(`j${index + 1}`), poster_url: null }),
    );
    mockListMyJobs.mockResolvedValue({ jobs: missingJobs, next_cursor: null });
    mockRefreshMyJobPosters.mockImplementation(async (jobIds) => ({
      jobs: jobIds.map((jobId) => {
        const item = missingJobs.find((candidate) => candidate.id === jobId);
        return {
          id: jobId,
          poster_url: null,
          poster_identity: item?.poster_identity ?? null,
          poster_status: "repairing",
        };
      }),
    }));

    renderHome();
    await act(async () => {
      await Promise.resolve();
    });
    await act(async () => {
      jest.advanceTimersByTime(POSTER_RECOVERY_DELAYS_MS[0]);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mockRefreshMyJobPosters.mock.calls[0][0]).toHaveLength(
      POSTER_REFRESH_MAX_JOB_IDS,
    );

    await act(async () => {
      jest.advanceTimersByTime(0);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mockRefreshMyJobPosters).toHaveBeenCalledTimes(2);
    expect(mockRefreshMyJobPosters.mock.calls[1][0]).toEqual(["j201"]);
  });

  it("does not poll an explicitly unavailable poster", async () => {
    jest.useFakeTimers();
    mockListMyJobs.mockResolvedValue({
      jobs: [{ ...job("j1"), poster_url: null, poster_status: "unavailable" }],
      next_cursor: null,
    });

    renderHome();
    await act(async () => {
      await Promise.resolve();
    });
    act(() => jest.advanceTimersByTime(120_000));

    expect(mockListMyJobs).toHaveBeenCalledTimes(1);
    expect(mockRefreshMyJobPosters).not.toHaveBeenCalled();
    expect(screen.getByTestId("tile-j1")).toHaveAttribute(
      "data-poster-status",
      "unavailable",
    );
    expect(screen.getByTestId("tile-j1")).toHaveAttribute(
      "data-poster-recovery-exhausted",
      "false",
    );
  });

  it("renders an Integrations section with the TikTok card under the grid (release rails target /plan#tiktok)", async () => {
    const { container } = renderHome();
    expect(
      await screen.findByRole("heading", { name: "Connected accounts" }),
    ).toBeInTheDocument();
    expect(container.querySelector("#tiktok")).toBeInTheDocument();
    expect(screen.getByTestId("tiktok-card")).toBeInTheDocument();
    await waitFor(() => expect(mockListMyJobs).toHaveBeenCalled());
  });

  it("hides the Integrations heading entirely when TikTok isn't available (no empty section)", async () => {
    mockTikTokAvailable = false;
    renderHome();
    await waitFor(() => expect(mockListMyJobs).toHaveBeenCalled());
    expect(
      screen.queryByRole("heading", { name: "Connected accounts" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByTestId("tiktok-card")).not.toBeInTheDocument();
  });
});
