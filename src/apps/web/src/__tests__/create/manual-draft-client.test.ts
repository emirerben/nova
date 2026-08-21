import {
  createOrResumeManualDraft,
  initializeManualDraft,
  type ManualDraftMedia,
} from "@/lib/plan-api";

const response = {
  plan_item_id: "item-1",
  job_id: "job-1",
  variant_id: null,
  status: "draft" as const,
};

describe("manual draft API client", () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("uses the authenticated plan proxy to create or resume a draft", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 201,
      json: jest.fn().mockResolvedValue(response),
    });

    await expect(createOrResumeManualDraft()).resolves.toEqual(response);

    expect(global.fetch).toHaveBeenCalledWith(
      "/api/plan/plan-items/manual-drafts",
      expect.objectContaining({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    );
  });

  it("initializes the canonical manual variant with ordered media metadata", async () => {
    const initialized = { ...response, variant_id: "original_text" };
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: jest.fn().mockResolvedValue(initialized),
    });
    const media: ManualDraftMedia[] = [
      { gcs_path: "users/u/first.mp4", duration_s: 8, kind: "video" },
      { gcs_path: "users/u/second.jpg", duration_s: 3, kind: "image" },
    ];

    await expect(initializeManualDraft("item-1", media)).resolves.toEqual(initialized);

    expect(global.fetch).toHaveBeenCalledWith(
      "/api/plan/plan-items/item-1/manual-draft/initialize",
      expect.objectContaining({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ media }),
      }),
    );
  });
});
