import { uploadGenerativeClip, uploadVoiceover } from "@/lib/generative-api";

type MockResponse = {
  ok: boolean;
  status: number;
  statusText?: string;
  json: () => Promise<unknown>;
};

function response(body: unknown, status = 200): MockResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: String(status),
    json: async () => body,
  };
}

describe("uploadGenerativeClip", () => {
  function installFetchMock(): jest.Mock {
    const fetchMock = jest.fn();
    Object.defineProperty(global, "fetch", {
      value: fetchMock,
      configurable: true,
      writable: true,
    });
    return fetchMock;
  }

  afterEach(() => {
    jest.restoreAllMocks();
    delete (global as { fetch?: typeof fetch }).fetch;
  });

  it("normalizes an audio-only MP4 voiceover and uploads it with create-only headers", async () => {
    const fetchMock = installFetchMock()
      .mockResolvedValueOnce(
        response({
          upload_url: "https://storage.example/voice",
          gcs_path: "voiceover-uploads/direct/u/b/voice.m4a",
          kind: "audio",
          content_type: "audio/mp4",
          upload_headers: { "x-goog-if-generation-match": "0" },
        }) as Response,
      )
      .mockResolvedValueOnce(response({}, 200) as Response);
    const file = new File(["audio"], "voice.mp4", { type: "video/mp4" });

    await expect(uploadVoiceover(file)).resolves.toEqual({
      gcs_path: "voiceover-uploads/direct/u/b/voice.m4a",
      kind: "audio",
    });

    const initBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(initBody).toEqual(
      expect.objectContaining({ filename: "voice.m4a", content_type: "audio/mp4" }),
    );
    expect(fetchMock.mock.calls[1][1]?.headers).toEqual({
      "Content-Type": "audio/mp4",
      "x-goog-if-generation-match": "0",
    });
  });

  it("wraps a recorded Blob with a stable filename before direct upload", async () => {
    const fetchMock = installFetchMock()
      .mockResolvedValueOnce(
        response({
          upload_url: "https://storage.example/voice",
          gcs_path: "voiceover-uploads/direct/u/b/voice.webm",
          kind: "audio",
          content_type: "audio/webm",
          upload_headers: {},
        }) as Response,
      )
      .mockResolvedValueOnce(response({}, 200) as Response);

    await uploadVoiceover(new Blob(["audio"], { type: "audio/webm" }));

    const initBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(initBody).toEqual(
      expect.objectContaining({ filename: "voiceover.webm", content_type: "audio/webm" }),
    );
  });

  it("uploads bytes straight to the signed GCS URL with the pinned content type", async () => {
    const fetchMock = installFetchMock()
      .mockResolvedValueOnce(
        response({
          upload_url: "https://storage.example/put",
          gcs_path: "dev-user/u/generative/b/clip.mov",
          kind: "video",
          content_type: "video/quicktime",
          upload_headers: { "x-goog-if-generation-match": "0" },
        }) as Response,
      )
      .mockResolvedValueOnce(response({}, 200) as Response);
    const file = new File(["video"], "clip.mov", { type: "video/quicktime" });

    const result = await uploadGenerativeClip(file);

    expect(result).toEqual({
      gcs_path: "dev-user/u/generative/b/clip.mov",
      kind: "video",
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://localhost:8000/generative-jobs/upload-url",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(2, "https://storage.example/put", {
      method: "PUT",
      headers: {
        "Content-Type": "video/quicktime",
        "x-goog-if-generation-match": "0",
      },
      body: file,
    });
    expect(fetchMock.mock.calls[0][1]?.body).not.toBeInstanceOf(FormData);
  });

  it("relays the same signed URL only when direct PUT is blocked by CORS", async () => {
    const fetchMock = installFetchMock()
      .mockResolvedValueOnce(
        response({
          upload_url: "https://storage.example/put",
          gcs_path: "dev-user/u/generative/b/clip.mp4",
          kind: "video",
          content_type: "video/mp4",
          upload_headers: { "x-goog-if-generation-match": "0" },
        }) as Response,
      )
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(response({}, 200) as Response);

    await uploadGenerativeClip(new File(["video"], "clip.mp4", { type: "video/mp4" }));

    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://localhost:8000/uploads/relay",
      expect.objectContaining({ method: "POST", body: expect.any(FormData) }),
    );
    const relayBody = fetchMock.mock.calls[2][1]?.body as FormData;
    expect(relayBody.get("file_size_bytes")).toBe(String(new Blob(["video"]).size));
    expect(relayBody.get("if_generation_match")).toBe("0");
  });

  it("falls back to the legacy multipart endpoint during an old-API rollout", async () => {
    const fetchMock = installFetchMock()
      .mockResolvedValueOnce(response({ detail: "Not Found" }, 404) as Response)
      .mockResolvedValueOnce(
        response({ gcs_path: "music-uploads/legacy/slot.mp4", kind: "video" }, 201) as Response,
      );

    const result = await uploadGenerativeClip(
      new File(["video"], "clip.mp4", { type: "video/mp4" }),
    );

    expect(result.gcs_path).toBe("music-uploads/legacy/slot.mp4");
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://localhost:8000/music-jobs/upload-slot",
      expect.objectContaining({ method: "POST", body: expect.any(FormData) }),
    );
  });

  it("starts no more than two signed uploads at once", async () => {
    const releaseInitialPuts: Array<() => void> = [];
    let initCalls = 0;
    let activePuts = 0;
    let maxActivePuts = 0;
    const fetchMock = installFetchMock().mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/generative-jobs/upload-url")) {
        initCalls += 1;
        return response({
          upload_url: `https://storage.example/put-${initCalls}`,
          gcs_path: `dev-user/u/generative/b/clip-${initCalls}.mp4`,
          kind: "video",
          content_type: "video/mp4",
        }) as Response;
      }
      activePuts += 1;
      maxActivePuts = Math.max(maxActivePuts, activePuts);
      if (url.endsWith("put-1") || url.endsWith("put-2")) {
        await new Promise<void>((resolve) => releaseInitialPuts.push(resolve));
      }
      activePuts -= 1;
      return response({}, 200) as Response;
    });
    const files = [1, 2, 3].map(
      (i) => new File([`video-${i}`], `clip-${i}.mp4`, { type: "video/mp4" }),
    );

    const uploads = files.map((file) => uploadGenerativeClip(file));
    for (let i = 0; i < 10 && releaseInitialPuts.length < 2; i += 1) await Promise.resolve();
    expect(initCalls).toBe(2);
    expect(maxActivePuts).toBe(2);

    releaseInitialPuts.splice(0).forEach((release) => release());
    await Promise.all(uploads);
    expect(initCalls).toBe(3);
    expect(maxActivePuts).toBe(2);
    expect(fetchMock).toHaveBeenCalledTimes(6);
  });

  it("releases its upload slot after a failed PUT", async () => {
    const fetchMock = installFetchMock()
      .mockResolvedValueOnce(
        response({
          upload_url: "https://storage.example/fail",
          gcs_path: "dev-user/u/generative/a/clip.mp4",
          kind: "video",
          content_type: "video/mp4",
        }) as Response,
      )
      .mockResolvedValueOnce(response({}, 500) as Response)
      .mockResolvedValueOnce(
        response({
          upload_url: "https://storage.example/pass",
          gcs_path: "dev-user/u/generative/b/clip.mp4",
          kind: "video",
          content_type: "video/mp4",
        }) as Response,
      )
      .mockResolvedValueOnce(response({}, 200) as Response);
    const file = new File(["video"], "clip.mp4", { type: "video/mp4" });

    await expect(uploadGenerativeClip(file)).rejects.toThrow("Upload failed (500)");
    await expect(uploadGenerativeClip(file)).resolves.toEqual({
      gcs_path: "dev-user/u/generative/b/clip.mp4",
      kind: "video",
    });
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });
});
