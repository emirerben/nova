import { editCopilotTurn, expandIdea } from "@/lib/plan-api";

describe("editCopilotTurn contract negotiation", () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("requests proposal/staged lifecycle contract v2", async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        intent: "edit",
        ops: [],
        confidence: 1,
        reply: "Ready.",
        suggestions: [],
        needs_clarification: false,
        outcome: "proposed",
      }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    await editCopilotTurn("item-1", "variant-1", {
      client_request_id: "turn-1",
      message: "stack all images",
      turns: [],
      snapshot: {} as never,
    });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(String(init.body))).toMatchObject({
      client_contract_version: 2,
      client_request_id: "turn-1",
      message: "stack all images",
    });
  });

  it("preserves an idea-expansion intent ID across transport retries", async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    await expandIdea("item-1", {
      creator_context: "make it calm",
      client_request_id: "expand-intent-1",
    });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({
      creator_context: "make it calm",
      client_request_id: "expand-intent-1",
    });
  });
});
