import { addJobToPlan, listMyJobs, NotAuthenticatedError } from "@/lib/me-api";

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

describe("me-api client", () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("listMyJobs hits /api/me/jobs and returns the page", async () => {
    const page = { jobs: [{ id: "j1" }], next_cursor: null };
    const fetchMock = jest.fn().mockResolvedValue(jsonResponse(200, page));
    global.fetch = fetchMock as unknown as typeof fetch;

    await expect(listMyJobs()).resolves.toEqual(page);
    expect(fetchMock).toHaveBeenCalledWith("/api/me/jobs", expect.any(Object));
  });

  it("listMyJobs forwards limit + cursor as query params", async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValue(jsonResponse(200, { jobs: [], next_cursor: null }));
    global.fetch = fetchMock as unknown as typeof fetch;

    await listMyJobs({ limit: 5, cursor: "2026-05-30T00:00:00+00:00" });
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("/api/me/jobs?");
    expect(url).toContain("limit=5");
    expect(url).toContain("cursor=2026-05-30");
  });

  it("addJobToPlan POSTs the day_index", async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValue(jsonResponse(200, { id: "j1", content_plan_item_id: "i1" }));
    global.fetch = fetchMock as unknown as typeof fetch;

    await addJobToPlan("j1", 3);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/me/jobs/j1/add-to-plan");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ day_index: 3 });
  });

  it("throws NotAuthenticatedError on 401", async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValue(jsonResponse(401, { detail: "Authentication required" })) as unknown as typeof fetch;

    await expect(listMyJobs()).rejects.toBeInstanceOf(NotAuthenticatedError);
  });

  it("surfaces the backend detail on other errors", async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValue(jsonResponse(404, { detail: "Plan day not found" })) as unknown as typeof fetch;

    await expect(addJobToPlan("j1", 99)).rejects.toThrow("Plan day not found");
  });
});
