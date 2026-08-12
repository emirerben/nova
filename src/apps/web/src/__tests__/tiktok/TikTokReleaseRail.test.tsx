import { fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { TikTokReleaseRail } from "@/components/TikTokReleaseRail";
import type { TikTokConnection, TikTokPublication } from "@/lib/tiktok-api";

const connection: TikTokConnection = {
  available: true,
  connected: true,
  status: "connected",
  account: { display_name: "Kria Studio", avatar_url: "https://example.test/avatar.jpg" },
  granted_scopes: ["video.publish"],
  can_publish: true,
  can_upload_draft: true,
  can_analyze: true,
  audited: true,
  beta: false,
  last_synced_at: "2026-08-01T00:00:00Z",
  learned_post_count: 4,
};

const basePublication: TikTokPublication = {
  id: "publication-1",
  job_id: "job-1",
  variant_id: "song_text",
  title: "A precise caption #topic",
  privacy_level: "PUBLIC_TO_EVERYONE",
  allow_comment: true,
  allow_duet: false,
  allow_stitch: true,
  creator_nickname: "Kria Studio",
  processing_status: "processing",
  visibility_status: "unknown",
  public_at: null,
  retryable: false,
  failure_code: null,
  failure_detail: null,
  latest_metrics: null,
  metrics_synced_at: null,
  evaluation_metrics: null,
  evaluation_captured_at: null,
  created_at: "2026-08-01T10:00:00Z",
  updated_at: "2026-08-01T10:00:00Z",
};

function renderRail(
  publication: TikTokPublication | null,
  publications = publication ? [publication] : [],
  overrides: Partial<ComponentProps<typeof TikTokReleaseRail>> = {},
) {
  return render(
    <TikTokReleaseRail
      connection={connection}
      publication={publication}
      publications={publications}
      canPublish
      baking={false}
      editHref="/edit/job-1"
      durationSeconds={18}
      variantLabel="Original text"
      onPublish={jest.fn()}
      onDownload={jest.fn()}
      {...overrides}
    />,
  );
}

it("keeps the exact publishing receipt visible while TikTok processes the post", () => {
  renderRail(basePublication);

  expect(screen.getByText("Sending to TikTok")).not.toBeNull();
  expect(screen.getByText("A precise caption #topic")).not.toBeNull();
  expect(screen.getByText("Public")).not.toBeNull();
  expect(screen.getByText("Comments on · Duet off · Stitch on")).not.toBeNull();
  expect(screen.getByText(/Submitted Aug 1/)).not.toBeNull();
});

it("labels a simulated receipt as local instead of claiming TikTok is processing it", () => {
  renderRail(basePublication, [basePublication], { simulation: true });

  expect(screen.getByText("Publish simulation complete")).not.toBeNull();
  expect(screen.getByText("This is a local connected-state preview. Nothing was sent to TikTok.")).not.toBeNull();
  expect(screen.getByText(/Simulated Aug 1/)).not.toBeNull();
  expect(screen.getByRole("button", { name: "Preview history (1)" })).not.toBeNull();
  expect(screen.queryByText("Sending to TikTok")).toBeNull();
});

it("offers publishing only for a connected account with permission", () => {
  const onPublish = jest.fn();
  const { rerender } = renderRail(null, [], { onPublish });

  fireEvent.click(screen.getByRole("button", { name: "Publish to TikTok" }));
  expect(onPublish).toHaveBeenCalledTimes(1);

  rerender(
    <TikTokReleaseRail
      connection={{ ...connection, connected: false, can_publish: false, account: null }}
      publication={null}
      publications={[]}
      canPublish={false}
      baking={false}
      editHref="/edit/job-1"
      durationSeconds={18}
      variantLabel="Original text"
      onPublish={onPublish}
      onDownload={jest.fn()}
    />,
  );
  expect(screen.getByText("Connect TikTok before publishing.")).not.toBeNull();
  expect(screen.getByRole("link", { name: "Connect TikTok" }).getAttribute("href")).toBe("/library");
  expect(screen.queryByRole("button", { name: "Publish to TikTok" })).toBeNull();

  rerender(
    <TikTokReleaseRail
      connection={{ ...connection, can_publish: false, can_upload_draft: false }}
      publication={null}
      publications={[]}
      canPublish={false}
      baking={false}
      editHref="/edit/job-1"
      durationSeconds={18}
      variantLabel="Original text"
      onPublish={onPublish}
      onDownload={jest.fn()}
    />,
  );
  expect(screen.getByText("TikTok publishing access needs to be reconnected.")).not.toBeNull();
  expect(screen.getByRole("link", { name: "Reconnect TikTok" }).getAttribute("href")).toBe("/library");
});

it("keeps download in the release overflow without lifecycle tabs", () => {
  renderRail(null);

  expect(screen.queryByRole("navigation", { name: "Video release stages" })).toBeNull();
  expect(screen.queryByRole("button", { name: "ready" })).toBeNull();
  expect(screen.queryByRole("button", { name: "publish" })).toBeNull();
  expect(screen.queryByRole("button", { name: "learn" })).toBeNull();
  expect(screen.getByText("Ready to publish")).not.toBeNull();
  expect(screen.queryByRole("button", { name: "Download video" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "More video actions" }));
  expect(screen.getByRole("button", { name: "Download video" })).not.toBeNull();
});

it("shows the frozen comparable result and keeps TikTok history accessible", async () => {
  const current: TikTokPublication = {
    ...basePublication,
    processing_status: "complete",
    visibility_status: "public",
    public_at: "2026-07-29T10:00:00Z",
    evaluation_metrics: {
      view_count: 2000,
      like_count: 240,
      comment_count: 18,
      share_count: 31,
      window_hours: 72,
    },
    evaluation_captured_at: "2026-08-01T10:00:00Z",
  };
  const previous: TikTokPublication = {
    ...current,
    id: "publication-0",
    evaluation_metrics: { ...current.evaluation_metrics, view_count: 1000 },
    created_at: "2026-07-25T10:00:00Z",
  };

  renderRail(current, [current, previous]);

  expect(await screen.findByText("What changed after 72 hours")).not.toBeNull();
  expect(screen.getByText(/2.0× the views/)).not.toBeNull();
  expect(screen.getByText("2K")).not.toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "TikTok history (2)" }));
  expect(screen.getByRole("dialog", { name: "TikTok history (2)" })).not.toBeNull();
  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByRole("dialog", { name: "TikTok history (2)" })).toBeNull();
});

it("uses account-wide publications for learning but item attempts for history", async () => {
  const current: TikTokPublication = {
    ...basePublication,
    processing_status: "complete",
    visibility_status: "public",
    public_at: "2026-07-29T10:00:00Z",
    evaluation_metrics: { view_count: 500, like_count: 50, comment_count: 4, share_count: 3, window_hours: 72 },
  };
  const previousOtherVideo: TikTokPublication = {
    ...current,
    id: "publication-other-video",
    job_id: "job-older",
    evaluation_metrics: { ...current.evaluation_metrics, view_count: 1000 },
  };

  renderRail(current, [current], { comparisonPublications: [current, previousOtherVideo] });

  expect(await screen.findByText(/50% fewer views/)).not.toBeNull();
  expect(screen.getByRole("button", { name: "TikTok history (1)" })).not.toBeNull();
});

it.each([
  ["private", "complete", "Published privately"],
  ["draft", "complete", "Ready in TikTok drafts"],
  ["removed", "complete", "No longer public"],
  ["unknown", "submission_unknown", "Check TikTok before retrying"],
] as const)("renders the %s/%s receipt state", (visibility, processing, heading) => {
  renderRail({
    ...basePublication,
    visibility_status: visibility,
    processing_status: processing,
    retryable: false,
  });

  expect(screen.getByText(heading)).not.toBeNull();
});

it("stops the working state after the creator posts an uploaded draft in TikTok", () => {
  renderRail({
    ...basePublication,
    delivery_mode: "draft_upload",
    processing_status: "complete",
    visibility_status: "unknown",
  });

  expect(screen.getByText("Posted from TikTok")).not.toBeNull();
  expect(screen.getByText(/audience was chosen there/)).not.toBeNull();
  expect(screen.queryByText("Sending to TikTok")).toBeNull();
});

it("fails closed when receipt history cannot be confirmed", () => {
  const onPublish = jest.fn();
  const onReceiptRetry = jest.fn();
  renderRail(null, [], { receiptState: "error", onPublish, onReceiptRetry });

  expect(screen.getByText(/Publishing stays paused to prevent a duplicate/)).not.toBeNull();
  expect(screen.queryByRole("button", { name: "Publish to TikTok" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Check again" }));
  expect(onReceiptRetry).toHaveBeenCalledTimes(1);
});

it("returns a confirmed failure to the full preparation pane instead of a dead receipt", () => {
  const onPublish = jest.fn();
  renderRail({
    ...basePublication,
    processing_status: "failed",
    retryable: false,
    failure_detail: "TikTok rejected the video's aspect ratio.",
  }, undefined, { onPublish });

  expect(screen.queryByText("Publishing failed")).toBeNull();
  expect(screen.getByText("Last attempt failed")).not.toBeNull();
  expect(screen.getByText("TikTok rejected the video's aspect ratio.")).not.toBeNull();
  expect(screen.getByRole("link", { name: "Edit video" })).not.toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Publish to TikTok" }));
  expect(onPublish).toHaveBeenCalledTimes(1);
});

it("sends an unconfirmed submission to TikTok recovery instead of a second delivery", () => {
  // TikTok never confirmed it received the first one. A publish button here can
  // post the same video twice; the fix is to go look in TikTok.
  const onReceiptRetry = jest.fn();
  renderRail(
    { ...basePublication, processing_status: "submission_unknown" },
    undefined,
    { onReceiptRetry },
  );

  expect(screen.getByText("Check TikTok before retrying")).not.toBeNull();
  expect(screen.getByRole("link", { name: "Open TikTok" })).not.toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Check status again" }));
  expect(onReceiptRetry).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("link", { name: "Edit video" })).not.toBeNull();
  expect(screen.queryByRole("button", { name: "Publish to TikTok" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Publish again" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Publish updated video" })).toBeNull();
});

it("keeps a retrying failure in receipt mode so a manual publish cannot race the worker", () => {
  renderRail({ ...basePublication, processing_status: "failed", retryable: true });

  expect(screen.getByText("TikTok is retrying")).not.toBeNull();
  expect(screen.queryByText("Last attempt failed")).toBeNull();
  expect(screen.getByRole("link", { name: "Edit video" })).not.toBeNull();
  // The worker is resubmitting this row itself. A publish button here races it
  // into a duplicate post — the receipt spinner stops, but the delivery hasn't.
  expect(screen.queryByRole("button", { name: "Publish again" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Publish updated video" })).toBeNull();
});

it("stays quiet about staleness when a timestamp will not parse", () => {
  renderRail(
    { ...basePublication, processing_status: "complete", visibility_status: "public" },
    undefined,
    { renderFinishedAt: "not-a-date" },
  );

  expect(screen.getByRole("button", { name: "Publish again" })).not.toBeNull();
  expect(screen.queryByRole("button", { name: "Publish updated video" })).toBeNull();
});

it("never hides Edit or Download once a publication exists for the job", () => {
  // Regression: the rail used to swap ReleasePreparationPane out wholesale when
  // `publication` went non-null, taking the only "Edit video" link in the app
  // with it — permanently, since publication rows are never deleted.
  renderRail(basePublication);

  expect(screen.getByText("Sending to TikTok")).not.toBeNull();
  expect(screen.getByRole("link", { name: "Edit video" }).getAttribute("href")).toBe("/edit/job-1");
  fireEvent.click(screen.getByRole("button", { name: "More video actions" }));
  expect(screen.getByRole("button", { name: "Download video" })).not.toBeNull();
});

it("offers a fresh publish in receipt mode, naming an edit made since the last attempt", () => {
  const onPublish = jest.fn();
  const published: TikTokPublication = {
    ...basePublication,
    processing_status: "complete",
    visibility_status: "private",
    created_at: "2026-08-01T10:00:00Z",
  };

  const { rerender } = renderRail(published, [published], {
    onPublish,
    renderFinishedAt: "2026-07-30T00:00:00Z",
  });
  fireEvent.click(screen.getByRole("button", { name: "Publish again" }));
  expect(onPublish).toHaveBeenCalledTimes(1);

  rerender(
    <TikTokReleaseRail
      connection={connection}
      publication={published}
      publications={[published]}
      canPublish
      baking={false}
      editHref="/edit/job-1"
      durationSeconds={18}
      renderFinishedAt="2026-08-02T00:00:00Z"
      variantLabel="Original text"
      onPublish={onPublish}
      onDownload={jest.fn()}
    />,
  );
  expect(screen.getByRole("button", { name: "Publish updated video" })).not.toBeNull();
  expect(screen.queryByRole("button", { name: "Publish again" })).toBeNull();
});

it("keeps a landed post calm but gives an edited cut primary weight", () => {
  const published: TikTokPublication = {
    ...basePublication,
    processing_status: "complete",
    visibility_status: "public",
    created_at: "2026-08-01T10:00:00Z",
  };

  const { rerender } = renderRail(published, [published], {
    renderFinishedAt: "2026-07-30T00:00:00Z",
  });
  // Nothing edited since posting — republish must not compete with the receipt.
  expect(screen.getByRole("button", { name: "Publish again" }).className).toContain("border-zinc-300");
  expect(screen.getByRole("button", { name: "Publish again" }).className).not.toContain("bg-[#0c0c0e]");

  rerender(
    <TikTokReleaseRail
      connection={connection}
      publication={published}
      publications={[published]}
      canPublish
      baking={false}
      editHref="/edit/job-1"
      durationSeconds={18}
      renderFinishedAt="2026-08-02T00:00:00Z"
      variantLabel="Original text"
      onPublish={jest.fn()}
      onDownload={jest.fn()}
    />,
  );
  expect(screen.getByRole("button", { name: "Publish updated video" }).className).toContain("bg-[#0c0c0e]");
});

it("withholds republish while the first submission is still in flight", () => {
  // basePublication is processing_status "processing" — TikTok still owes an
  // outcome. A publish button here would double-post.
  renderRail(basePublication);

  expect(screen.getByText("Sending to TikTok")).not.toBeNull();
  expect(screen.queryByRole("button", { name: "Publish again" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Publish updated video" })).toBeNull();
  expect(screen.getByRole("link", { name: "Edit video" })).not.toBeNull();
});

it("offers republish once TikTok settles the outcome", () => {
  renderRail({
    ...basePublication,
    processing_status: "complete",
    visibility_status: "public",
  });

  expect(screen.getByRole("button", { name: "Publish again" })).not.toBeNull();
});

it("locks the receipt-mode republish while an exact export is baking", () => {
  const onPublish = jest.fn();
  renderRail(
    { ...basePublication, processing_status: "complete", visibility_status: "private" },
    undefined,
    { onPublish, baking: true },
  );

  const republish = screen.getByRole("button", { name: "Preparing your video…" });
  expect((republish as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(republish);
  expect(onPublish).not.toHaveBeenCalled();
  expect((screen.getByRole("button", { name: "More video actions" }) as HTMLButtonElement).disabled).toBe(true);
});

it("drops the Edit link but keeps the overflow when the variant has no editor entry", () => {
  renderRail(basePublication, undefined, { editHref: null });

  expect(screen.queryByRole("link", { name: "Edit video" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "More video actions" }));
  expect(screen.getByRole("button", { name: "Download video" })).not.toBeNull();
});

it("falls back to generic failure copy when TikTok gives no detail", () => {
  renderRail({
    ...basePublication,
    processing_status: "failed",
    retryable: false,
    failure_detail: null,
  });

  expect(screen.getByText("Last attempt failed")).not.toBeNull();
  expect(screen.getByText("TikTok could not publish this post. Nothing was posted.")).not.toBeNull();
});

it("does not offer a repeat publish when the account can no longer publish", () => {
  renderRail(
    { ...basePublication, processing_status: "complete", visibility_status: "private" },
    undefined,
    { canPublish: false },
  );

  expect(screen.queryByRole("button", { name: "Publish again" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Publish updated video" })).toBeNull();
  expect(screen.getByRole("link", { name: "Edit video" })).not.toBeNull();
});

it("does not expose a dead-end learning state before publishing", () => {
  const onPublish = jest.fn();
  renderRail(null, [], {
    connection: { ...connection, connected: false, can_publish: false, account: null },
    canPublish: false,
    onPublish,
  });

  expect(onPublish).not.toHaveBeenCalled();
  expect(screen.queryByText("Learning starts after publishing")).toBeNull();
  expect(screen.queryByRole("button", { name: "Publish first" })).toBeNull();
  expect(screen.getByText("Connect TikTok before publishing.")).not.toBeNull();
});

it("does not claim a rendering or failed video is publishable", () => {
  renderRail(null, [], { canPublish: false, videoReady: false });

  expect(screen.getByText("Publishing unlocks after this video finishes rendering successfully.")).not.toBeNull();
  expect(screen.queryByRole("button", { name: "Publish to TikTok" })).toBeNull();
  expect(screen.getByText("Video isn't ready yet")).not.toBeNull();
  expect(screen.getByText("Waiting for render")).not.toBeNull();
  expect((screen.getByRole("button", { name: "More video actions" }) as HTMLButtonElement).disabled).toBe(true);
});

it("keeps secondary publish actions locked while an exact export is baking", () => {
  const onPublish = jest.fn();
  const { rerender } = renderRail({ ...basePublication, processing_status: "failed", retryable: false }, undefined, {
    onPublish,
    baking: true,
  });

  const retry = screen.getByRole("button", { name: "Preparing your video…" });
  expect((retry as HTMLButtonElement).disabled).toBe(true);
  rerender(
    <TikTokReleaseRail
      connection={connection}
      publication={null}
      publications={[]}
      canPublish
      baking
      editHref="/edit/job-1"
      durationSeconds={18}
      variantLabel="Original text"
      onPublish={onPublish}
      onDownload={jest.fn()}
    />,
  );
  expect((screen.getByRole("button", { name: "Preparing your video…" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "More video actions" }) as HTMLButtonElement).disabled).toBe(true);
  expect(onPublish).not.toHaveBeenCalled();
});

it("does not claim there is no baseline when comparison history failed to load", async () => {
  const current: TikTokPublication = {
    ...basePublication,
    processing_status: "complete",
    visibility_status: "public",
    evaluation_metrics: { view_count: 500, like_count: 50, comment_count: 4, share_count: 3, window_hours: 72 },
  };
  renderRail(current, [current], { comparisonAvailable: false });

  expect(await screen.findByText(/Comparison history is unavailable right now/)).not.toBeNull();
  expect(screen.queryByText(/More published posts will make the comparison stronger/)).toBeNull();
});
