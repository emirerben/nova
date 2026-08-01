import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { TikTokPublishDialog } from "@/components/TikTokPublishDialog";
import { createTikTokPublication, getTikTokPublishOptions } from "@/lib/tiktok-api";

jest.mock("@/lib/tiktok-api", () => ({
  getTikTokPublishOptions: jest.fn(),
  createTikTokPublication: jest.fn(),
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
    consent_version: "2026-08-01",
  });
});

it("requires manual privacy and music confirmation with interactions off", async () => {
  render(
    <TikTokPublishDialog
      open
      jobId="job-1"
      variantId="song_text"
      onClose={jest.fn()}
    />,
  );

  await screen.findByText(/Posting as/);
  expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("");
  expect((screen.getByLabelText("Comment") as HTMLInputElement).checked).toBe(false);
  expect((screen.getByLabelText(/Duet/) as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByLabelText("Stitch") as HTMLInputElement).checked).toBe(false);
  expect(
    (screen.getByRole("button", { name: "Publish now" }) as HTMLButtonElement).disabled,
  ).toBe(true);

  fireEvent.change(screen.getByRole("combobox"), { target: { value: "SELF_ONLY" } });
  fireEvent.click(screen.getByLabelText(/Music Usage Confirmation/));
  expect(
    (screen.getByRole("button", { name: "Publish now" }) as HTMLButtonElement).disabled,
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
  await screen.findByText(/Posting as/);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "SELF_ONLY" } });
  fireEvent.click(screen.getByLabelText(/Music Usage Confirmation/));
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
  }));
  expect(onPublished).toHaveBeenCalledWith(expect.objectContaining({ id: "publication-1" }));
});

it("shows publish-options failures instead of a stuck loading state", async () => {
  mockedOptions.mockRejectedValue(new Error("Reconnect TikTok"));
  render(
    <TikTokPublishDialog open jobId="job-1" variantId="song_text" onClose={jest.fn()} />,
  );
  expect(await screen.findByText("Reconnect TikTok")).not.toBeNull();
  expect(screen.queryByText(/Checking your TikTok/)).toBeNull();
});

it("keeps the dialog recoverable after publication submission fails", async () => {
  mockedCreate.mockRejectedValue(new Error("The video changed"));
  render(
    <TikTokPublishDialog open jobId="job-1" variantId="song_text" onClose={jest.fn()} />,
  );
  await screen.findByText(/Posting as/);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "SELF_ONLY" } });
  fireEvent.click(screen.getByLabelText(/Music Usage Confirmation/));
  fireEvent.click(screen.getByRole("button", { name: "Publish now" }));

  expect(await screen.findByText("The video changed")).not.toBeNull();
  expect((screen.getByRole("button", { name: "Publish now" }) as HTMLButtonElement).disabled).toBe(false);
});

it("blocks TikTok's invalid branded-content plus private-privacy combination", async () => {
  render(
    <TikTokPublishDialog open jobId="job-1" variantId="song_text" onClose={jest.fn()} />,
  );
  await screen.findByText(/Posting as/);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "SELF_ONLY" } });
  fireEvent.click(screen.getByLabelText("This video promotes a brand, product, or service"));
  fireEvent.click(screen.getByLabelText(/Music Usage Confirmation/));

  expect(screen.getByText("Choose at least one commercial-content type.")).not.toBeNull();
  expect((screen.getByRole("button", { name: "Publish now" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByLabelText("Branded Content"));

  expect(screen.getByText(/Branded Content Policy and Music Usage Confirmation/)).not.toBeNull();
  expect(screen.getByText(/does not allow branded content/)).not.toBeNull();
  expect((screen.getByRole("button", { name: "Publish now" }) as HTMLButtonElement).disabled).toBe(true);
});
