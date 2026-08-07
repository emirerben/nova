import TikTokProductPage from "@/app/tiktok/page";

describe("TikTok product page demo video", () => {
  const originalFetch = global.fetch;
  const originalApiUrl = process.env.API_URL;
  const originalPublicApiUrl = process.env.NEXT_PUBLIC_API_URL;

  beforeEach(() => {
    process.env.API_URL = "https://api.example.com";
    delete process.env.NEXT_PUBLIC_API_URL;
  });

  afterEach(() => {
    global.fetch = originalFetch;
    if (originalApiUrl === undefined) delete process.env.API_URL;
    else process.env.API_URL = originalApiUrl;
    if (originalPublicApiUrl === undefined) delete process.env.NEXT_PUBLIC_API_URL;
    else process.env.NEXT_PUBLIC_API_URL = originalPublicApiUrl;
  });

  test("passes the resolved landing clip to the interactive workspace", async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => [
        { key: "landing/clip-overnight.mp4", src: "https://cdn.example.com/demo.mp4" },
      ],
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const page = await TikTokProductPage();

    expect(fetchMock).toHaveBeenCalledWith(
      "https://api.example.com/landing-clips?keys=landing%2Fclip-overnight.mp4",
      { cache: "no-store", signal: expect.any(AbortSignal) },
    );
    expect(page.props.videoSrc).toBe("https://cdn.example.com/demo.mp4");
  });

  test("falls back when the clip service returns a non-success response", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: false }) as unknown as typeof fetch;

    const page = await TikTokProductPage();

    expect(page.props.videoSrc).toBeNull();
  });

  test("falls back when the clip service returns no usable rows", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => [],
    }) as unknown as typeof fetch;

    const page = await TikTokProductPage();

    expect(page.props.videoSrc).toBeNull();
  });

  test("fails open to the placeholder when the clip request throws", async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error("offline")) as unknown as typeof fetch;

    const page = await TikTokProductPage();

    expect(page.props.videoSrc).toBeNull();
  });

  test("fails open to the placeholder when the clip service stalls", async () => {
    jest.useFakeTimers();
    global.fetch = jest.fn(
      (_input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
        }),
    ) as unknown as typeof fetch;

    try {
      const pagePromise = TikTokProductPage();
      jest.advanceTimersByTime(4_000);
      const page = await pagePromise;

      expect(page.props.videoSrc).toBeNull();
    } finally {
      jest.useRealTimers();
    }
  });
});
