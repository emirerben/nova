import userEvent from "@testing-library/user-event";
import { render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import LibraryTile from "@/components/library/LibraryTile";
import { deleteMyJob, MeApiError, type LibraryJob } from "@/lib/me-api";
import { toast } from "sonner";

jest.mock("@/lib/me-api", () => ({
  deleteMyJob: jest.fn(),
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
const mockToast = toast as unknown as { success: jest.Mock; error: jest.Mock };

beforeEach(() => {
  mockDeleteMyJob.mockReset().mockResolvedValue(undefined);
  mockToast.success.mockReset();
  mockToast.error.mockReset();
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

it("keeps a playable fallback when a ready job has no poster", () => {
  const { container } = render(<LibraryTile job={{ ...baseJob, poster_url: null }} />);

  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("video")).toHaveAttribute("src", baseJob.output_url);
  expect(container.querySelector("video")).not.toHaveAttribute("autoplay");
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
  const user = userEvent.setup();
  const onDeleted = jest.fn();
  render(<LibraryTile job={baseJob} onDeleted={onDeleted} />);

  await user.click(screen.getByRole("button", { name: "More video actions" }));
  await user.click(screen.getByRole("menuitem", { name: "Delete video" }));

  expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  expect(screen.getByText(/You can’t undo this/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Keep video" })).toHaveFocus();

  await user.click(screen.getByRole("button", { name: "Delete video" }));

  await waitFor(() => expect(mockDeleteMyJob).toHaveBeenCalledWith("job-1"));
  expect(onDeleted).toHaveBeenCalledWith("job-1");
});

it("keeps plan footage in the linked-video confirmation copy", async () => {
  const user = userEvent.setup();
  render(<LibraryTile job={{ ...baseJob, content_plan_item_id: "item-1" }} />);

  await user.click(screen.getByRole("button", { name: "More video actions" }));
  await user.click(screen.getByRole("menuitem", { name: "Delete video" }));

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
  const user = userEvent.setup();
  const onDeleted = jest.fn();
  mockDeleteMyJob.mockRejectedValue(new MeApiError("not found", 404));
  render(<LibraryTile job={baseJob} onDeleted={onDeleted} />);

  await user.click(screen.getByRole("button", { name: "More video actions" }));
  await user.click(screen.getByRole("menuitem", { name: "Delete video" }));
  await user.click(screen.getByRole("button", { name: "Delete video" }));

  await waitFor(() => expect(onDeleted).toHaveBeenCalledWith("job-1"));
  expect(mockToast.success).toHaveBeenCalledWith("Video deleted.");
  expect(mockToast.error).not.toHaveBeenCalled();
});

it("keeps the tile and explains a delete conflict", async () => {
  const user = userEvent.setup();
  const onDeleted = jest.fn();
  mockDeleteMyJob.mockRejectedValue(new MeApiError("still processing", 409));
  render(<LibraryTile job={baseJob} onDeleted={onDeleted} />);

  await user.click(screen.getByRole("button", { name: "More video actions" }));
  await user.click(screen.getByRole("menuitem", { name: "Delete video" }));
  await user.click(screen.getByRole("button", { name: "Delete video" }));

  await waitFor(() => expect(mockToast.error).toHaveBeenCalledWith(expect.stringContaining("still being prepared")));
  expect(onDeleted).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "More video actions" })).toBeInTheDocument();
});
