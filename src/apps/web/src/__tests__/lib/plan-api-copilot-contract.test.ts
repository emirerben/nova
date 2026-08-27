import { editCopilotTurn } from "@/lib/plan-api";

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
      message: "stack all images",
      turns: [],
      snapshot: {} as never,
    });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(String(init.body))).toMatchObject({
      client_contract_version: 2,
      message: "stack all images",
    });
  });
});
