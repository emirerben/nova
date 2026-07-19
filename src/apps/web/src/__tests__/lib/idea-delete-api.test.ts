export {};

const mockGetServerSession = jest.fn();

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
  process.env = {
    ...originalEnv,
    API_URL: "https://api.example.test",
    INTERNAL_API_KEY: "test-internal-key",
  };
  global.fetch = mockFetch as unknown as typeof fetch;
});

afterEach(() => {
  process.env = originalEnv;
});

describe("idea deletion transport", () => {
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
    const headers = new Headers({ etag: '"idea-delete-v1"', "x-request-id": "req-1" });
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
    expect((response as unknown as { headers: Headers }).headers).toBe(headers);
    expect(arrayBuffer).not.toHaveBeenCalled();
  });

  it("proxies HEAD responses with a null body", async () => {
    const arrayBuffer = jest.fn().mockResolvedValue(new ArrayBuffer(0));
    const headers = new Headers({ "cache-control": "private, max-age=0" });
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
    expect((response as unknown as { headers: Headers }).headers).toBe(headers);
    expect(arrayBuffer).not.toHaveBeenCalled();
  });
});
