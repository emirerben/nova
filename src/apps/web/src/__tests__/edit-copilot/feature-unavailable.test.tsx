/**
 * Regression cover for the "chatbox shows an unexplained error" bug.
 *
 * The editor gates Nova on NEXT_PUBLIC_EDIT_* while the API gates the same
 * routes on EDIT_*. When only the UI half is on, every copilot turn and every
 * proactive director review 404s. Before this fix both surfaces collapsed the
 * 404 into hardcoded "couldn't reach Nova" copy, and the director re-fired on
 * every material revision, so the drawer showed a failure the user could
 * neither explain nor escape.
 */
import "@testing-library/jest-dom";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import CopilotDrawer from "@/app/plan/items/[id]/_editor/CopilotDrawer";
import {
  FeatureDisabledError,
  editCopilotTurn,
  editDirectorSuggestions,
} from "@/lib/plan-api";
import { isFeatureUnavailable } from "@/lib/edit-copilot/availability";
import {
  COPILOT_GENERIC_ERROR,
  COPILOT_UNAVAILABLE_MESSAGE,
  copilotErrorMessage,
  useEditCopilot,
} from "@/lib/edit-copilot/useEditCopilot";
import {
  DIRECTOR_UNAVAILABLE_MESSAGE,
  useEditDirector,
} from "@/lib/edit-copilot/useEditDirector";
import type { CopilotMessage } from "@/lib/edit-copilot/useEditCopilot";
import type { CopilotSnapshot } from "@/lib/edit-copilot/snapshot";

// Keep FeatureDisabledError real so `instanceof` still identifies it; only the
// network-touching helpers are stubbed.
jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  editCopilotTurn: jest.fn(),
  editDirectorSuggestions: jest.fn(),
  editDirectorFeedback: jest.fn(),
}));

const turnMock = editCopilotTurn as jest.MockedFunction<typeof editCopilotTurn>;
const suggestionsMock = editDirectorSuggestions as jest.MockedFunction<
  typeof editDirectorSuggestions
>;

function snapshot(): CopilotSnapshot {
  return {
    text_bars: [],
    slots: [],
    has_narrated_captions: false,
    total_duration_s: 5,
    max_duration_s: 60,
    remaining_duration_s: 55,
    allowed_op_families: ["text", "clip"],
  } as CopilotSnapshot;
}

describe("isFeatureUnavailable", () => {
  it("matches the flag gate and the missing-route skew, not ordinary 404s", () => {
    expect(isFeatureUnavailable(new FeatureDisabledError("edit_copilot_not_enabled"))).toBe(true);
    expect(isFeatureUnavailable(new FeatureDisabledError("edit_director_not_enabled"))).toBe(true);
    // FastAPI's default body when the deployed API predates the route.
    expect(isFeatureUnavailable(new Error("Not Found"))).toBe(true);

    // Real 404s on the same routes must stay retryable errors.
    expect(isFeatureUnavailable(new Error("Plan item not found"))).toBe(false);
    expect(isFeatureUnavailable(new Error("No render to edit yet"))).toBe(false);
    expect(isFeatureUnavailable(new Error("Variant not found"))).toBe(false);
    // A 502 from the agent is a genuine failure worth retrying.
    expect(isFeatureUnavailable(new Error("edit_copilot_failed"))).toBe(false);
  });

  it("identifies by name, so a duplicated module copy still matches", () => {
    // Same shape, different class identity — what a second bundled copy of
    // plan-api (or a partial jest mock) produces. `instanceof` would miss it,
    // and would throw outright if the class were absent from the module.
    const crossRealm = new Error("edit_director_not_enabled");
    crossRealm.name = "FeatureDisabledError";
    expect(isFeatureUnavailable(crossRealm)).toBe(true);
  });

  it("never throws on non-Error rejections", () => {
    // Called from inside catch blocks: throwing here would discard the error
    // being handled and strand the drawer mid-turn.
    for (const value of [undefined, null, "boom", 404, {}]) {
      expect(() => isFeatureUnavailable(value)).not.toThrow();
      expect(isFeatureUnavailable(value)).toBe(false);
    }
  });
});

describe("copilotErrorMessage", () => {
  it("hides backend diagnostics that mean nothing to a creator", () => {
    // 502 sentinel from routes/_copilot.py.
    expect(copilotErrorMessage(new Error("edit_copilot_failed"))).toBe(COPILOT_GENERIC_ERROR);
    // slowapi 429 bodies use {"error": ...}, so request() never finds a detail.
    expect(copilotErrorMessage(new Error("Request failed (429)"))).toBe(COPILOT_GENERIC_ERROR);
    expect(copilotErrorMessage(new Error("Request failed (500)"))).toBe(COPILOT_GENERIC_ERROR);
    expect(copilotErrorMessage(new Error(""))).toBe(COPILOT_GENERIC_ERROR);
    expect(copilotErrorMessage("not an error")).toBe(COPILOT_GENERIC_ERROR);
  });

  it("keeps route copy that actually tells the user something", () => {
    for (const message of [
      "Plan item not found",
      "No render to edit yet",
      "Variant not found",
      "This variant is still rendering.",
    ]) {
      expect(copilotErrorMessage(new Error(message))).toBe(message);
    }
  });
});

describe("useEditCopilot when the API has no copilot route", () => {
  beforeEach(() => jest.clearAllMocks());

  it("latches unavailable, explains why, and stops issuing turns", async () => {
    turnMock.mockRejectedValue(new FeatureDisabledError("edit_copilot_not_enabled"));

    const { result } = renderHook(() =>
      useEditCopilot({
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => snapshot(),
        applyOps: jest.fn(),
      }),
    );

    await act(async () => {
      await result.current.send("change the captions colour");
    });

    await waitFor(() => expect(result.current.unavailable).toBe(true));
    expect(result.current.error).toBe(COPILOT_UNAVAILABLE_MESSAGE);
    // The user's text is handed back rather than swallowed.
    expect(result.current.restoredInput).toBe("change the captions colour");
    expect(turnMock).toHaveBeenCalledTimes(1);

    // Latched: further sends must not re-fire a request that cannot succeed.
    await act(async () => {
      await result.current.send("try again");
    });
    expect(turnMock).toHaveBeenCalledTimes(1);
  });

  it("drops a queued follow-up instead of stranding it behind an inert composer", async () => {
    let rejectTurn: ((reason: unknown) => void) | null = null;
    turnMock.mockReturnValue(
      new Promise((_resolve, reject) => {
        rejectTurn = reject;
      }),
    );

    const { result } = renderHook(() =>
      useEditCopilot({
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => snapshot(),
        applyOps: jest.fn(),
      }),
    );

    act(() => {
      void result.current.send("first");
    });
    await waitFor(() => expect(result.current.sending).toBe(true));

    // Queues behind the in-flight turn.
    act(() => {
      void result.current.send("queued follow-up");
    });
    await waitFor(() => expect(result.current.queued?.text).toBe("queued follow-up"));

    await act(async () => {
      rejectTurn?.(new FeatureDisabledError("edit_copilot_not_enabled"));
      await Promise.resolve();
    });

    await waitFor(() => expect(result.current.unavailable).toBe(true));
    // Composer is now disabled, so a surviving chip would be unsendable.
    expect(result.current.queued).toBeNull();
  });

  it("never shows a raw backend sentinel in the drawer", async () => {
    // The 502 the copilot route raises when the agent fails. Rendering
    // `{error}` verbatim would put "edit_copilot_failed" in front of a creator.
    turnMock.mockRejectedValue(new Error("edit_copilot_failed"));

    const { result } = renderHook(() =>
      useEditCopilot({
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => snapshot(),
        applyOps: jest.fn(),
      }),
    );

    await act(async () => {
      await result.current.send("tighten the cuts");
    });

    await waitFor(() => expect(result.current.error).toBe(COPILOT_GENERIC_ERROR));
    expect(result.current.error).not.toContain("edit_copilot_failed");
    // Still a normal failure: retrying is allowed.
    expect(result.current.unavailable).toBe(false);
  });

  it("keeps a genuine failure retryable and surfaces its real message", async () => {
    turnMock.mockRejectedValue(new Error("Plan item not found"));

    const { result } = renderHook(() =>
      useEditCopilot({
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => snapshot(),
        applyOps: jest.fn(),
      }),
    );

    await act(async () => {
      await result.current.send("make the hook punchier");
    });

    await waitFor(() => expect(result.current.error).toBe("Plan item not found"));
    expect(result.current.unavailable).toBe(false);

    await act(async () => {
      await result.current.send("make the hook punchier");
    });
    expect(turnMock).toHaveBeenCalledTimes(2);
  });
});

describe("useEditDirector when the API has no director route", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.clearAllMocks();
    window.sessionStorage.clear();
  });
  afterEach(() => jest.useRealTimers());

  it("stops re-reviewing after a flag-gated 404 instead of erroring on every edit", async () => {
    suggestionsMock.mockRejectedValue(new FeatureDisabledError("edit_director_not_enabled"));

    const { rerender, result } = renderHook(
      ({ revision }: { revision: number }) =>
        useEditDirector({
          enabled: true,
          omniEnabled: false,
          itemId: "item-1",
          variantId: "variant-1",
          buildSnapshot: () => ({ ...snapshot(), total_duration_s: revision }),
          applyOpsAtomic: jest.fn(),
          onApplied: jest.fn(),
        }),
      { initialProps: { revision: 1 } },
    );

    await act(async () => {
      jest.advanceTimersByTime(1200);
      await Promise.resolve();
    });

    expect(result.current.unavailable).toBe(true);
    expect(result.current.error).toBe(DIRECTOR_UNAVAILABLE_MESSAGE);
    expect(suggestionsMock).toHaveBeenCalledTimes(1);

    // Each edit changes the complete snapshot. Before the fix this re-fired the doomed
    // request and repainted the failure; now it must stay quiet.
    for (const revision of [2, 3, 4]) {
      rerender({ revision });
      await act(async () => {
        jest.advanceTimersByTime(1200);
        await Promise.resolve();
      });
    }
    expect(suggestionsMock).toHaveBeenCalledTimes(1);
  });

  it("lets a deliberate Refresh clear the latch so the button is not dead", async () => {
    suggestionsMock.mockRejectedValue(new FeatureDisabledError("edit_director_not_enabled"));

    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: false,
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => snapshot(),
        applyOpsAtomic: jest.fn(),
        onApplied: jest.fn(),
      }),
    );

    await act(async () => {
      jest.advanceTimersByTime(1200);
      await Promise.resolve();
    });
    expect(result.current.unavailable).toBe(true);
    expect(suggestionsMock).toHaveBeenCalledTimes(1);

    // DirectorSuggestions renders a visible Refresh button; it must still do
    // something after the latch, otherwise the user is clicking a dead control.
    act(() => result.current.refresh());
    await act(async () => {
      jest.advanceTimersByTime(1200);
      await Promise.resolve();
    });
    expect(suggestionsMock).toHaveBeenCalledTimes(2);
  });
});

describe("CopilotDrawer error surface", () => {
  const baseProps = {
    layoutMode: "full" as const,
    open: true,
    messages: [] as CopilotMessage[],
    sending: false,
    queued: null,
    restoredInput: "",
    suggestions: [],
    historyVersion: 0,
    canUndo: true,
    onSend: jest.fn(),
    onCancelQueued: jest.fn(),
    onEditQueued: jest.fn(),
    onStop: jest.fn(),
    onUndo: jest.fn(),
    onClose: jest.fn(),
    onClearRestoredInput: jest.fn(),
  };

  it("renders the actual error rather than fixed copy", () => {
    render(<CopilotDrawer {...baseProps} error={COPILOT_UNAVAILABLE_MESSAGE} unavailable />);
    expect(screen.getByText(COPILOT_UNAVAILABLE_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByText(/couldn't reach Nova just now/i)).not.toBeInTheDocument();
  });

  it("passes a real backend detail straight through", () => {
    render(<CopilotDrawer {...baseProps} error="Plan item not found" />);
    expect(screen.getByText("Plan item not found")).toBeInTheDocument();
  });

  it("makes the composer inert when the feature is unavailable", () => {
    render(<CopilotDrawer {...baseProps} error={COPILOT_UNAVAILABLE_MESSAGE} unavailable />);
    expect(screen.getByLabelText("Tell Nova what to change")).toBeDisabled();
  });
});
