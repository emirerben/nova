import {
  createOwnedGenerativeJob,
  getOwnedGenerativeJobStatus,
  openGenerativeJobInEditor,
  retryOwnedGenerativeJob,
  uploadOwnedGenerativeClip,
} from "@/lib/generative-api";

describe("owned generative API", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    global.fetch = jest.fn();
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("uses the same-origin authenticated proxy for upload initialization", async () => {
    const fetchMock = global.fetch as jest.Mock;
    fetchMock
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
            upload_url: "https://storage.example/signed",
            gcs_path: "users/u/clip.mp4",
            kind: "video",
            content_type: "video/mp4",
            upload_headers: {},
          }),
      })
      .mockResolvedValueOnce({ ok: true, status: 200 });

    await expect(
      uploadOwnedGenerativeClip(new File(["clip"], "clip.mp4", { type: "video/mp4" })),
    ).resolves.toEqual({ gcs_path: "users/u/clip.mp4", kind: "video" });

    expect(fetchMock.mock.calls[0][0]).toBe("/api/plan/generative-jobs/upload-url");
    expect(fetchMock.mock.calls[1][0]).toBe("https://storage.example/signed");
  });

  it("streams an owned upload fallback directly through Fly after a storage network error", async () => {
    const fetchMock = global.fetch as jest.Mock;
    fetchMock
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          upload_url: "https://storage.example/signed",
          gcs_path: "users/u/large.mp4",
          kind: "video",
          content_type: "video/mp4",
          upload_headers: {},
        }),
      })
      .mockRejectedValueOnce(new TypeError("CORS blocked"))
      .mockResolvedValueOnce({ ok: true, status: 200 });

    await expect(
      uploadOwnedGenerativeClip(new File(["clip"], "large.mp4", { type: "video/mp4" })),
    ).resolves.toEqual({ gcs_path: "users/u/large.mp4", kind: "video" });

    expect(fetchMock.mock.calls[2][0]).toBe("http://localhost:8000/uploads/relay");
    expect(fetchMock.mock.calls[2][1]).toEqual(
      expect.objectContaining({ method: "POST", body: expect.any(FormData) }),
    );
  });

  it("routes create, status, retry, and promotion through authenticated same-origin endpoints", async () => {
    const fetchMock = global.fetch as jest.Mock;
    fetchMock
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ job_id: "job/1", status: "queued" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ job_id: "job/1", status: "processing", variants: [] }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ job_id: "job/1", status: "queued" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ plan_item_id: "item-1", variant_id: "variant-1" }),
      });

    await createOwnedGenerativeJob(["users/u/clip.mp4"], null, { intent: "Warm" });
    await getOwnedGenerativeJobStatus("job/1");
    await retryOwnedGenerativeJob("job/1");
    await openGenerativeJobInEditor("job/1", "Warm");

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/plan/generative-jobs",
      "/api/plan/generative-jobs/job%2F1/status",
      "/api/me/jobs/job%2F1/retry",
      "/api/me/jobs/job%2F1/open-in-editor",
    ]);
  });
});
