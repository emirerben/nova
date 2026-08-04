import {
  createTikTokPublication,
  disconnectTikTok,
  getTikTokConnection,
  getTikTokPublicationReceipt,
  getTikTokPublishOptions,
  listTikTokPublications,
  shouldPollTikTokPublication,
  startTikTokOAuth,
} from "@/lib/tiktok-api";
import { NotAuthenticatedError } from "@/lib/plan-api";

function response(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

afterEach(() => jest.restoreAllMocks());

it("routes authenticated TikTok calls through the same-origin plan proxy", async () => {
  const fetchMock = jest.fn().mockResolvedValue(response(200, { connected: false }));
  global.fetch = fetchMock as typeof fetch;
  await getTikTokConnection();
  expect(fetchMock).toHaveBeenCalledWith("/api/plan/tiktok/connection", expect.any(Object));
});

it("keeps TikTok OAuth connected to the current item journey", async () => {
  const fetchMock = jest.fn().mockResolvedValue(response(200, { authorization_url: "https://tiktok.test/oauth" }));
  global.fetch = fetchMock as typeof fetch;
  const assign = jest.fn();
  const originalLocation = window.location;
  try {
    Object.defineProperty(window, "location", { value: { assign }, writable: true });

    await startTikTokOAuth("/plan/items/item-1?tiktok=return");

    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init.body)).toEqual({ return_to: "/plan/items/item-1?tiktok=return" });
    expect(assign).toHaveBeenCalledWith("https://tiktok.test/oauth");
  } finally {
    Object.defineProperty(window, "location", { value: originalLocation, writable: true });
  }
});

it("includes the exact displayed variant in publish options", async () => {
  const fetchMock = jest.fn().mockResolvedValue(response(200, {}));
  global.fetch = fetchMock as typeof fetch;
  await getTikTokPublishOptions("job-1", "song_text");
  expect(fetchMock.mock.calls[0][0]).toContain("job_id=job-1");
  expect(fetchMock.mock.calls[0][0]).toContain("variant_id=song_text");
});

it("scopes receipt history to the exact job and variant", async () => {
  const fetchMock = jest.fn().mockResolvedValue(response(200, []));
  global.fetch = fetchMock as typeof fetch;
  await listTikTokPublications({ jobId: "job-1", variantId: "song_text" });
  expect(fetchMock.mock.calls[0][0]).toContain("job_id=job-1");
  expect(fetchMock.mock.calls[0][0]).toContain("variant_id=song_text");
});

it("loads the canonical receipt through the dedicated fail-closed endpoint", async () => {
  const fetchMock = jest.fn().mockResolvedValue(response(200, null));
  global.fetch = fetchMock as typeof fetch;
  await getTikTokPublicationReceipt("job-1", "song_text");
  expect(fetchMock.mock.calls[0][0]).toContain("/publications/receipt?");
  expect(fetchMock.mock.calls[0][0]).toContain("job_id=job-1");
  expect(fetchMock.mock.calls[0][0]).toContain("variant_id=song_text");
});

it("posts publication consent as JSON", async () => {
  const fetchMock = jest.fn().mockResolvedValue(response(202, { id: "publication-1" }));
  global.fetch = fetchMock as typeof fetch;
  await createTikTokPublication({
    job_id: "job-1",
    source_revision: "a".repeat(64),
    idempotency_key: "idem-12345",
    title: "caption",
    privacy_level: "SELF_ONLY",
    allow_comment: false,
    allow_duet: false,
    allow_stitch: false,
    brand_content_toggle: false,
    brand_organic_toggle: false,
    is_aigc: false,
    music_usage_confirmed: true,
    consent_version: "2026-08-01",
  });
  const [, init] = fetchMock.mock.calls[0];
  expect(init.method).toBe("POST");
  expect(JSON.parse(init.body)).toEqual(expect.objectContaining({ source_revision: "a".repeat(64) }));
});

it("turns a 401 into the shared authentication error", async () => {
  global.fetch = jest.fn().mockResolvedValue(response(401, {})) as typeof fetch;
  await expect(getTikTokConnection()).rejects.toBeInstanceOf(NotAuthenticatedError);
});

it("surfaces TikTok error detail and falls back for non-JSON failures", async () => {
  global.fetch = jest.fn().mockResolvedValue(response(409, { detail: "Reconnect TikTok" })) as typeof fetch;
  await expect(getTikTokConnection()).rejects.toThrow("Reconnect TikTok");

  global.fetch = jest.fn().mockResolvedValue({
    ok: false,
    status: 503,
    json: async () => { throw new Error("not json"); },
  } as unknown as Response) as typeof fetch;
  await expect(getTikTokConnection()).rejects.toThrow("TikTok request failed (503)");
});

it("handles a disconnect response with no JSON body", async () => {
  global.fetch = jest.fn().mockResolvedValue(response(204, undefined)) as typeof fetch;
  await expect(disconnectTikTok()).resolves.toBeUndefined();
});

it("stops polling completed private publications", () => {
  const publication = {
    id: "publication-1",
    job_id: "job-1",
    variant_id: null,
    processing_status: "complete",
    visibility_status: "private",
    retryable: false,
    failure_code: null,
    failure_detail: null,
    latest_metrics: null,
    metrics_synced_at: null,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:01:00Z",
  };

  expect(shouldPollTikTokPublication(publication)).toBe(false);
  expect(shouldPollTikTokPublication({ ...publication, visibility_status: "unknown" })).toBe(true);
  expect(shouldPollTikTokPublication({
    ...publication,
    processing_status: "failed",
    visibility_status: "unknown",
    retryable: true,
  })).toBe(true);
  expect(
    shouldPollTikTokPublication({
      ...publication,
      id: "local-preview-11111111-1111-4111-8111-111111111111",
      processing_status: "processing",
      visibility_status: "unknown",
    }),
  ).toBe(false);
});
