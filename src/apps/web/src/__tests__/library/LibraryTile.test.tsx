import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import LibraryTile from "@/components/library/LibraryTile";
import type { LibraryJob } from "@/lib/me-api";

const baseJob: LibraryJob = {
  id: "job-1",
  mode: "generative",
  status: "ready",
  raw_status: "ready",
  output_url: "https://example.test/video.mp4",
  output_variant_id: "song_text",
  tiktok_publishable: true,
  tiktok_publication: null,
  created_at: "2026-08-01T10:00:00Z",
  content_plan_item_id: null,
  feedback_signal: null,
};

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
