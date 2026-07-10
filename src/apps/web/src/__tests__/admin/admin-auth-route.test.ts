/**
 * Tests for the /api/admin-auth route handler.
 *
 * Mocks next/server to avoid jsdom incompatibilities with NextRequest /
 * NextResponse, then drives POST directly. Covers the three resolution
 * branches: missing env (500), wrong token (401), right token (200).
 *
 * Mirrors the pattern in github-route.test.ts.
 */

export {};

jest.mock("next/server", () => {
  class MockNextResponse {
    body: string;
    status: number;
    headers: Map<string, string>;

    constructor(body: string, init?: { status?: number }) {
      this.body = body;
      this.status = init?.status ?? 200;
      this.headers = new Map();
    }

    async json() {
      return JSON.parse(this.body);
    }

    static json(data: unknown, init?: { status?: number }) {
      return new MockNextResponse(JSON.stringify(data), init);
    }
  }

  return {
    NextRequest: class {
      private _body: string;
      constructor(body: string) {
        this._body = body;
      }
      async json() {
        return JSON.parse(this._body);
      }
    },
    NextResponse: MockNextResponse,
  };
});

const originalEnv = process.env;

beforeEach(() => {
  jest.resetModules();
  process.env = { ...originalEnv };
  delete process.env.ADMIN_TOKEN;
});

afterEach(() => {
  process.env = originalEnv;
});

async function importRoute() {
  return await import("@/app/api/admin-auth/route");
}

function makeRequest(body: unknown) {
  // The route handler only calls req.json(); reuse the NextRequest mock.
  const { NextRequest } = require("next/server");
  return new NextRequest(JSON.stringify(body));
}

describe("/api/admin-auth route", () => {
  test("returns 500 when ADMIN_TOKEN is not configured", async () => {
    // ADMIN_TOKEN intentionally unset.
    const { POST } = await importRoute();
    const res = await POST(makeRequest({ token: "anything" }));
    expect(res.status).toBe(500);
    const data = await res.json();
    expect(data.detail).toMatch(/not configured/i);
  });

  test("returns 401 when token does not match", async () => {
    process.env.ADMIN_TOKEN = "the-real-token";
    const { POST } = await importRoute();
    const res = await POST(makeRequest({ token: "wrong" }));
    expect(res.status).toBe(401);
    const data = await res.json();
    expect(data.ok).toBe(false);
  });

  test("returns 200 when token matches exactly", async () => {
    process.env.ADMIN_TOKEN = "the-real-token";
    const { POST } = await importRoute();
    const res = await POST(makeRequest({ token: "the-real-token" }));
    expect(res.status).toBe(200);
    const data = await res.json();
    expect(data.ok).toBe(true);
  });

  test("returns 401 when token has matching prefix but different length", async () => {
    // Guards against `timingSafeEqual` throwing on length mismatch — the route
    // does an explicit length check first so a length-mismatched candidate
    // falls through to 401 cleanly.
    process.env.ADMIN_TOKEN = "abc123";
    const { POST } = await importRoute();
    const res = await POST(makeRequest({ token: "abc1234" }));
    expect(res.status).toBe(401);
  });

  test("returns 401 when body has no token field", async () => {
    process.env.ADMIN_TOKEN = "the-real-token";
    const { POST } = await importRoute();
    const res = await POST(makeRequest({}));
    expect(res.status).toBe(401);
  });
});
