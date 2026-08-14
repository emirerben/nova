import { PlanApiError, requestPoolAssetUploadUrls } from "@/lib/plan-api";

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
});
