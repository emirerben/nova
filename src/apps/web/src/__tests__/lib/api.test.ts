import { createTemplateJob } from "@/lib/api";

describe("createTemplateJob retry behaviour", () => {
  const okResponse = () =>
    ({
      ok: true,
      status: 200,
      json: async () => ({
        job_id: "job-123",
        status: "queued",
        template_id: "tpl-1",
      }),
    }) as unknown as Response;

  const params = {
    template_id: "tpl-1",
    clip_gcs_paths: ["a/b/clip_001.mp4"],
    selected_platforms: ["tiktok"],
  };

  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it("succeeds without retry when the first fetch resolves", async () => {
    const fetchMock = jest.fn().mockResolvedValue(okResponse());
    global.fetch = fetchMock as unknown as typeof fetch;

    await expect(createTemplateJob(params)).resolves.toEqual({
      job_id: "job-123",
      status: "queued",
      template_id: "tpl-1",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("recovers when the first fetch throws TypeError and the retry resolves", async () => {
    const fetchMock = jest
      .fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(okResponse());
    global.fetch = fetchMock as unknown as typeof fetch;

    const promise = createTemplateJob(params);
    // Attach the assertion before advancing the timer so the resolution
    // handler is in place by the time the retry fetch resolves.
    const expectation = expect(promise).resolves.toMatchObject({
      job_id: "job-123",
    });
    await jest.advanceTimersByTimeAsync(2000);
    await expectation;
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("throws the user-visible error when both attempts fail", async () => {
    const fetchMock = jest
      .fn()
      .mockRejectedValue(new TypeError("Failed to fetch"));
    global.fetch = fetchMock as unknown as typeof fetch;

    const promise = createTemplateJob(params);
    // Attach the rejection assertion before advancing the timer so the
    // unhandled-rejection guard sees the handler in place.
    const expectation = expect(promise).rejects.toThrow(
      "Cannot reach the server. Make sure the API is running.",
    );
    await jest.advanceTimersByTimeAsync(2000);
    await expectation;
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
