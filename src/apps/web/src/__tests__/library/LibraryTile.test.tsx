import userEvent from "@testing-library/user-event";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import LibraryTile, { PREVIEW_LOAD_TIMEOUT_MS } from "@/components/library/LibraryTile";
import {
  deleteMyJob,
  getMyJobPlaybackUrl,
  MeApiError,
  type LibraryJob,
} from "@/lib/me-api";
import { toast } from "sonner";

jest.mock("@/lib/me-api", () => ({
  deleteMyJob: jest.fn(),
  getMyJobPlaybackUrl: jest.fn(),
  MeApiError: class MeApiError extends Error {
    status: number;

    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  },
}));

jest.mock("sonner", () => ({
  toast: { success: jest.fn(), error: jest.fn() },
}));

const baseJob: LibraryJob = {
  id: "job-1",
  mode: "generative",
  status: "ready",
  raw_status: "ready",
  output_url: "https://example.test/video.mp4",
  poster_url: "https://example.test/video.mp4.poster.jpg",
  poster_identity: "song_text:2026-08-01T10:00:00Z",
  output_variant_id: "song_text",
  tiktok_publishable: true,
  tiktok_publication: null,
  created_at: "2026-08-01T10:00:00Z",
  content_plan_item_id: null,
  feedback_signal: null,
};

const mockDeleteMyJob = deleteMyJob as jest.MockedFunction<typeof deleteMyJob>;
const mockGetMyJobPlaybackUrl = getMyJobPlaybackUrl as jest.MockedFunction<
  typeof getMyJobPlaybackUrl
>;
const mockToast = toast as unknown as { success: jest.Mock; error: jest.Mock };
let playSpy: jest.SpyInstance<Promise<void>, []>;
let pauseSpy: jest.SpyInstance<void, []>;

beforeEach(() => {
  playSpy = jest
    .spyOn(window.HTMLMediaElement.prototype, "play")
    .mockResolvedValue(undefined);
  pauseSpy = jest.spyOn(window.HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  mockDeleteMyJob.mockReset().mockResolvedValue(undefined);
  mockGetMyJobPlaybackUrl.mockReset().mockResolvedValue({
    video_url: "https://example.test/video.mp4?sig=fresh",
  });
  mockToast.success.mockReset();
  mockToast.error.mockReset();
});

afterEach(() => {
  jest.useRealTimers();
  playSpy.mockRestore();
  pauseSpy.mockRestore();
});

it("tells the creator their TikTok upload is waiting in the app inbox, not TikTok's Drafts tab", () => {
  const job: LibraryJob = {
    ...baseJob,
    tiktok_publication: {
      id: "publication-1",
      job_id: "job-1",
      variant_id: "song_text",
      delivery_mode: "draft_upload",
      processing_status: "complete",
      visibility_status: "draft",
      retryable: false,
      deletion_blocked: false,
      failure_code: null,
      failure_detail: null,
      latest_metrics: null,
      metrics_synced_at: null,
      created_at: "2026-08-01T10:00:00Z",
      updated_at: "2026-08-01T10:00:00Z",
    },
  };

  render(<LibraryTile job={job} />);

  expect(screen.getByText("Waiting in your TikTok app inbox")).not.toBeNull();
  expect(screen.getByText(/Open the TikTok app/)).not.toBeNull();
  expect(screen.queryByText(/Ready to finish in TikTok/)).toBeNull();
  expect(screen.queryAllByText(/TikTok drafts/i)).toHaveLength(0);
});

it("does not claim a live public post is waiting in the inbox", () => {
  const job: LibraryJob = {
    ...baseJob,
    tiktok_publication: {
      id: "publication-2",
      job_id: "job-1",
      variant_id: "song_text",
      delivery_mode: "direct_post",
      processing_status: "complete",
      visibility_status: "public",
      retryable: false,
      deletion_blocked: false,
      failure_code: null,
      failure_detail: null,
      latest_metrics: null,
      metrics_synced_at: null,
      created_at: "2026-08-01T10:00:00Z",
      updated_at: "2026-08-01T10:00:00Z",
    },
  };

  render(<LibraryTile job={job} />);

  expect(screen.getByText("Live on TikTok")).not.toBeNull();
  expect(screen.queryByText("Waiting in your TikTok app inbox")).toBeNull();
});

it("uses structured failure taxonomy without exposing raw worker status", () => {
  const job: LibraryJob = {
    ...baseJob,
    status: "failed",
    raw_status: "variants_failed",
    failure_reason: "processing_timeout",
    output_url: null,
  };

  render(<LibraryTile job={job} />);

  expect(screen.getByText("The render took too long")).not.toBeNull();
  expect(screen.queryByText(/variants_failed/)).toBeNull();
});

it("wraps a pinned video in a single Open link to its plan item — no other controls", () => {
  const job: LibraryJob = { ...baseJob, content_plan_item_id: "item-42" };

  render(<LibraryTile job={job} />);

  const links = screen.getAllByRole("link");
  expect(links).toHaveLength(1);
  expect(links[0]).toHaveAttribute("href", "/plan/items/item-42");
  expect(screen.getByText("Open")).toBeInTheDocument();

  // Old per-tile action row is gone entirely — those live on the item page now.
  expect(screen.queryByRole("button", { name: /download/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /publish to tiktok/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /add to plan/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("group", { name: /rate this video/i })).not.toBeInTheDocument();
});

it("renders status only, with no Open link, for a job that isn't pinned to a plan item", () => {
  const job: LibraryJob = { ...baseJob, content_plan_item_id: null };

  render(<LibraryTile job={job} />);

  expect(screen.queryByRole("link")).not.toBeInTheDocument();
  expect(screen.queryByText("Open")).not.toBeInTheDocument();
});

it("shows a Ready-to-post badge on a finished video", () => {
  const job: LibraryJob = { ...baseJob, content_plan_item_id: "item-1" };

  render(<LibraryTile job={job} />);

  expect(screen.getByText("Ready to post")).toBeInTheDocument();
});

it("renders the durable poster in the library tile without loading the MP4", () => {
  const { container } = render(<LibraryTile job={baseJob} />);

  expect(screen.getByRole("img", { name: "Your video" })).toHaveAttribute(
    "src",
    baseJob.poster_url,
  );
  expect(container.querySelector("video")).toBeNull();
});

it("keeps a posterless tile visible without eagerly loading its MP4", () => {
  const { container } = render(<LibraryTile job={{ ...baseJob, poster_url: null }} />);

  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("video[src]")).toBeNull();
  expect(screen.getByRole("button", { name: "Play preview" })).toBeInTheDocument();
  expect(mockGetMyJobPlaybackUrl).not.toHaveBeenCalled();
});

it("does not issue grid-wide MP4 requests for multiple posterless tiles", () => {
  const { container } = render(
    <>
      <LibraryTile job={{ ...baseJob, id: "job-1", poster_url: null }} />
      <LibraryTile job={{ ...baseJob, id: "job-2", poster_url: null }} />
      <LibraryTile job={{ ...baseJob, id: "job-3", poster_url: null }} />
    </>,
  );

  expect(container.querySelectorAll("video[src]")).toHaveLength(0);
  expect(screen.getAllByRole("button", { name: "Play preview" })).toHaveLength(3);
  expect(mockGetMyJobPlaybackUrl).not.toHaveBeenCalled();
});

it("loads only the selected posterless preview after explicit interaction", async () => {
  const user = userEvent.setup();
  const selectedListUrl = "https://example.test/video-2.mp4?sig=list-stale";
  const selectedFreshUrl = "https://example.test/job-2.mp4?sig=click-fresh";
  mockGetMyJobPlaybackUrl.mockImplementation(async (jobId) => ({
    video_url: `https://example.test/${jobId}.mp4?sig=${jobId === "job-2" ? "click-fresh" : "unused"}`,
  }));
  const { container } = render(
    <>
      <LibraryTile
        job={{ ...baseJob, id: "job-1", output_url: "https://example.test/video-1.mp4", poster_url: null }}
      />
      <LibraryTile
        job={{ ...baseJob, id: "job-2", output_url: selectedListUrl, poster_url: null }}
      />
      <LibraryTile
        job={{ ...baseJob, id: "job-3", output_url: "https://example.test/video-3.mp4", poster_url: null }}
      />
    </>,
  );

  await user.click(screen.getAllByRole("button", { name: "Play preview" })[1]);

  const videos = container.querySelectorAll("video[src]");
  expect(videos).toHaveLength(1);
  expect(videos[0]).toHaveAttribute("src", selectedFreshUrl);
  expect(videos[0]).not.toHaveAttribute("src", selectedListUrl);
  expect(videos[0]).toHaveAttribute("preload", "metadata");
  expect(mockGetMyJobPlaybackUrl).toHaveBeenCalledTimes(1);
  expect(mockGetMyJobPlaybackUrl).toHaveBeenCalledWith("job-2", expect.any(AbortSignal));
  expect(playSpy).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("status")).toHaveTextContent("Loading preview…");
  expect(screen.getByRole("button", { name: "Stop preview" })).toHaveFocus();

  fireEvent.playing(videos[0]);

  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

it("stops an active preview, releases the video, and restores trigger focus", async () => {
  const user = userEvent.setup();
  const { container } = render(<LibraryTile job={{ ...baseJob, poster_url: null }} />);

  await user.click(screen.getByRole("button", { name: "Play preview" }));
  const stopButton = screen.getByRole("button", { name: "Stop preview" });
  expect(stopButton).toHaveFocus();

  await user.click(stopButton);

  expect(pauseSpy).toHaveBeenCalledTimes(1);
  expect(container.querySelector("video[src]")).toBeNull();
  expect(screen.getByRole("button", { name: "Play preview" })).toHaveFocus();
});

it("preserves active playback when poster recovery completes, then shows the poster after stop", async () => {
  const user = userEvent.setup();
  const posterlessJob: LibraryJob = {
    ...baseJob,
    poster_url: null,
    poster_status: "repairing",
  };
  const recoveredPosterUrl = "https://example.test/video.poster.jpg?sig=recovered";
  const { container, rerender } = render(<LibraryTile job={posterlessJob} />);

  await user.click(screen.getByRole("button", { name: "Play preview" }));
  const activeVideo = container.querySelector("video[src]") as HTMLVideoElement;
  fireEvent.playing(activeVideo);

  rerender(
    <LibraryTile
      job={{
        ...posterlessJob,
        poster_url: recoveredPosterUrl,
        poster_identity: "song_text:2026-08-28T10:00:00Z",
        poster_status: "ready",
      }}
    />,
  );

  expect(container.querySelector("video[src]")).toBe(activeVideo);
  expect(screen.queryByRole("img", { name: "Your video" })).not.toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "Stop preview" }));

  expect(container.querySelector("video[src]")).toBeNull();
  expect(screen.getByRole("img", { name: "Your video" })).toHaveAttribute(
    "src",
    recoveredPosterUrl,
  );
});

it("keeps at most one posterless tile decoding after another preview is selected", async () => {
  const user = userEvent.setup();
  mockGetMyJobPlaybackUrl.mockImplementation(async (jobId) => ({
    video_url: `https://example.test/${jobId}.mp4?sig=fresh`,
  }));
  const { container } = render(
    <>
      <LibraryTile
        job={{ ...baseJob, id: "job-1", output_url: "https://example.test/video-1.mp4", poster_url: null }}
      />
      <LibraryTile
        job={{ ...baseJob, id: "job-2", output_url: "https://example.test/video-2.mp4", poster_url: null }}
      />
    </>,
  );

  await user.click(screen.getAllByRole("button", { name: "Play preview" })[0]);
  await user.click(screen.getAllByRole("button", { name: "Play preview" })[0]);

  const videos = container.querySelectorAll("video[src]");
  expect(videos).toHaveLength(1);
  expect(videos[0]).toHaveAttribute("src", "https://example.test/job-2.mp4?sig=fresh");
  expect(pauseSpy).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "Stop preview" })).toHaveFocus();
});

it("ignores an older pending refresh after another tile is selected", async () => {
  let resolveFirst: ((value: { video_url: string }) => void) | undefined;
  const firstRefresh = new Promise<{ video_url: string }>((resolve) => {
    resolveFirst = resolve;
  });
  mockGetMyJobPlaybackUrl.mockImplementation((jobId) =>
    jobId === "job-1"
      ? firstRefresh
      : Promise.resolve({ video_url: "https://example.test/job-2.mp4?sig=fresh" }),
  );
  const user = userEvent.setup();
  const { container } = render(
    <>
      <LibraryTile job={{ ...baseJob, id: "job-1", poster_url: null }} />
      <LibraryTile job={{ ...baseJob, id: "job-2", poster_url: null }} />
    </>,
  );

  await user.click(screen.getAllByRole("button", { name: "Play preview" })[0]);
  expect(screen.getByRole("button", { name: "Loading preview" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Play preview" }));

  await waitFor(() => {
    expect(container.querySelectorAll("video[src]")).toHaveLength(1);
  });
  expect(container.querySelector("video[src]")).toHaveAttribute(
    "src",
    "https://example.test/job-2.mp4?sig=fresh",
  );

  await act(async () => {
    resolveFirst?.({ video_url: "https://example.test/job-1.mp4?sig=late" });
    await firstRefresh;
  });

  expect(container.querySelectorAll("video[src]")).toHaveLength(1);
  expect(container.querySelector("video[src]")).toHaveAttribute(
    "src",
    "https://example.test/job-2.mp4?sig=fresh",
  );
});

it("uses a direct gesture without refetching when Safari blocks delayed autoplay", async () => {
  const firstFreshUrl = "https://example.test/video.mp4?sig=first";
  mockGetMyJobPlaybackUrl.mockResolvedValueOnce({ video_url: firstFreshUrl });
  playSpy.mockRejectedValueOnce(new DOMException("Autoplay blocked", "NotAllowedError"));
  const user = userEvent.setup();
  const { container } = render(<LibraryTile job={{ ...baseJob, poster_url: null }} />);

  await user.click(screen.getByRole("button", { name: "Play preview" }));

  const directPlay = await screen.findByRole("button", { name: "Tap to play preview" });
  const video = container.querySelector("video[src]") as HTMLVideoElement;
  expect(directPlay).toHaveFocus();
  expect(video).toHaveAttribute("src", firstFreshUrl);
  expect(mockGetMyJobPlaybackUrl).toHaveBeenCalledTimes(1);
  expect(playSpy).toHaveBeenCalledTimes(1);

  await user.click(directPlay);

  expect(container.querySelector("video[src]")).toBe(video);
  expect(mockGetMyJobPlaybackUrl).toHaveBeenCalledTimes(1);
  expect(playSpy).toHaveBeenCalledTimes(2);
  fireEvent.playing(video);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Tap to play preview" })).not.toBeInTheDocument();
});

it("never retries an expired list or prior-attempt URL after the clock advances", async () => {
  jest.useFakeTimers();
  jest.setSystemTime(new Date("2026-08-28T10:00:00Z"));
  const expiredListUrl =
    "https://example.test/video.mp4?X-Goog-Date=20260828T090000Z&X-Goog-Expires=60&sig=list";
  const firstAttemptUrl =
    "https://example.test/video.mp4?X-Goog-Date=20260828T100000Z&X-Goog-Expires=60&sig=first";
  const retryUrl =
    "https://example.test/video.mp4?X-Goog-Date=20260828T100200Z&X-Goog-Expires=60&sig=retry";
  mockGetMyJobPlaybackUrl
    .mockResolvedValueOnce({ video_url: firstAttemptUrl })
    .mockResolvedValueOnce({ video_url: retryUrl });
  const { container } = render(
    <LibraryTile job={{ ...baseJob, output_url: expiredListUrl, poster_url: null }} />,
  );

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Play preview" }));
    await Promise.resolve();
  });
  const firstVideo = container.querySelector("video[src]") as HTMLVideoElement;
  expect(firstVideo).toHaveAttribute("src", firstAttemptUrl);
  expect(firstVideo).not.toHaveAttribute("src", expiredListUrl);
  fireEvent.error(firstVideo);

  act(() => jest.setSystemTime(new Date("2026-08-28T10:02:00Z")));
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Retry preview" }));
    await Promise.resolve();
  });

  const retryVideo = container.querySelector("video[src]") as HTMLVideoElement;
  expect(retryVideo).toHaveAttribute("src", retryUrl);
  expect(retryVideo).not.toHaveAttribute("src", expiredListUrl);
  expect(retryVideo).not.toHaveAttribute("src", firstAttemptUrl);
  expect(mockGetMyJobPlaybackUrl).toHaveBeenCalledTimes(2);
});

it("can play a ready posterless job even when list-time URL signing failed", async () => {
  const user = userEvent.setup();
  const { container } = render(
    <LibraryTile job={{ ...baseJob, output_url: null, poster_url: null }} />,
  );

  await user.click(screen.getByRole("button", { name: "Play preview" }));

  expect(mockGetMyJobPlaybackUrl).toHaveBeenCalledWith(
    baseJob.id,
    expect.any(AbortSignal),
  );
  expect(container.querySelector("video[src]")).toHaveAttribute(
    "src",
    "https://example.test/video.mp4?sig=fresh",
  );
});

it("restores focus and offers a fresh Retry when playback URL refresh fails", async () => {
  mockGetMyJobPlaybackUrl
    .mockRejectedValueOnce(new MeApiError("Could not sign playback URL", 503))
    .mockResolvedValueOnce({ video_url: "https://example.test/video.mp4?sig=recovered" });
  const user = userEvent.setup();
  const { container } = render(<LibraryTile job={{ ...baseJob, poster_url: null }} />);

  await user.click(screen.getByRole("button", { name: "Play preview" }));

  const retry = await screen.findByRole("button", { name: "Retry preview" });
  expect(retry).toHaveFocus();
  expect(container.querySelector("video[src]")).toBeNull();

  await user.click(retry);

  expect(mockGetMyJobPlaybackUrl).toHaveBeenCalledTimes(2);
  expect(container.querySelector("video[src]")).toHaveAttribute(
    "src",
    "https://example.test/video.mp4?sig=recovered",
  );
  expect(screen.getByRole("button", { name: "Stop preview" })).toHaveFocus();
});

it("allows narrow mobile failure copy to wrap inside the tile", async () => {
  playSpy.mockRejectedValueOnce(new DOMException("Playback failed", "NotSupportedError"));
  const user = userEvent.setup();
  render(<LibraryTile job={{ ...baseJob, poster_url: null }} />);

  await user.click(screen.getByRole("button", { name: "Play preview" }));
  const retry = await screen.findByRole("button", { name: "Retry preview" });
  const failure = screen.getByRole("status");

  expect(retry).toHaveClass("whitespace-normal");
  expect(failure).toHaveClass("whitespace-normal");
});

it("returns to a retryable placeholder when preview loading stalls", async () => {
  jest.useFakeTimers();
  playSpy.mockReturnValueOnce(new Promise(() => undefined));
  const { container } = render(<LibraryTile job={{ ...baseJob, poster_url: null }} />);

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Play preview" }));
    await Promise.resolve();
  });

  expect(container.querySelector("video[src]")).not.toBeNull();
  expect(screen.getByRole("status")).toHaveTextContent("Loading preview…");

  act(() => jest.advanceTimersByTime(PREVIEW_LOAD_TIMEOUT_MS));

  expect(screen.getByRole("button", { name: "Retry preview" })).toBeInTheDocument();
  expect(container.querySelector("video[src]")).toBeNull();
});

it("times out a hanging playback URL refresh and ignores its late result", async () => {
  jest.useFakeTimers();
  let resolveRefresh: ((value: { video_url: string }) => void) | undefined;
  let receivedSignal: AbortSignal | undefined;
  const pendingRefresh = new Promise<{ video_url: string }>((resolve) => {
    resolveRefresh = resolve;
  });
  mockGetMyJobPlaybackUrl.mockImplementation((_jobId, signal) => {
    receivedSignal = signal;
    return pendingRefresh;
  });
  const { container } = render(<LibraryTile job={{ ...baseJob, poster_url: null }} />);

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Play preview" }));
    await Promise.resolve();
  });
  expect(screen.getByRole("button", { name: "Loading preview" })).toBeDisabled();
  expect(receivedSignal?.aborted).toBe(false);

  act(() => jest.advanceTimersByTime(PREVIEW_LOAD_TIMEOUT_MS));

  expect(receivedSignal?.aborted).toBe(true);
  expect(screen.getByRole("button", { name: "Retry preview" })).toHaveFocus();
  expect(container.querySelector("video[src]")).toBeNull();

  await act(async () => {
    resolveRefresh?.({ video_url: "https://example.test/video.mp4?sig=too-late" });
    await pendingRefresh;
  });

  expect(container.querySelector("video[src]")).toBeNull();
  expect(screen.getByRole("button", { name: "Retry preview" })).toBeInTheDocument();
});

it("recovers from a playing preview wait and clears the stale failure timer", async () => {
  jest.useFakeTimers();
  const { container } = render(<LibraryTile job={{ ...baseJob, poster_url: null }} />);

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Play preview" }));
    await Promise.resolve();
  });
  const video = container.querySelector("video[src]") as HTMLVideoElement;
  fireEvent.playing(video);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();

  fireEvent.waiting(video);
  expect(screen.getByRole("status")).toHaveTextContent("Loading preview…");
  act(() => jest.advanceTimersByTime(PREVIEW_LOAD_TIMEOUT_MS / 2));
  fireEvent.playing(video);
  act(() => jest.advanceTimersByTime(PREVIEW_LOAD_TIMEOUT_MS));

  expect(container.querySelector("video[src]")).toBe(video);
  expect(screen.queryByRole("button", { name: "Retry preview" })).not.toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

it("makes a previously playing preview retryable after sustained buffering", async () => {
  jest.useFakeTimers();
  const { container } = render(<LibraryTile job={{ ...baseJob, poster_url: null }} />);

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Play preview" }));
    await Promise.resolve();
  });
  const video = container.querySelector("video[src]") as HTMLVideoElement;
  fireEvent.playing(video);
  fireEvent.waiting(video);
  act(() => jest.advanceTimersByTime(PREVIEW_LOAD_TIMEOUT_MS));

  expect(container.querySelector("video[src]")).toBeNull();
  expect(screen.getByRole("button", { name: "Retry preview" })).toBeInTheDocument();
});

it("does not tear down buffered Safari playback on a stalled fetch event", async () => {
  jest.useFakeTimers();
  const { container } = render(<LibraryTile job={{ ...baseJob, poster_url: null }} />);

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Play preview" }));
    await Promise.resolve();
  });
  const video = container.querySelector("video[src]") as HTMLVideoElement;
  fireEvent.playing(video);
  fireEvent.stalled(video);
  act(() => jest.advanceTimersByTime(PREVIEW_LOAD_TIMEOUT_MS));

  expect(container.querySelector("video[src]")).toBe(video);
  expect(screen.queryByRole("button", { name: "Retry preview" })).not.toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

it("keeps a pinned posterless tile as one link without an inline media request", () => {
  const { container } = render(
    <LibraryTile
      job={{ ...baseJob, poster_url: null, content_plan_item_id: "item-42" }}
    />,
  );

  const links = screen.getAllByRole("link");
  expect(links).toHaveLength(1);
  expect(links[0]).toHaveAttribute("href", "/plan/items/item-42");
  expect(screen.getAllByText("Preparing preview…")).not.toHaveLength(0);
  expect(screen.queryByText("Open video")).not.toBeInTheDocument();
  expect(container.querySelector("video[src]")).toBeNull();
  expect(mockGetMyJobPlaybackUrl).not.toHaveBeenCalled();
});

it("reports one poster image failure and keeps explicit tap-to-play fallback", () => {
  const onPosterLoadError = jest.fn();
  const { container } = render(
    <LibraryTile job={baseJob} onPosterLoadError={onPosterLoadError} />,
  );

  fireEvent.error(screen.getByRole("img", { name: "Your video" }));

  expect(onPosterLoadError).toHaveBeenCalledTimes(1);
  expect(onPosterLoadError).toHaveBeenCalledWith(
    baseJob.id,
    baseJob.poster_identity,
  );
  expect(screen.getByText("Thumbnail unavailable. Tap to play.")).toBeInTheDocument();
  expect(screen.getByText("Ready to post")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Play preview" })).toBeInTheDocument();
  expect(container.querySelector("video[src]")).toBeNull();
});

it("reports the stable poster identity after the thumbnail loads", () => {
  const onPosterLoadSuccess = jest.fn();
  render(
    <LibraryTile job={baseJob} onPosterLoadSuccess={onPosterLoadSuccess} />,
  );

  fireEvent.load(screen.getByRole("img", { name: "Your video" }));

  expect(onPosterLoadSuccess).toHaveBeenCalledWith(
    baseJob.id,
    baseJob.poster_identity,
  );
});

it("keeps an explicitly unavailable poster tap-to-play without fetching video bytes", () => {
  const { container } = render(
    <LibraryTile
      job={{ ...baseJob, poster_url: null, poster_status: "unavailable" }}
    />,
  );

  expect(screen.getByText("Thumbnail unavailable. Tap to play.")).toBeInTheDocument();
  expect(screen.getByText("Ready to post")).toBeInTheDocument();
  expect(screen.queryByText("Open video")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Play preview" })).toBeInTheDocument();
  expect(container.querySelector("video[src]")).toBeNull();
  expect(mockGetMyJobPlaybackUrl).not.toHaveBeenCalled();
});

it("keeps an explicitly unavailable pinned poster as a static editor link", () => {
  const { container } = render(
    <LibraryTile
      job={{
        ...baseJob,
        poster_url: null,
        poster_status: "unavailable",
        content_plan_item_id: "item-42",
      }}
    />,
  );

  expect(screen.getByText("Thumbnail unavailable")).toBeInTheDocument();
  expect(screen.getByText("Ready to post")).toBeInTheDocument();
  expect(screen.getAllByRole("link")).toHaveLength(1);
  expect(screen.queryByText("Open video")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Play preview" })).not.toBeInTheDocument();
  expect(container.querySelector("video[src]")).toBeNull();
  expect(mockGetMyJobPlaybackUrl).not.toHaveBeenCalled();
});

it("does not declare a thumbnail dead just because client retries ran out", () => {
  // Exhausting the recovery ladder only means "we stopped asking". It can only
  // happen while the server's last answer was `repairing`, and a queued repair
  // routinely outlives the ladder — so stating the terminal verdict here would
  // report a failure the server never gave (DESIGN.md §7 D19).
  const { container } = render(
    <LibraryTile
      job={{ ...baseJob, poster_url: null, poster_status: "repairing" }}
      posterRecoveryExhausted
    />,
  );

  expect(screen.queryByText("Thumbnail unavailable. Tap to play.")).not.toBeInTheDocument();
  expect(screen.getByText("Ready to post")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Play preview" })).toBeInTheDocument();
  expect(container.querySelector("video[src]")).toBeNull();
  expect(mockGetMyJobPlaybackUrl).not.toHaveBeenCalled();
});

it("settles a posterless tile only on the server's terminal verdict", () => {
  const { container } = render(
    <LibraryTile
      job={{ ...baseJob, poster_url: null, poster_status: "unavailable" }}
      posterRecoveryExhausted
    />,
  );

  expect(screen.getByText("Thumbnail unavailable. Tap to play.")).toBeInTheDocument();
  expect(screen.getByText("Ready to post")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Play preview" })).toBeInTheDocument();
  expect(container.querySelector("video[src]")).toBeNull();
  expect(mockGetMyJobPlaybackUrl).not.toHaveBeenCalled();
});

it("distinguishes a temporary poster refresh outage from confirmed exhaustion", () => {
  const { container } = render(
    <LibraryTile
      job={{ ...baseJob, poster_url: null, poster_status: "repairing" }}
      posterRefreshUnavailable
    />,
  );

  expect(
    screen.getByText("Thumbnail temporarily unavailable. Tap to play."),
  ).toBeInTheDocument();
  expect(screen.queryByText("Thumbnail unavailable. Tap to play.")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Play preview" })).toBeInTheDocument();
  expect(container.querySelector("video[src]")).toBeNull();
  expect(mockGetMyJobPlaybackUrl).not.toHaveBeenCalled();
});

it("keeps a nonblank retry state when an activated fallback video fails", async () => {
  const user = userEvent.setup();
  const { container } = render(<LibraryTile job={{ ...baseJob, poster_url: null }} />);

  await user.click(screen.getByRole("button", { name: "Play preview" }));
  fireEvent.error(container.querySelector("video") as HTMLVideoElement);

  expect(screen.getByText("Preview unavailable. You can try again.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Retry preview" })).toBeInTheDocument();
  expect(container.querySelector("video[src]")).toBeNull();
});

it("shows a Rendering badge while a video is still processing", () => {
  const job: LibraryJob = {
    ...baseJob,
    status: "generating",
    output_url: null,
    content_plan_item_id: "item-1",
  };

  render(<LibraryTile job={job} />);

  expect(screen.getByText("Rendering…")).toBeInTheDocument();
});

it("offers a destructive delete flow and removes the tile after success", async () => {
  const onDeleted = jest.fn();
  render(<LibraryTile job={baseJob} onDeleted={onDeleted} />);

  fireEvent.keyDown(screen.getByRole("button", { name: "More video actions" }), { key: "Enter" });
  fireEvent.click(await screen.findByRole("menuitem", { name: "Delete video" }));

  expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  expect(screen.getByText(/You can’t undo this/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Keep video" })).toHaveFocus();

  fireEvent.click(screen.getByRole("button", { name: "Delete video" }));

  await waitFor(() => expect(mockDeleteMyJob).toHaveBeenCalledWith("job-1"));
  expect(onDeleted).toHaveBeenCalledWith("job-1");
});

it("keeps plan footage in the linked-video confirmation copy", async () => {
  render(<LibraryTile job={{ ...baseJob, content_plan_item_id: "item-1" }} />);

  fireEvent.keyDown(screen.getByRole("button", { name: "More video actions" }), { key: "Enter" });
  fireEvent.click(await screen.findByRole("menuitem", { name: "Delete video" }));

  expect(screen.getByText(/edit plan and uploaded footage will stay available/)).toBeInTheDocument();
  expect(screen.queryByText(/You can’t undo this/)).not.toBeInTheDocument();
});

it("does not offer deletion while TikTok is still processing", () => {
  render(
    <LibraryTile
      job={{
        ...baseJob,
        tiktok_publication: {
          id: "publication-active",
          job_id: "job-1",
          variant_id: "song_text",
          delivery_mode: "direct_post",
          processing_status: "processing",
          visibility_status: "unknown",
          retryable: false,
          deletion_blocked: true,
          failure_code: null,
          failure_detail: null,
          latest_metrics: null,
          metrics_synced_at: null,
          created_at: "2026-08-01T10:00:00Z",
          updated_at: "2026-08-01T10:00:00Z",
        },
      }}
    />,
  );

  expect(screen.queryByRole("button", { name: "More video actions" })).not.toBeInTheDocument();
});

it("reconciles a stale 404 as a successful delete", async () => {
  const onDeleted = jest.fn();
  mockDeleteMyJob.mockRejectedValue(new MeApiError("not found", 404));
  render(<LibraryTile job={baseJob} onDeleted={onDeleted} />);

  fireEvent.keyDown(screen.getByRole("button", { name: "More video actions" }), { key: "Enter" });
  fireEvent.click(await screen.findByRole("menuitem", { name: "Delete video" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete video" }));

  await waitFor(() => expect(onDeleted).toHaveBeenCalledWith("job-1"));
  expect(mockToast.success).toHaveBeenCalledWith("Video deleted.");
  expect(mockToast.error).not.toHaveBeenCalled();
});

it("keeps the tile and explains a delete conflict", async () => {
  const onDeleted = jest.fn();
  mockDeleteMyJob.mockRejectedValue(new MeApiError("still processing", 409));
  render(<LibraryTile job={baseJob} onDeleted={onDeleted} />);

  fireEvent.keyDown(screen.getByRole("button", { name: "More video actions" }), { key: "Enter" });
  fireEvent.click(await screen.findByRole("menuitem", { name: "Delete video" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete video" }));

  await waitFor(() => expect(mockToast.error).toHaveBeenCalledWith(expect.stringContaining("still being prepared")));
  expect(onDeleted).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "More video actions" })).toBeInTheDocument();
});
