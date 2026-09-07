import {
  createCreatorMemoryItem,
  getCreatorMemory,
  MemoryApiError,
  toggleCreatorMemory,
} from "@/lib/memory-api";

describe("creator Personalization API", () => {
  afterEach(() => jest.restoreAllMocks());

  it("normalizes the owner-scoped active item response into sections", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: jest.fn().mockResolvedValue({
        enabled: true,
        revision: 4,
        active: [{ id: "one", category: "video_style", instruction: "Use Playfair", enforcement: "constraint", state: "active" }],
        suggestions: [],
      }),
    });

    await expect(getCreatorMemory()).resolves.toMatchObject({
      enabled: true,
      revision: 4,
      items: [{ id: "one", section: "video_style", instruction: "Use Playfair" }],
    });
  });

  it("sends the revision and idempotency data for mutations", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: jest.fn().mockResolvedValue({ revision: 5 }),
    });

    await createCreatorMemoryItem({ instruction: "Always use Playfair", expected_revision: 4 });
    const [, init] = (global.fetch as jest.Mock).mock.calls[0];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body).toMatchObject({ instruction: "Always use Playfair", expected_revision: 4, category: "other", enforcement: "default" });
    expect(typeof body.idempotency_key).toBe("string");
  });

  it("preserves the safe creator-memory-disabled error code", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: jest.fn().mockResolvedValue({ detail: { code: "creator_memory_disabled", message: "Personalization is paused." } }),
    });

    await expect(toggleCreatorMemory(true, 4)).rejects.toMatchObject<Partial<MemoryApiError>>({
      code: "creator_memory_disabled",
      status: 503,
      message: "Personalization is paused.",
    });
  });
});
