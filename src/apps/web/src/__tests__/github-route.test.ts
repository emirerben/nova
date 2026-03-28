/**
 * Tests for the /api/architecture/github route handler.
 *
 * We mock next/server to avoid jsdom incompatibilities with NextRequest/NextResponse,
 * then test the GET handler logic directly.
 */

// Mock next/server before any imports
jest.mock("next/server", () => {
  class MockNextResponse {
    body: any;
    status: number;
    headers: Map<string, string>;

    constructor(body: any, init?: { status?: number }) {
      this.body = body;
      this.status = init?.status ?? 200;
      this.headers = new Map();
    }

    async json() {
      return JSON.parse(this.body);
    }

    static json(data: any, init?: { status?: number }) {
      return new MockNextResponse(JSON.stringify(data), init);
    }
  }

  return {
    NextRequest: class {
      url: string;
      constructor(url: string) {
        this.url = url;
      }
    },
    NextResponse: MockNextResponse,
  };
});

// Mock fetch globally
const mockFetch = jest.fn();
global.fetch = mockFetch;

const originalEnv = process.env;

beforeEach(() => {
  jest.resetModules();
  mockFetch.mockReset();
  process.env = { ...originalEnv, GITHUB_TOKEN: "test-token", GITHUB_REPO: "emirerben/nova" };
});

afterEach(() => {
  process.env = originalEnv;
});

async function importRoute() {
  return await import("@/app/api/architecture/github/route");
}

function makeRequest(params: Record<string, string>) {
  const url = new URL("http://localhost:3000/api/architecture/github");
  Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  return { url: url.toString() } as any;
}

describe("/api/architecture/github route", () => {
  test("issues endpoint returns issues for given label", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => [
        {
          title: "Fix scoring bug",
          html_url: "https://github.com/emirerben/nova/issues/1",
          number: 1,
          state: "open",
          created_at: "2026-03-25T00:00:00Z",
        },
      ],
    });

    const { GET } = await importRoute();
    const res = await GET(makeRequest({ type: "issues", label: "module:processing" }));
    const data = await res.json();

    expect(data.items).toHaveLength(1);
    expect(data.items[0].title).toBe("Fix scoring bug");
    expect(data.items[0].number).toBe(1);
  });

  test("commits endpoint returns commits for given path", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => [
        {
          sha: "abc1234567890",
          commit: {
            message: "fix: improve scoring\nDetails here",
            author: { name: "Emil", date: "2026-03-26T00:00:00Z" },
          },
          html_url: "https://github.com/emirerben/nova/commit/abc1234",
        },
      ],
    });

    const { GET } = await importRoute();
    const res = await GET(makeRequest({ type: "commits", path: "src/apps/api/app/pipeline" }));
    const data = await res.json();

    expect(data.items).toHaveLength(1);
    expect(data.items[0].sha).toBe("abc1234");
    expect(data.items[0].message).toBe("fix: improve scoring");
    expect(data.items[0].author).toBe("Emil");
  });

  test("returns empty array when no GITHUB_TOKEN", async () => {
    process.env.GITHUB_TOKEN = "";

    const { GET } = await importRoute();
    const res = await GET(makeRequest({ type: "issues", label: "module:upload" }));
    const data = await res.json();

    expect(data.items).toEqual([]);
    // Should NOT have called GitHub API
    expect(mockFetch).not.toHaveBeenCalled();
  });

  test("returns rateLimited flag on 403", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 403,
      json: async () => ({ message: "rate limit exceeded" }),
    });

    const { GET } = await importRoute();
    const res = await GET(makeRequest({ type: "issues", label: "module:clips" }));
    const data = await res.json();

    expect(data.items).toEqual([]);
    expect(data.rateLimited).toBe(true);
  });

  test("rejects invalid type param with 400", async () => {
    const { GET } = await importRoute();
    const res = await GET(makeRequest({ type: "invalid" }));

    expect(res.status).toBe(400);
    const data = await res.json();
    expect(data.error).toMatch(/invalid type/i);
  });
});
