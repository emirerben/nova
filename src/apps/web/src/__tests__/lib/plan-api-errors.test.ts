import { analyzeTikTokStyle, PlanApiError, requestPoolAssetUploadUrls } from "@/lib/plan-api";

describe("PlanApiError metadata", () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("preserves actionable terminal 4xx detail and retryability", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 409,
      headers: new Headers({ "X-Request-Id": "req-4xx" }),
      json: jest.fn().mockResolvedValue({
          detail: {
            message: "This upload retry does not match the originally selected file.",
            code: "reservation_mismatch",
            retryable: false,
            stage: "registration",
          },
          request_id: "req-4xx",
      }),
    });

    await expect(
      requestPoolAssetUploadUrls(
        "item-1",
        [
          {
            filename: "shot.png",
            content_type: "image/png",
            file_size_bytes: 10,
            client_upload_id: "file-1",
          },
        ],
        "batch-1",
      ),
    ).rejects.toMatchObject<Partial<PlanApiError>>({
      name: "PlanApiError",
      message: "This upload retry does not match the originally selected file.",
      status: 409,
      code: "reservation_mismatch",
      retryable: false,
      requestId: "req-4xx",
      stage: "registration",
    });
  });

  it("starts TikTok style analysis only through the explicit POST endpoint", async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: jest.fn().mockResolvedValue({ queued: true }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    await expect(analyzeTikTokStyle("persona-1")).resolves.toEqual({ queued: true });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/plan/personas/persona-1/analyze-tiktok-style",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("preserves the budget reset contract", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 429,
      headers: new Headers(),
      json: jest.fn().mockResolvedValue({
        detail: "AI budget exhausted for this scope.",
        code: "ai_budget_exhausted",
        scope: "director_daily",
        reset_at: "2026-09-09T00:00:00+00:00",
        retryable: false,
      }),
    });

    await expect(analyzeTikTokStyle("persona-1")).rejects.toMatchObject<Partial<PlanApiError>>({
      status: 429,
      code: "ai_budget_exhausted",
      retryable: false,
      scope: "director_daily",
      resetAt: "2026-09-09T00:00:00+00:00",
    });
  });
});
