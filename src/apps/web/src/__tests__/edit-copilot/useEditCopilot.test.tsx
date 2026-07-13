import { act, renderHook, waitFor } from "@testing-library/react";
import { editCopilotTurn } from "@/lib/plan-api";
import {
  editCopilotStorageKey,
  messagesToCopilotTurns,
  useEditCopilot,
  type CopilotMessage,
  type UseEditCopilotOptions,
} from "@/lib/edit-copilot/useEditCopilot";
import type { ApplyCopilotOpsResult } from "@/lib/edit-copilot/apply-ops";
import type { CopilotSnapshot } from "@/lib/edit-copilot/snapshot";
import type { EditCopilotTurnResponse } from "@/lib/plan-api";

jest.mock("@/lib/plan-api", () => ({
  editCopilotTurn: jest.fn(),
}));

const mockEditCopilotTurn = editCopilotTurn as jest.MockedFunction<
  typeof editCopilotTurn
>;

function snapshot(label: string): CopilotSnapshot {
  return {
    text_bars: [],
    slots: [],
    has_narrated_captions: false,
    total_duration_s: 0,
    max_duration_s: 60,
    remaining_duration_s: 60,
    allowed_op_families: ["text", "clip"],
    // Test-only marker; snapshots are JSON and the route accepts arbitrary keys.
    marker: label,
  } as CopilotSnapshot & { marker: string };
}

function response(
  over: Partial<EditCopilotTurnResponse> = {},
): EditCopilotTurnResponse {
  return {
    intent: "edit",
    ops: [],
    confidence: 0.9,
    reply: "Done",
    suggestions: [],
    needs_clarification: false,
    ...over,
  };
}

function appliedResult(
  over: Partial<ApplyCopilotOpsResult> = {},
): ApplyCopilotOpsResult {
  return {
    textActions: [],
    nextSlots: null,
    applied: [],
    rejected: [],
    ...over,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function storedMessages(itemId: string, variantId: string): CopilotMessage[] {
  const raw = window.sessionStorage.getItem(
    editCopilotStorageKey(itemId, variantId),
  );
  if (!raw) return [];
  return JSON.parse(raw).messages;
}

function copilotOptions(
  over: Partial<UseEditCopilotOptions> = {},
): UseEditCopilotOptions {
  return {
    itemId: "item-1",
    variantId: "variant-1",
    buildSnapshot: jest.fn(() => snapshot("initial")),
    applyOps: jest.fn(() => appliedResult()),
    ...over,
  };
}

function renderCopilot(over: Partial<UseEditCopilotOptions> = {}) {
  return renderHook((props: UseEditCopilotOptions) => useEditCopilot(props), {
    initialProps: copilotOptions(over),
  });
}

beforeEach(() => {
  mockEditCopilotTurn.mockReset();
  window.sessionStorage.clear();
});

describe("messagesToCopilotTurns", () => {
  it("annotates assistant turns with applied and rejected outcome summaries", () => {
    expect(
      messagesToCopilotTurns([
        { id: "u", role: "user", text: "make it smaller" },
        {
          id: "a",
          role: "assistant",
          text: "Done\n\nCouldn't apply: Clip 2: missing",
          applied: ["Size: 64 → 54"],
          rejected: ["Clip 2: missing"],
        },
      ]),
    ).toEqual([
      { role: "user", content: "make it smaller" },
      {
        role: "assistant",
        content: "Done\n\nCouldn't apply: Clip 2: missing",
        applied: ["Size: 64 → 54"],
        rejected: ["Clip 2: missing"],
      },
    ]);
  });
});

describe("useEditCopilot", () => {
  it("queues one follow-up and sends it with a post-apply snapshot", async () => {
    const first = deferred<EditCopilotTurnResponse>();
    mockEditCopilotTurn
      .mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce(response({ reply: "second done" }));

    let draftLabel = "before";
    const buildSnapshot = jest.fn(() => snapshot(draftLabel));
    const applyOps = jest.fn(() => {
      draftLabel = "after-first-apply";
      return appliedResult({
        applied: [{ label: "Size", from: "64", to: "54" }],
      });
    });
    const { result } = renderCopilot({ buildSnapshot, applyOps });

    act(() => {
      void result.current.send("first");
    });
    await waitFor(() => expect(result.current.sending).toBe(true));

    act(() => {
      void result.current.send("second");
    });
    expect(result.current.queued?.text).toBe("second");

    await act(async () => {
      first.resolve(
        response({ ops: [{ op: "edit_text", bar_index: 0, text: "x" }] }),
      );
      await first.promise;
    });

    await waitFor(() => expect(mockEditCopilotTurn).toHaveBeenCalledTimes(2));
    expect(mockEditCopilotTurn.mock.calls[0][2].snapshot).toMatchObject({
      marker: "before",
    });
    expect(mockEditCopilotTurn.mock.calls[1][2].snapshot).toMatchObject({
      marker: "after-first-apply",
    });
  });

  it("restores input and does not append history on transport failure", async () => {
    mockEditCopilotTurn.mockRejectedValueOnce(new Error("network down"));
    const { result } = renderCopilot();

    await act(async () => {
      await result.current.send("try this");
    });

    expect(result.current.messages).toEqual([]);
    expect(result.current.restoredInput).toBe("try this");
    expect(result.current.error).toBe("network down");
  });

  it("appends rejected outcome suffixes and includes them in later turns", async () => {
    mockEditCopilotTurn
      .mockResolvedValueOnce(response({ reply: "I changed what I could" }))
      .mockResolvedValueOnce(response({ reply: "Follow-up done" }));
    const applyOps = jest
      .fn()
      .mockReturnValueOnce(
        appliedResult({
          rejected: [
            {
              op: "set_clip_duration",
              label: "Clip 2 duration",
              reason: "user_changed",
              detail: "clip duration was changed after Nova read it",
            },
          ],
        }),
      )
      .mockReturnValueOnce(appliedResult());
    const { result } = renderCopilot({ applyOps });

    await act(async () => {
      await result.current.send("cut clip 2");
    });
    expect(result.current.messages[1].text).toContain(
      "Couldn't apply: Clip 2 duration",
    );

    await act(async () => {
      await result.current.send("make it shorter then");
    });

    expect(mockEditCopilotTurn.mock.calls[1][2].turns).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          role: "assistant",
          rejected: [
            "Clip 2 duration: clip duration was changed after Nova read it",
          ],
        }),
      ]),
    );
  });

  it("round-trips the thread through sessionStorage", async () => {
    mockEditCopilotTurn.mockResolvedValueOnce(response({ reply: "Stored" }));
    const first = renderCopilot({ variantId: "variant-storage" });

    await act(async () => {
      await first.result.current.send("remember this");
    });
    await waitFor(() =>
      expect(
        window.sessionStorage.getItem(
          editCopilotStorageKey("item-1", "variant-storage"),
        ),
      ).toContain("remember this"),
    );
    first.unmount();

    const second = renderCopilot({ variantId: "variant-storage" });
    expect(second.result.current.messages.map((m) => m.text)).toEqual([
      "remember this",
      "Stored",
    ]);
  });

  it("keys threads by item id as well as variant id", async () => {
    mockEditCopilotTurn
      .mockResolvedValueOnce(response({ reply: "A stored" }))
      .mockResolvedValueOnce(response({ reply: "B stored" }));
    const first = renderCopilot({
      itemId: "item-a",
      variantId: "same-variant",
    });
    const second = renderCopilot({
      itemId: "item-b",
      variantId: "same-variant",
    });

    await act(async () => {
      await first.result.current.send("from item a");
    });
    await act(async () => {
      await second.result.current.send("from item b");
    });

    await waitFor(() =>
      expect(
        window.sessionStorage.getItem(
          editCopilotStorageKey("item-a", "same-variant"),
        ),
      ).toContain("from item a"),
    );
    expect(
      window.sessionStorage.getItem(
        editCopilotStorageKey("item-b", "same-variant"),
      ),
    ).toContain("from item b");
    expect(editCopilotStorageKey("item-a", "same-variant")).toBe(
      "nova-edit-copilot-thread:item-a:same-variant",
    );
  });

  it("refuses to read, write, or send while the variant id is unresolved", async () => {
    const { result } = renderCopilot({ variantId: "" });

    await act(async () => {
      await result.current.send("hello");
    });

    expect(mockEditCopilotTurn).not.toHaveBeenCalled();
    expect(result.current.messages).toEqual([]);
    expect(
      Array.from({ length: window.sessionStorage.length }, (_, index) =>
        window.sessionStorage.key(index),
      ),
    ).not.toContain("nova-edit-copilot-thread:item-1:");
    expect(window.sessionStorage.length).toBe(0);
  });

  it("does not leak variant A's thread into variant B's bucket on switch", async () => {
    mockEditCopilotTurn.mockResolvedValueOnce(response({ reply: "A done" }));
    const props = copilotOptions({ variantId: "variant-a" });
    const { result, rerender } = renderHook(
      (hookProps: UseEditCopilotOptions) => useEditCopilot(hookProps),
      { initialProps: props },
    );

    await act(async () => {
      await result.current.send("variant a text");
    });
    await waitFor(() =>
      expect(
        window.sessionStorage.getItem(
          editCopilotStorageKey("item-1", "variant-a"),
        ),
      ).toContain("variant a text"),
    );

    rerender({ ...props, variantId: "variant-b" });

    await waitFor(() => expect(result.current.messages).toEqual([]));
    expect(
      window.sessionStorage.getItem(
        editCopilotStorageKey("item-1", "variant-a"),
      ),
    ).toContain("variant a text");
    expect(
      window.sessionStorage.getItem(
        editCopilotStorageKey("item-1", "variant-b"),
      ) ?? "",
    ).not.toContain("variant a text");
  });

  it("restores variant B's own thread on switch", async () => {
    const variantBThread = [
      { id: "stored-u", role: "user", text: "variant b saved" },
      { id: "stored-a", role: "assistant", text: "B reply" },
    ];
    window.sessionStorage.setItem(
      editCopilotStorageKey("item-1", "variant-b"),
      JSON.stringify({ v: 1, messages: variantBThread }),
    );
    const props = copilotOptions({ variantId: "variant-a" });
    const { result, rerender } = renderHook(
      (hookProps: UseEditCopilotOptions) => useEditCopilot(hookProps),
      { initialProps: props },
    );

    rerender({ ...props, variantId: "variant-b" });

    await waitFor(() =>
      expect(result.current.messages).toEqual(variantBThread),
    );
  });

  it("appends the user bubble optimistically while the turn is in flight", async () => {
    const turn = deferred<EditCopilotTurnResponse>();
    mockEditCopilotTurn.mockReturnValueOnce(turn.promise);
    const { result } = renderCopilot();

    act(() => {
      void result.current.send("make it punchier");
    });

    await waitFor(() => expect(result.current.sending).toBe(true));
    expect(result.current.messages).toMatchObject([
      { role: "user", text: "make it punchier", pending: true },
    ]);

    await act(async () => {
      turn.resolve(response({ reply: "Punchier now" }));
      await turn.promise;
    });

    await waitFor(() => expect(result.current.sending).toBe(false));
    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[0]).toMatchObject({
      role: "user",
      text: "make it punchier",
    });
    expect(result.current.messages[0].pending).toBeUndefined();
    expect(result.current.messages[1]).toMatchObject({
      role: "assistant",
      text: "Punchier now",
    });
  });

  it("does not persist the pending user bubble", async () => {
    const turn = deferred<EditCopilotTurnResponse>();
    mockEditCopilotTurn.mockReturnValueOnce(turn.promise);
    const { result } = renderCopilot({ variantId: "variant-pending" });

    act(() => {
      void result.current.send("pending text");
    });

    await waitFor(() => {
      const raw = window.sessionStorage.getItem(
        editCopilotStorageKey("item-1", "variant-pending"),
      );
      expect(raw).not.toBeNull();
      expect(raw).not.toContain("pending text");
    });

    await act(async () => {
      turn.resolve(response({ reply: "Done pending" }));
      await turn.promise;
    });

    await waitFor(() =>
      expect(
        window.sessionStorage.getItem(
          editCopilotStorageKey("item-1", "variant-pending"),
        ),
      ).toContain("pending text"),
    );
    expect(
      storedMessages("item-1", "variant-pending").map(
        (message) => message.text,
      ),
    ).toEqual(["pending text", "Done pending"]);
  });

  it("stop removes the optimistic bubble and restores the input", async () => {
    const turn = deferred<EditCopilotTurnResponse>();
    mockEditCopilotTurn.mockReturnValueOnce(turn.promise);
    const { result } = renderCopilot();

    act(() => {
      void result.current.send("stop me");
    });
    await waitFor(() => expect(result.current.messages).toHaveLength(1));

    act(() => result.current.stop());

    expect(result.current.messages).toEqual([]);
    expect(result.current.restoredInput).toBe("stop me");

    await act(async () => {
      turn.resolve(response({ reply: "Too late" }));
      await turn.promise;
    });

    expect(result.current.messages).toEqual([]);
  });

  it("clears mirrored storage on explicit clear", async () => {
    mockEditCopilotTurn.mockResolvedValueOnce(response({ reply: "Stored" }));
    const { result } = renderCopilot({ variantId: "variant-clear" });

    await act(async () => {
      await result.current.send("persist");
    });
    act(() => result.current.clear());

    expect(result.current.messages).toEqual([]);
    expect(
      window.sessionStorage.getItem(
        editCopilotStorageKey("item-1", "variant-clear"),
      ),
    ).toBeNull();
  });
});
