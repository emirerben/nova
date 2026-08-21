import { render, screen } from "@testing-library/react";
import LibraryTile from "@/app/library/_components/LibraryTile";
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

  render(
    <LibraryTile job={job} plan={null} onPinned={jest.fn()} canPublishToTikTok />,
  );

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

  render(
    <LibraryTile job={job} plan={null} onPinned={jest.fn()} canPublishToTikTok />,
  );

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

  render(
    <LibraryTile job={job} plan={null} onPinned={jest.fn()} canPublishToTikTok={false} />,
  );

  expect(screen.getByText("The render took too long")).not.toBeNull();
  expect(screen.queryByText(/variants_failed/)).toBeNull();
});
