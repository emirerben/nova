export {};

const mockGetServerSession = jest.fn();
let mockThrowNextResponseConstruction = false;

jest.mock("next-auth", () => ({
  getServerSession: mockGetServerSession,
}));

jest.mock("@/lib/auth", () => ({ authOptions: {} }));

jest.mock("next/server", () => {
  class MockNextResponse {
    body: unknown;
    status: number;
    headers: unknown;

    constructor(body: unknown, init?: { status?: number; headers?: unknown }) {
      const status = init?.status ?? 200;
      if (mockThrowNextResponseConstruction && body instanceof ArrayBuffer) {
        throw new TypeError("response construction failed");
      }
      if ([204, 205, 304].includes(status) && body !== null) {
        throw new TypeError(`Invalid response status code ${status}`);
      }
      this.body = body;
      this.status = status;
      this.headers = init?.headers;
    }

    static json(data: unknown, init?: { status?: number }) {
      return new MockNextResponse(JSON.stringify(data), init);
    }
  }

  return { NextRequest: class {}, NextResponse: MockNextResponse };
});

const originalEnv = process.env;
const mockFetch = jest.fn();

beforeEach(() => {
  jest.resetModules();
  mockFetch.mockReset();
  mockGetServerSession.mockReset();
  mockThrowNextResponseConstruction = false;
  process.env = {
    ...originalEnv,
    API_URL: "https://api.example.test",
    INTERNAL_API_KEY: "test-internal-key",
  };
  global.fetch = mockFetch as unknown as typeof fetch;
});

afterEach(() => {
  jest.restoreAllMocks();
  process.env = originalEnv;
});

describe("authenticated proxy response transport", () => {
  it("accepts a successful 204 without trying to parse JSON", async () => {
    const json = jest.fn().mockRejectedValue(new SyntaxError("Unexpected end of JSON input"));
    mockFetch.mockResolvedValueOnce({ ok: true, status: 204, json });

    const { deleteIdea } = await import("@/lib/plan-api");

    await expect(deleteIdea("idea-1")).resolves.toBeUndefined();
    expect(json).not.toHaveBeenCalled();
    expect(mockFetch).toHaveBeenCalledWith(
      "/api/plan/plan-items/idea-1",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it.each([204, 205, 304])("proxies status %s with a null body", async (status) => {
    const arrayBuffer = jest.fn().mockResolvedValue(new ArrayBuffer(0));
    const headers = new Headers({
      "content-type": "application/json",
      etag: '"idea-delete-v1"',
      "x-request-id": "req-1",
    });
    mockFetch.mockResolvedValueOnce({
      status,
      arrayBuffer,
      headers,
    });
    mockGetServerSession.mockResolvedValueOnce({ user: { id: "user-1" } });

    const { makeProxyHandlers } = await import("@/lib/api-proxy");
    const request = {
      method: "DELETE",
      nextUrl: { search: "" },
      headers: { get: () => null },
      arrayBuffer: jest.fn().mockResolvedValue(new ArrayBuffer(0)),
    };

    const response = await makeProxyHandlers().DELETE(
      request as never,
      { params: Promise.resolve({ path: ["plan-items", "idea-1"] }) },
    );

    expect(response.status).toBe(status);
    expect((response as unknown as { body: unknown }).body).toBeNull();
    expect((response as unknown as { headers: HeadersInit }).headers).toEqual({
      "Content-Type": "application/json",
      "X-Correlation-Id": expect.any(String),
      "X-Request-Id": expect.any(String),
    });
    expect(arrayBuffer).not.toHaveBeenCalled();
  });

  it("strips stale compression metadata from a decoded 200 response", async () => {
    const decodedBody = new Uint8Array(Buffer.from('{"ok":true}', "utf8")).buffer;
    const arrayBuffer = jest.fn().mockResolvedValue(decodedBody);
    const headers = new Headers({
      "cache-control": "private, max-age=0",
      "content-encoding": "zstd",
      "content-length": "4096",
      "content-type": "application/json",
      "set-cookie": "internal=value",
      "transfer-encoding": "chunked",
    });
    mockFetch.mockResolvedValueOnce({
      status: 200,
      arrayBuffer,
      headers,
    });
    mockGetServerSession.mockResolvedValueOnce({ user: { id: "user-1" } });

    const { makeProxyHandlers } = await import("@/lib/api-proxy");
    const request = {
      method: "GET",
      nextUrl: { search: "" },
      headers: { get: () => null },
      arrayBuffer: jest.fn(),
    };

    const response = await makeProxyHandlers().GET(
      request as never,
      { params: Promise.resolve({ path: ["personas"] }) },
    );

    expect(response.status).toBe(200);
    expect((response as unknown as { body: unknown }).body).toBe(decodedBody);
    const forwardedHeaders = new Headers(
      (response as unknown as { headers: HeadersInit }).headers,
    );
    expect(Object.fromEntries(forwardedHeaders.entries())).toEqual({
      "content-type": "application/json",
      "x-correlation-id": expect.any(String),
      "x-request-id": expect.any(String),
    });
    expect(forwardedHeaders.get("content-encoding")).toBeNull();
    expect(forwardedHeaders.get("content-length")).toBeNull();
    expect(forwardedHeaders.get("transfer-encoding")).toBeNull();
    expect(forwardedHeaders.get("set-cookie")).toBeNull();
    expect(forwardedHeaders.get("cache-control")).toBeNull();
    expect(arrayBuffer).toHaveBeenCalledTimes(1);
  });

  it("proxies HEAD responses with a null body", async () => {
    const arrayBuffer = jest.fn().mockResolvedValue(new ArrayBuffer(0));
    const headers = new Headers({
      "cache-control": "private, max-age=0",
      "content-type": "application/json",
    });
    mockFetch.mockResolvedValueOnce({
      status: 200,
      arrayBuffer,
      headers,
    });
    mockGetServerSession.mockResolvedValueOnce({ user: { id: "user-1" } });

    const { makeProxyHandlers } = await import("@/lib/api-proxy");
    const request = {
      method: "HEAD",
      nextUrl: { search: "" },
      headers: { get: () => null },
      arrayBuffer: jest.fn(),
    };

    const response = await makeProxyHandlers().GET(
      request as never,
      { params: Promise.resolve({ path: ["content-plans"] }) },
    );

    expect(response.status).toBe(200);
    expect((response as unknown as { body: unknown }).body).toBeNull();
    expect((response as unknown as { headers: HeadersInit }).headers).toEqual({
      "Content-Type": "application/json",
      "X-Correlation-Id": expect.any(String),
      "X-Request-Id": expect.any(String),
    });
    expect(arrayBuffer).not.toHaveBeenCalled();
  });

  it("returns a correlated, user-safe 502 when the upstream fetch fails", async () => {
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    mockFetch.mockRejectedValueOnce(new TypeError("socket closed"));
    mockGetServerSession.mockResolvedValueOnce({ user: { id: "user-1" } });
    const { makeProxyHandlers } = await import("@/lib/api-proxy");
    const request = {
      method: "GET",
      nextUrl: { search: "" },
      headers: { get: (name: string) => (name === "x-request-id" ? "req-fetch" : null) },
      arrayBuffer: jest.fn(),
    };

    const response = await makeProxyHandlers().GET(
      request as never,
      { params: Promise.resolve({ path: ["plan-items", "item-1"] }) },
    );

    expect(response.status).toBe(502);
    expect(JSON.parse(String((response as unknown as { body: unknown }).body))).toEqual({
      detail: "Kria couldn't reach the video service. Retry in a moment.",
      code: "upstream_unavailable",
      stage: "upstream_fetch",
      retryable: true,
      request_id: "req-fetch",
      correlation_id: "req-fetch",
    });
    expect(errorSpy).toHaveBeenCalledWith(
      "[api-proxy] boundary failure",
      expect.objectContaining({
        correlationId: "req-fetch",
        stage: "upstream_fetch",
        errorCode: "upstream_unavailable",
      }),
    );
  });

  it("logs correlated configuration failures with a safe stage and code", async () => {
    process.env.INTERNAL_API_KEY = "";
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    mockGetServerSession.mockResolvedValueOnce({ user: { id: "user-1" } });
    const { makeProxyHandlers } = await import("@/lib/api-proxy");
    const request = {
      method: "GET",
      nextUrl: { search: "" },
      headers: { get: (name: string) => (name === "x-correlation-id" ? "batch-config" : null) },
      arrayBuffer: jest.fn(),
    };
    const response = await makeProxyHandlers().GET(
      request as never,
      { params: Promise.resolve({ path: ["plan-items", "item-1"] }) },
    );
    expect(response.status).toBe(500);
    expect(errorSpy).toHaveBeenCalledWith(
      "[api-proxy] INTERNAL_API_KEY missing",
      expect.objectContaining({
        correlationId: "batch-config",
        stage: "proxy_config",
        errorCode: "server_misconfigured",
      }),
    );
  });

  it("returns a correlated 502 when reading the incoming request body fails", async () => {
    mockGetServerSession.mockResolvedValueOnce({ user: { id: "user-1" } });
    const { makeProxyHandlers } = await import("@/lib/api-proxy");
    const request = {
      method: "POST",
      nextUrl: { search: "" },
      headers: { get: (name: string) => (name === "x-correlation-id" ? "batch-body" : null) },
      arrayBuffer: jest.fn().mockRejectedValue(new TypeError("body stream closed")),
    };

    const response = await makeProxyHandlers().POST(
      request as never,
      { params: Promise.resolve({ path: ["plan-items", "item-1", "assets"] }) },
    );
    const body = JSON.parse(String((response as unknown as { body: unknown }).body));
    expect(response.status).toBe(502);
    expect(body).toMatchObject({
      code: "upstream_unavailable",
      stage: "request_body",
      correlation_id: "batch-body",
    });
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("sanitizes an arbitrary upstream 500 body instead of forwarding it", async () => {
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    const arrayBuffer = jest.fn().mockResolvedValue(Buffer.from("Internal Server Error"));
    mockFetch.mockResolvedValueOnce({
      status: 500,
      arrayBuffer,
      headers: new Headers({ "content-type": "text/plain" }),
    });
    mockGetServerSession.mockResolvedValueOnce({ user: { id: "user-1" } });
    const { makeProxyHandlers } = await import("@/lib/api-proxy");
    const request = {
      method: "POST",
      nextUrl: { search: "" },
      headers: { get: () => null },
      arrayBuffer: jest.fn().mockResolvedValue(new ArrayBuffer(0)),
    };

    const response = await makeProxyHandlers().POST(
      request as never,
      { params: Promise.resolve({ path: ["plan-items", "item-1", "assets"] }) },
    );
    const body = JSON.parse(String((response as unknown as { body: unknown }).body));
    expect(response.status).toBe(500);
    expect(body.detail).toBe("Kria couldn't complete that request. Retry in a moment.");
    expect(body.detail).not.toMatch(/internal server error/i);
    expect(body.request_id).toEqual(expect.any(String));
    expect(arrayBuffer).toHaveBeenCalledTimes(1);
    expect(errorSpy).toHaveBeenCalledWith(
      "[api-proxy] upstream server error",
      expect.objectContaining({
        stage: "upstream_response",
        errorCode: "upstream_error",
      }),
    );
  });

  it("stays generic for a 5xx JSON body whose code isn't on the pass-through whitelist", async () => {
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    const detail = { detail: { code: "some_other_failure", message: "Raw internal detail." } };
    const arrayBuffer = jest.fn().mockResolvedValue(Buffer.from(JSON.stringify(detail)));
    mockFetch.mockResolvedValueOnce({
      status: 500,
      arrayBuffer,
      headers: new Headers({ "content-type": "application/json" }),
    });
    mockGetServerSession.mockResolvedValueOnce({ user: { id: "user-1" } });
    const { makeProxyHandlers } = await import("@/lib/api-proxy");
    const request = {
      method: "POST",
      nextUrl: { search: "" },
      headers: { get: () => null },
      arrayBuffer: jest.fn().mockResolvedValue(new ArrayBuffer(0)),
    };

    const response = await makeProxyHandlers().POST(
      request as never,
      { params: Promise.resolve({ path: ["plan-items", "item-1", "edit-proposal", "conversation"] }) },
    );
    const body = JSON.parse(String((response as unknown as { body: unknown }).body));
    expect(response.status).toBe(500);
    expect(body.detail).toBe("Kria couldn't complete that request. Retry in a moment.");
    expect(body).not.toMatchObject({ detail: "Raw internal detail." });
    expect(errorSpy).toHaveBeenCalledWith(
      "[api-proxy] upstream server error",
      expect.objectContaining({ errorCode: "upstream_error" }),
    );
  });

  it("passes through a whitelisted guided-edit 5xx code's message verbatim", async () => {
    jest.spyOn(console, "error").mockImplementation(() => {});
    const detail = {
      detail: { code: "edit_guide_failed", message: "Kria couldn't finish planning this edit." },
    };
    const arrayBuffer = jest.fn().mockResolvedValue(Buffer.from(JSON.stringify(detail)));
    mockFetch.mockResolvedValueOnce({
      status: 502,
      arrayBuffer,
      headers: new Headers({ "content-type": "application/json" }),
    });
    mockGetServerSession.mockResolvedValueOnce({ user: { id: "user-1" } });
    const { makeProxyHandlers } = await import("@/lib/api-proxy");
    const request = {
      method: "POST",
      nextUrl: { search: "" },
      headers: { get: () => null },
      arrayBuffer: jest.fn().mockResolvedValue(new ArrayBuffer(0)),
    };

    const response = await makeProxyHandlers().POST(
      request as never,
      { params: Promise.resolve({ path: ["plan-items", "item-1", "edit-proposal", "conversation"] }) },
    );
    const body = JSON.parse(String((response as unknown as { body: unknown }).body));
    expect(response.status).toBe(502);
    expect(body).toEqual({
      detail: "Kria couldn't finish planning this edit.",
      code: "edit_guide_failed",
      retryable: true,
    });
  });

  it("caps a whitelisted pass-through message at 300 characters", async () => {
    jest.spyOn(console, "error").mockImplementation(() => {});
    const longMessage = "x".repeat(400);
    const detail = { detail: { code: "proposal_dispatch_failed", message: longMessage } };
    const arrayBuffer = jest.fn().mockResolvedValue(Buffer.from(JSON.stringify(detail)));
    mockFetch.mockResolvedValueOnce({
      status: 500,
      arrayBuffer,
      headers: new Headers({ "content-type": "application/json" }),
    });
    mockGetServerSession.mockResolvedValueOnce({ user: { id: "user-1" } });
    const { makeProxyHandlers } = await import("@/lib/api-proxy");
    const request = {
      method: "POST",
      nextUrl: { search: "" },
      headers: { get: () => null },
      arrayBuffer: jest.fn().mockResolvedValue(new ArrayBuffer(0)),
    };

    const response = await makeProxyHandlers().POST(
      request as never,
      { params: Promise.resolve({ path: ["plan-items", "item-1", "edit-proposal", "conversation"] }) },
    );
    const body = JSON.parse(String((response as unknown as { body: unknown }).body));
    expect(body.code).toBe("proposal_dispatch_failed");
    expect(body.detail).toHaveLength(300);
  });

  it("preserves an actionable upstream 4xx body", async () => {
    const detail = {
      detail: "Images must be 25 MB or smaller.",
      code: "upload_too_large",
      stage: "validation",
      retryable: false,
    };
    mockFetch.mockResolvedValueOnce({
      status: 422,
      arrayBuffer: jest.fn().mockResolvedValue(Buffer.from(JSON.stringify(detail))),
      body: null,
      headers: new Headers({ "content-type": "application/json" }),
    });
    mockGetServerSession.mockResolvedValueOnce({ user: { id: "user-1" } });
    const { makeProxyHandlers } = await import("@/lib/api-proxy");
    const request = {
      method: "POST",
      nextUrl: { search: "" },
      headers: { get: () => null },
      arrayBuffer: jest.fn().mockResolvedValue(new ArrayBuffer(0)),
    };

    const response = await makeProxyHandlers().POST(
      request as never,
      { params: Promise.resolve({ path: ["plan-items", "item-1", "assets"] }) },
    );
    expect(response.status).toBe(422);
    expect(JSON.parse(String((response as unknown as { body: unknown }).body))).toEqual(detail);
  });

  it("turns an upstream response-read failure into a correlated 502", async () => {
    mockFetch.mockResolvedValueOnce({
      status: 200,
      arrayBuffer: jest.fn().mockRejectedValue(new TypeError("decoded body unavailable")),
      headers: new Headers({ "content-type": "application/json" }),
    });
    mockGetServerSession.mockResolvedValueOnce({ user: { id: "user-1" } });
    const { makeProxyHandlers } = await import("@/lib/api-proxy");
    const request = {
      method: "GET",
      nextUrl: { search: "" },
      headers: { get: () => null },
      arrayBuffer: jest.fn(),
    };
    const response = await makeProxyHandlers().GET(
      request as never,
      { params: Promise.resolve({ path: ["plan-items", "item-1"] }) },
    );
    expect(response.status).toBe(502);
    const body = JSON.parse(String((response as unknown as { body: unknown }).body));
    expect(body.code).toBe("upstream_unavailable");
    expect(body.request_id).toEqual(expect.any(String));
  });

  it("turns response construction failure into a correlated safe 502", async () => {
    mockThrowNextResponseConstruction = true;
    mockFetch.mockResolvedValueOnce({
      status: 200,
      arrayBuffer: jest.fn().mockResolvedValue(new ArrayBuffer(8)),
      headers: new Headers({ "content-type": "application/json" }),
    });
    mockGetServerSession.mockResolvedValueOnce({ user: { id: "user-1" } });
    const { makeProxyHandlers } = await import("@/lib/api-proxy");
    const request = {
      method: "GET",
      nextUrl: { search: "" },
      headers: { get: () => null },
      arrayBuffer: jest.fn(),
    };
    const response = await makeProxyHandlers().GET(
      request as never,
      { params: Promise.resolve({ path: ["plan-items", "item-1"] }) },
    );
    expect(response.status).toBe(502);
    const body = JSON.parse(String((response as unknown as { body: unknown }).body));
    expect(body).toMatchObject({ code: "upstream_unavailable", stage: "response_finalize" });
  });
});
