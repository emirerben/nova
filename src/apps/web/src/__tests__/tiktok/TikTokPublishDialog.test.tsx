import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { TikTokPublishDialog } from "@/components/TikTokPublishDialog";
import { createTikTokPublication, getTikTokPublishOptions } from "@/lib/tiktok-api";

jest.mock("@/lib/tiktok-api", () => ({
  getTikTokPublishOptions: jest.fn(),
  createTikTokPublication: jest.fn(),
  startTikTokOAuth: jest.fn(),
}));

const mockedOptions = getTikTokPublishOptions as jest.MockedFunction<typeof getTikTokPublishOptions>;
const mockedCreate = createTikTokPublication as jest.MockedFunction<typeof createTikTokPublication>;

beforeAll(() => {
  Object.defineProperty(globalThis, "crypto", {
    value: { randomUUID: () => "11111111-1111-4111-8111-111111111111" },
    configurable: true,
  });
});

beforeEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
  mockedOptions.mockResolvedValue({
    preview_url: "https://example.test/video.mp4",
    source_revision: "a".repeat(64),
    variant_id: "song_text",
    duration_s: 18,
    creator_nickname: "Creator",
    privacy_options: ["SELF_ONLY"],
    comment_disabled: false,
    duet_disabled: true,
    stitch_disabled: false,
    max_duration_s: 60,
    suggested_title: "A caption #topic",
    audited: false,
    consent_version: "2026-08-11",
    can_direct_post: true,
    can_upload_draft: true,
  });
});

it("requires manual privacy and music confirmation before review", async () => {
  render(
    <TikTokPublishDialog
      open
      jobId="job-1"
      variantId="song_text"
      onClose={jest.fn()}
    />,
  );

  await screen.findByText("Creator");
  expect(document.activeElement).toBe(screen.getByRole("heading", { name: "TikTok delivery details" }));
  expect((screen.getByRole("radio", { name: /Only you/ }) as HTMLInputElement).checked).toBe(false);
  expect((screen.getByLabelText("Comments off") as HTMLInputElement).checked).toBe(false);
  fireEvent.focus(screen.getByLabelText("Comments off"));
  expect(screen.getByLabelText("Comments off").closest("label")?.className).toContain("focus-within:outline");
  expect((screen.getByLabelText("Duet unavailable") as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByLabelText("Stitch off") as HTMLInputElement).checked).toBe(false);
  expect(
    (screen.getByRole("button", { name: "Review post" }) as HTMLButtonElement).disabled,
  ).toBe(true);

  fireEvent.click(screen.getByRole("radio", { name: /Only you/ }));
  fireEvent.click(screen.getByLabelText(/Music Usage Confirmation/));
  expect(
    (screen.getByRole("button", { name: "Review post" }) as HTMLButtonElement).disabled,
  ).toBe(false);
});

it("submits the exact source revision and unchecked interaction defaults", async () => {
  mockedCreate.mockResolvedValue({
    id: "publication-1",
    job_id: "job-1",
    variant_id: "song_text",
    processing_status: "queued",
    visibility_status: "unknown",
    retryable: false,
    failure_code: null,
    failure_detail: null,
    latest_metrics: null,
    metrics_synced_at: null,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
  });
  const onPublished = jest.fn();
  render(
    <TikTokPublishDialog
      open
      jobId="job-1"
      variantId="song_text"
      onClose={jest.fn()}
      onPublished={onPublished}
    />,
  );
  await screen.findByText("Creator");
  fireEvent.click(screen.getByRole("radio", { name: /Only you/ }));
  fireEvent.click(screen.getByLabelText(/Music Usage Confirmation/));
  fireEvent.click(screen.getByRole("button", { name: "Review post" }));
  fireEvent.click(screen.getByRole("button", { name: "Publish now" }));

  await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1));
  expect(mockedCreate).toHaveBeenCalledWith(expect.objectContaining({
    job_id: "job-1",
    variant_id: "song_text",
    source_revision: "a".repeat(64),
    privacy_level: "SELF_ONLY",
    allow_comment: false,
    allow_duet: false,
    allow_stitch: false,
    music_usage_confirmed: true,
    delivery_mode: "direct_post",
    draft_handoff_confirmed: false,
  }));
  expect(onPublished).toHaveBeenCalledWith(expect.objectContaining({ id: "publication-1" }));
});

it("shows the exact confirmation summary and returns to editable details", async () => {
  render(
    <TikTokPublishDialog open jobId="job-1" variantId="song_text" onClose={jest.fn()} />,
  );
  await screen.findByText("Creator");
  fireEvent.change(screen.getByRole("textbox"), {
    target: { value: "Final caption #kria" },
  });
  fireEvent.click(screen.getByRole("radio", { name: /Only you/ }));
  fireEvent.click(screen.getByLabelText("Comments off"));
  fireEvent.click(screen.getByLabelText(/Music Usage Confirmation/));
  fireEvent.click(screen.getByRole("button", { name: "Review post" }));

  expect(screen.getByRole("heading", { name: "Confirm TikTok delivery" })).not.toBeNull();
  expect(screen.getByText("Final caption #kria")).not.toBeNull();
  expect(screen.getByText("Comments on")).not.toBeNull();
  expect(screen.getByText(/Changes or removal may need to be made in TikTok/)).not.toBeNull();

  expect(screen.getAllByRole("button", { name: "Edit details" })).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Edit details" }));
  expect(screen.getByDisplayValue("Final caption #kria")).not.toBeNull();
});

it("prevents rapid double submission while the first request is pending", async () => {
  let resolvePublication: ((value: Awaited<ReturnType<typeof createTikTokPublication>>) => void) | undefined;
  mockedCreate.mockImplementation(() => new Promise((resolve) => { resolvePublication = resolve; }));
  render(
    <TikTokPublishDialog open jobId="job-1" variantId="song_text" onClose={jest.fn()} />,
  );
  await screen.findByText("Creator");
  fireEvent.click(screen.getByRole("radio", { name: /Only you/ }));
  fireEvent.click(screen.getByLabelText(/Music Usage Confirmation/));
  fireEvent.click(screen.getByRole("button", { name: "Review post" }));
  const publish = screen.getByRole("button", { name: "Publish now" });
  fireEvent.click(publish);
  fireEvent.click(publish);

  expect(mockedCreate).toHaveBeenCalledTimes(1);
  expect((screen.getByRole("button", { name: "Sending to TikTok…" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "Exit" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByRole("dialog", { name: "Send to TikTok" })).not.toBeNull();
  resolvePublication?.({
    id: "publication-1",
    job_id: "job-1",
    variant_id: "song_text",
    processing_status: "queued",
    visibility_status: "unknown",
    retryable: false,
    failure_code: null,
    failure_detail: null,
    latest_metrics: null,
    metrics_synced_at: null,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
  });
});

it("shows publish-options failures instead of a stuck loading state", async () => {
  mockedOptions.mockRejectedValue(new Error("Reconnect TikTok"));
  render(
    <TikTokPublishDialog open jobId="job-1" variantId="song_text" onClose={jest.fn()} />,
  );
  expect(await screen.findByText("Reconnect TikTok")).not.toBeNull();
  expect(screen.getByRole("button", { name: "Retry settings" })).not.toBeNull();
  expect(screen.getByRole("button", { name: "Return to item" })).not.toBeNull();
  expect(screen.queryByText(/Checking TikTok settings/)).toBeNull();
});

it("resets the workspace scroll position when moving to confirmation", async () => {
  render(
    <TikTokPublishDialog open jobId="job-1" variantId="song_text" onClose={jest.fn()} />,
  );
  await screen.findByText("Creator");
  const scroll = screen.getByTestId("tiktok-publish-scroll");
  scroll.scrollTop = 480;
  fireEvent.click(screen.getByRole("radio", { name: /Only you/ }));
  fireEvent.click(screen.getByLabelText(/Music Usage Confirmation/));
  fireEvent.click(screen.getByRole("button", { name: "Review post" }));

  expect(scroll.scrollTop).toBe(0);
  expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Confirm TikTok delivery" }));
});

it("keeps the dialog recoverable after publication submission fails", async () => {
  mockedCreate.mockRejectedValue(new Error("The video changed"));
  render(
    <TikTokPublishDialog open jobId="job-1" variantId="song_text" onClose={jest.fn()} />,
  );
  await screen.findByText("Creator");
  fireEvent.click(screen.getByRole("radio", { name: /Only you/ }));
  fireEvent.click(screen.getByLabelText(/Music Usage Confirmation/));
  fireEvent.click(screen.getByRole("button", { name: "Review post" }));
  fireEvent.click(screen.getByRole("button", { name: "Publish now" }));

  expect(await screen.findByText("The video changed before TikTok received it.")).not.toBeNull();
  expect(screen.getByText("Your details are still here. Review them and try again.")).not.toBeNull();
  expect((screen.getByRole("button", { name: "Publish now" }) as HTMLButtonElement).disabled).toBe(false);
});

it("simulates the connected publish flow without calling TikTok APIs", async () => {
  const onPublished = jest.fn();
  render(
    <TikTokPublishDialog
      open
      jobId="job-1"
      variantId="song_text"
      videoTitle="Preview caption"
      simulation={{
        creatorNickname: "Emir",
        previewUrl: "https://example.test/local-preview.mp4",
        durationSeconds: 18,
      }}
      onClose={jest.fn()}
      onPublished={onPublished}
    />,
  );

  expect(await screen.findByText("No TikTok API request will be made.")).not.toBeNull();
  expect(screen.getByRole("dialog", { name: "Preview TikTok delivery" })).not.toBeNull();
  expect(screen.getByTestId("tiktok-publish-workspace").className).toContain("fixed inset-0");
  expect(screen.getByTestId("tiktok-publish-workspace").className).not.toContain("md:w-[560px]");
  fireEvent.click(screen.getByRole("radio", { name: /Public/ }));
  fireEvent.click(screen.getByLabelText(/Music Usage Confirmation/));
  fireEvent.click(screen.getByRole("button", { name: "Review post" }));
  expect(screen.getByText("Post summary")).not.toBeNull();
  expect(screen.getByText("Completing this preview creates a local receipt only. Nothing will be sent to TikTok.")).not.toBeNull();
  expect(screen.queryByText(/Publishing creates a TikTok post/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Simulate delivery" }));

  expect(mockedOptions).not.toHaveBeenCalled();
  expect(mockedCreate).not.toHaveBeenCalled();
  expect(onPublished).toHaveBeenCalledWith(expect.objectContaining({
    job_id: "job-1",
    creator_nickname: "Emir",
    processing_status: "processing",
  }));
});

it("sends the exact render to the TikTok inbox with explicit handoff consent", async () => {
  mockedCreate.mockResolvedValue({
    id: "draft-1",
    job_id: "job-1",
    variant_id: "song_text",
    delivery_mode: "draft_upload",
    processing_status: "queued",
    visibility_status: "unknown",
    retryable: false,
    failure_code: null,
    failure_detail: null,
    latest_metrics: null,
    metrics_synced_at: null,
    created_at: "2026-08-11T00:00:00Z",
    updated_at: "2026-08-11T00:00:00Z",
  });
  render(
    <TikTokPublishDialog open jobId="job-1" variantId="song_text" onClose={jest.fn()} />,
  );

  await screen.findByText("Creator");
  fireEvent.click(screen.getByRole("radio", { name: /Finish in TikTok/ }));
  expect(screen.getByText(/TikTok will send an inbox notification/)).not.toBeNull();
  fireEvent.click(screen.getByLabelText(/Music Usage Confirmation/));
  fireEvent.click(screen.getByLabelText(/must open the TikTok app on my phone/));
  fireEvent.click(screen.getByRole("button", { name: "Review handoff" }));
  expect(screen.getByText("Inbox handoff summary")).not.toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Send to TikTok inbox" }));

  await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1));
  expect(mockedCreate).toHaveBeenCalledWith(expect.objectContaining({
    delivery_mode: "draft_upload",
    source_revision: "a".repeat(64),
    privacy_level: "TIKTOK_DRAFT",
    title: "",
    draft_handoff_confirmed: true,
  }));
});

it("auto-selects draft handoff when the grant only includes upload", async () => {
  mockedOptions.mockResolvedValue({
    preview_url: "https://example.test/video.mp4",
    source_revision: "a".repeat(64),
    variant_id: "song_text",
    duration_s: 18,
    creator_nickname: "Upload-only creator",
    privacy_options: ["SELF_ONLY"],
    comment_disabled: false,
    duet_disabled: false,
    stitch_disabled: false,
    max_duration_s: 60,
    suggested_title: "A caption #topic",
    audited: false,
    consent_version: "2026-08-11",
    can_direct_post: false,
    can_upload_draft: true,
  });

  render(
    <TikTokPublishDialog open jobId="job-1" variantId="song_text" onClose={jest.fn()} />,
  );

  await screen.findByText("Upload-only creator");
  expect((screen.getByRole("radio", { name: /Post now/ }) as HTMLInputElement).disabled).toBe(true);
  expect(
    (screen.getByRole("radio", { name: /Finish in TikTok/ }) as HTMLInputElement).checked,
  ).toBe(true);
  expect(screen.getByText(/TikTok will send an inbox notification/)).not.toBeNull();
});

it("reuses the delivery-mode idempotency key after an ambiguous remount", async () => {
  const storageKey = "tiktok:publish-key:job-1:song_text:draft_upload";
  window.sessionStorage.setItem(storageKey, "stable-draft-key");
  mockedCreate.mockRejectedValue(new Error("TikTok did not confirm whether it received the delivery"));

  const first = render(
    <TikTokPublishDialog open jobId="job-1" variantId="song_text" onClose={jest.fn()} />,
  );
  await screen.findByText("Creator");
  fireEvent.click(screen.getByRole("radio", { name: /Finish in TikTok/ }));
  fireEvent.click(screen.getByLabelText(/Music Usage Confirmation/));
  fireEvent.click(screen.getByLabelText(/must open the TikTok app on my phone/));
  fireEvent.click(screen.getByRole("button", { name: "Review handoff" }));
  fireEvent.click(screen.getByRole("button", { name: "Send to TikTok inbox" }));
  await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1));
  first.unmount();

  render(<TikTokPublishDialog open jobId="job-1" variantId="song_text" onClose={jest.fn()} />);
  await screen.findByText("Creator");
  fireEvent.click(screen.getByRole("radio", { name: /Finish in TikTok/ }));
  fireEvent.click(screen.getByLabelText(/Music Usage Confirmation/));
  fireEvent.click(screen.getByLabelText(/must open the TikTok app on my phone/));
  fireEvent.click(screen.getByRole("button", { name: "Review handoff" }));
  fireEvent.click(screen.getByRole("button", { name: "Send to TikTok inbox" }));
  await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(2));

  expect(mockedCreate.mock.calls[0][0].idempotency_key).toBe("stable-draft-key");
  expect(mockedCreate.mock.calls[1][0].idempotency_key).toBe("stable-draft-key");
});

it("blocks TikTok's invalid branded-content plus private-privacy combination", async () => {
  render(
    <TikTokPublishDialog open jobId="job-1" variantId="song_text" onClose={jest.fn()} />,
  );
  await screen.findByText("Creator");
  fireEvent.click(screen.getByRole("radio", { name: /Only you/ }));
  fireEvent.click(screen.getByLabelText("This video promotes a brand, product, or service"));
  fireEvent.click(screen.getByLabelText(/Music Usage Confirmation/));

  expect(screen.getByText("Choose at least one commercial-content type.")).not.toBeNull();
  expect((screen.getByRole("button", { name: "Review post" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByLabelText("Branded Content"));

  expect(screen.getByText(/Branded Content Policy and Music Usage Confirmation/)).not.toBeNull();
  expect(screen.getByText(/does not allow branded content/)).not.toBeNull();
  expect((screen.getByRole("button", { name: "Review post" }) as HTMLButtonElement).disabled).toBe(true);
});
