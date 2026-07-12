import { act, renderHook, waitFor } from "@testing-library/react";
import { editCopilotTurn } from "@/lib/plan-api";
import {
  messagesToCopilotTurns,
  useEditCopilot,
  type UseEditCopilotOptions,
} from "@/lib/edit-copilot/useEditCopilot";
import type { ApplyCopilotOpsResult } from "@/lib/edit-copilot/apply-ops";
import type { CopilotSnapshot } from "@/lib/edit-copilot/snapshot";
import type { EditCopilotTurnResponse } from "@/lib/plan-api";

jest.mock("@/lib/plan-api", () => ({
  editCopilotTurn: jest.fn(),
}));

const mockEditCopilotTurn = editCopilotTurn as jest.MockedFunction<typeof editCopilotTurn>;

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

function response(over: Partial<EditCopilotTurnResponse> = {}): EditCopilotTurnResponse {
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

function appliedResult(over: Partial<ApplyCopilotOpsResult> = {}): ApplyCopilotOpsResult {
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

function renderCopilot(over: Partial<UseEditCopilotOptions> = {}) {
  const defaults: UseEditCopilotOptions = {
    itemId: "item-1",
    variantId: "variant-1",
    buildSnapshot: jest.fn(() => snapshot("initial")),
    applyOps: jest.fn(() => appliedResult()),
  };
  return renderHook((props: UseEditCopilotOptions) => useEditCopilot(props), {
    initialProps: { ...defaults, ...over },
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
          applied: ["Size: 64 -> 54"],
          rejected: ["Clip 2: missing"],
        },
      ]),
    ).toEqual([
      { role: "user", content: "make it smaller" },
      {
        role: "assistant",
        content: "Done\n\nCouldn't apply: Clip 2: missing",
        applied: ["Size: 64 -> 54"],
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
      first.resolve(response({ ops: [{ op: "edit_text", bar_index: 0, text: "x" }] }));
      await first.promise;
    });

    await waitFor(() => expect(mockEditCopilotTurn).toHaveBeenCalledTimes(2));
    expect(mockEditCopilotTurn.mock.calls[0][2].snapshot).toMatchObject({ marker: "before" });
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
    expect(result.current.messages[1].text).toContain("Couldn't apply: Clip 2 duration");

    await act(async () => {
      await result.current.send("make it shorter then");
    });

    expect(mockEditCopilotTurn.mock.calls[1][2].turns).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          role: "assistant",
          rejected: ["Clip 2 duration: clip duration was changed after Nova read it"],
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
      expect(window.sessionStorage.getItem("nova-edit-copilot-thread:variant-storage")).toContain(
        "remember this",
      ),
    );
    first.unmount();

    const second = renderCopilot({ variantId: "variant-storage" });
    expect(second.result.current.messages.map((m) => m.text)).toEqual([
      "remember this",
      "Stored",
    ]);
  });

  it("clears mirrored storage on explicit clear", async () => {
    mockEditCopilotTurn.mockResolvedValueOnce(response({ reply: "Stored" }));
    const { result } = renderCopilot({ variantId: "variant-clear" });

    await act(async () => {
      await result.current.send("persist");
    });
    act(() => result.current.clear());

    expect(result.current.messages).toEqual([]);
    expect(window.sessionStorage.getItem("nova-edit-copilot-thread:variant-clear")).toBeNull();
  });
});
