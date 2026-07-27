import { act, renderHook } from "@testing-library/react";
import {
  cancelOmniAsset,
  claimOmniAsset,
  editDirectorFeedback,
  editDirectorSuggestions,
  getOmniAsset,
  startOmniAsset,
  type EditorSuggestion,
} from "@/lib/plan-api";
import {
  directorSnapshotRevision,
  useEditDirector,
} from "@/lib/edit-copilot/useEditDirector";
import type { ApplyCopilotOpsResult } from "@/lib/edit-copilot/apply-ops";
import type { CopilotSnapshot } from "@/lib/edit-copilot/snapshot";

jest.mock("@/lib/plan-api", () => ({
  editDirectorSuggestions: jest.fn(),
  editDirectorFeedback: jest.fn(),
  startOmniAsset: jest.fn(),
  getOmniAsset: jest.fn(),
  cancelOmniAsset: jest.fn(),
  claimOmniAsset: jest.fn(),
}));

const suggestionsMock = editDirectorSuggestions as jest.MockedFunction<typeof editDirectorSuggestions>;
const feedbackMock = editDirectorFeedback as jest.MockedFunction<typeof editDirectorFeedback>;
const startMock = startOmniAsset as jest.MockedFunction<typeof startOmniAsset>;
const getMock = getOmniAsset as jest.MockedFunction<typeof getOmniAsset>;
const cancelMock = cancelOmniAsset as jest.MockedFunction<typeof cancelOmniAsset>;
const claimMock = claimOmniAsset as jest.MockedFunction<typeof claimOmniAsset>;

function snapshot(text = "old hook"): CopilotSnapshot {
  return {
    text_bars: [{
      index: 0,
      id: "bar-1",
      text,
      start_s: 0,
      end_s: 2,
      role: "generative_intro",
      font_family: "Inter",
      size_px: 64,
      color: "#FFFFFF",
      highlight_color: null,
      effect: "static",
      alignment: "center",
      text_case: "none",
      letter_spacing: 0,
      line_spacing: 1,
      max_width_frac: 0.8,
      stroke_width: 0,
      position: "middle",
      x_frac: null,
      y_frac: null,
    }],
    slots: [],
    has_narrated_captions: false,
    total_duration_s: 5,
    max_duration_s: 60,
    remaining_duration_s: 55,
    allowed_op_families: ["text"],
  };
}

function suggestion(overrides: Partial<EditorSuggestion> = {}): EditorSuggestion {
  return {
    id: "director-1",
    category: "text",
    title: "Sharper hook",
    rationale: "Make the first beat create a clearer question.",
    expected_benefit: "More curiosity in the opening second.",
    confidence: 0.9,
    start_s: 0,
    end_s: 2,
    apply_mode: "instant",
    ops: [{ op: "edit_text", bar_index: 0, text: "new hook" }],
    omni: null,
    ...overrides,
  };
}

function appliedResult(): ApplyCopilotOpsResult {
  return {
    textActions: [{ type: "EDIT_TEXT", id: "bar-1", text: "new hook" }],
    nextSlots: null,
    applied: [{ label: "Text", from: "old hook", to: "new hook" }],
    rejected: [],
  };
}

async function loadInitialReview(): Promise<void> {
  await act(async () => {
    jest.advanceTimersByTime(250);
    await Promise.resolve();
  });
}

describe("useEditDirector", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.clearAllMocks();
    window.sessionStorage.clear();
    feedbackMock.mockResolvedValue(undefined);
    cancelMock.mockResolvedValue({
      asset_id: "asset-1",
      status: "cancelled",
      progress: 0,
      model: "gemini-omni-flash-preview",
      error: null,
      operation: null,
    });
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it("loads, accepts atomically, dismisses, and refreshes", async () => {
    const current = snapshot();
    const first = suggestion();
    const second = suggestion({ id: "director-2", title: "Cleaner pace" });
    suggestionsMock.mockResolvedValue({
      suggestions: [first, second],
      snapshot_revision: directorSnapshotRevision(current),
      requested_model: "gemini-3.1-pro-preview",
      model_used: "gemini-3.1-pro-preview",
      fallback_reason: null,
    });
    const applyOpsAtomic = jest.fn(() => appliedResult());
    const onApplied = jest.fn();
    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: false,
        itemId: "item-1",
        variantId: "variant-1",
        materialRevision: 1,
        buildSnapshot: () => current,
        applyOpsAtomic,
        onApplied,
      }),
    );

    await loadInitialReview();
    expect(result.current.suggestions).toHaveLength(2);
    expect(result.current.modelUsed).toBe("gemini-3.1-pro-preview");

    act(() => result.current.accept(first));
    expect(applyOpsAtomic).toHaveBeenCalledWith(first.ops, current);
    expect(onApplied).toHaveBeenCalledTimes(1);
    expect(result.current.suggestions.map((item) => item.id)).toEqual(["director-2"]);

    act(() => result.current.dismiss(second));
    expect(result.current.suggestions).toEqual([]);
    expect(window.sessionStorage.getItem("nova-edit-director-dismissed:item-1:variant-1"))
      .toContain("director-2");

    act(() => result.current.refresh());
    await act(async () => {
      jest.advanceTimersByTime(1200);
      await Promise.resolve();
    });
    expect(suggestionsMock).toHaveBeenCalledTimes(2);
  });

  it("drops a response when the material draft changed while it was in flight", async () => {
    let current = snapshot();
    let resolveRequest: ((value: Awaited<ReturnType<typeof editDirectorSuggestions>>) => void) | null = null;
    suggestionsMock.mockReturnValue(new Promise((resolve) => {
      resolveRequest = resolve;
    }));
    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: false,
        itemId: "item-1",
        variantId: "variant-1",
        materialRevision: 1,
        buildSnapshot: () => current,
        applyOpsAtomic: () => appliedResult(),
        onApplied: jest.fn(),
      }),
    );

    await act(async () => {
      jest.advanceTimersByTime(250);
    });
    current = snapshot("user changed this");
    await act(async () => {
      resolveRequest?.({
        suggestions: [suggestion()],
        snapshot_revision: directorSnapshotRevision(snapshot()),
        requested_model: "gemini-3.1-pro-preview",
        model_used: "gemini-3.1-pro-preview",
        fallback_reason: null,
      });
      await Promise.resolve();
    });

    expect(result.current.suggestions).toEqual([]);
  });

  it("polls an Omni asset and inserts it only after the normalized operation is ready", async () => {
    const current = snapshot();
    const omni = suggestion({
      id: "director-omni",
      apply_mode: "omni_async",
      ops: [],
      omni: {
        action: "generate_insert",
        prompt: "A short visual bridge",
        insert_at_s: 2,
        duration_s: 4,
        source_clip_index: null,
        source_start_s: null,
        source_end_s: null,
        reference_clip_index: null,
        reference_frame_s: null,
      },
    });
    suggestionsMock.mockResolvedValue({
      suggestions: [omni],
      snapshot_revision: directorSnapshotRevision(current),
      requested_model: "gemini-3.1-pro-preview",
      model_used: "gemini-3.1-pro-preview",
      fallback_reason: null,
    });
    startMock.mockResolvedValue({
      asset_id: "asset-1",
      status: "queued",
      progress: 0.02,
      model: "gemini-omni-flash-preview",
      error: null,
      operation: null,
    });
    getMock.mockResolvedValue({
      asset_id: "asset-1",
      status: "ready",
      progress: 1,
      model: "gemini-omni-flash-preview",
      error: null,
      operation: null,
    });
    claimMock.mockResolvedValue({
      asset_id: "asset-1",
      status: "ready",
      progress: 1,
      model: "gemini-omni-flash-preview",
      error: null,
      operation: {
        op: "insert_generated_asset",
        asset_id: "asset-1",
        clip_index: 3,
        insert_at_s: 2,
        duration_s: 4,
      },
    });
    const applyOpsAtomic = jest.fn(() => appliedResult());
    const onApplied = jest.fn();
    const onGeneratedAssetReady = jest.fn();
    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: true,
        itemId: "item-1",
        variantId: "variant-1",
        materialRevision: 1,
        buildSnapshot: () => current,
        applyOpsAtomic,
        onApplied,
        onGeneratedAssetReady,
      }),
    );

    await loadInitialReview();
    act(() => result.current.accept(omni));
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.generation?.status).toBe("queued");
    expect(onApplied).not.toHaveBeenCalled();

    await act(async () => {
      jest.advanceTimersByTime(2000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(applyOpsAtomic).toHaveBeenCalledWith(
      [expect.objectContaining({ op: "insert_generated_asset", asset_id: "asset-1" })],
      current,
    );
    expect(onApplied).toHaveBeenCalledTimes(1);
    expect(onGeneratedAssetReady).toHaveBeenCalledTimes(1);
    expect(result.current.generation).toBeNull();
  });

  it("disables cancellation as soon as an Omni operation is applied locally", async () => {
    const current = snapshot();
    const omni = suggestion({
      id: "director-omni-accepted",
      apply_mode: "omni_async",
      ops: [],
      omni: {
        action: "generate_insert",
        prompt: "A short visual bridge",
        insert_at_s: 2,
        duration_s: 4,
      },
    });
    suggestionsMock.mockResolvedValue({
      suggestions: [omni],
      snapshot_revision: directorSnapshotRevision(current),
      requested_model: "gemini-3.1-pro-preview",
      model_used: "gemini-3.1-pro-preview",
      fallback_reason: null,
    });
    startMock.mockResolvedValue({
      asset_id: "asset-1",
      status: "queued",
      progress: 0.02,
      model: "gemini-omni-flash-preview",
    });
    getMock.mockResolvedValue({
      asset_id: "asset-1",
      status: "ready",
      progress: 1,
      model: "gemini-omni-flash-preview",
      operation: null,
    });
    claimMock.mockResolvedValue({
      asset_id: "asset-1",
      status: "ready",
      progress: 1,
      model: "gemini-omni-flash-preview",
      operation: {
        op: "insert_generated_asset",
        asset_id: "asset-1",
        clip_index: 3,
        insert_at_s: 2,
        duration_s: 4,
      },
    });
    let finishRefresh: (() => void) | null = null;
    const onGeneratedAssetReady = jest.fn(
      () => new Promise<void>((resolve) => {
        finishRefresh = resolve;
      }),
    );
    const onApplied = jest.fn();
    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: true,
        itemId: "item-1",
        variantId: "variant-1",
        materialRevision: 1,
        buildSnapshot: () => current,
        applyOpsAtomic: () => appliedResult(),
        onApplied,
        onGeneratedAssetReady,
      }),
    );

    await loadInitialReview();
    act(() => result.current.accept(omni));
    await act(async () => {
      await Promise.resolve();
      jest.advanceTimersByTime(2000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(onApplied).toHaveBeenCalledTimes(1);
    expect(result.current.generation).toBeNull();
    act(() => result.current.cancelGeneration());
    expect(cancelMock).not.toHaveBeenCalled();

    await act(async () => {
      finishRefresh?.();
      await Promise.resolve();
    });
  });

  it("keeps raw Omni failure codes out of the user-facing error", async () => {
    const current = snapshot();
    const omni = suggestion({
      id: "director-omni-failed",
      apply_mode: "omni_async",
      ops: [],
      omni: {
        action: "generate_insert",
        prompt: "A short visual bridge",
        insert_at_s: 2,
        duration_s: 4,
      },
    });
    suggestionsMock.mockResolvedValue({
      suggestions: [omni],
      snapshot_revision: directorSnapshotRevision(current),
      requested_model: "gemini-3.1-pro-preview",
      model_used: "gemini-3.1-pro-preview",
      fallback_reason: null,
    });
    startMock.mockResolvedValue({
      asset_id: "asset-1",
      status: "queued",
      progress: 0.02,
      model: "gemini-omni-flash-preview",
    });
    getMock.mockResolvedValue({
      asset_id: "asset-1",
      status: "failed",
      progress: 0,
      model: "gemini-omni-flash-preview",
      error: "omni_provider_budget_exceeded",
    });
    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: true,
        itemId: "item-1",
        variantId: "variant-1",
        materialRevision: 1,
        buildSnapshot: () => current,
        applyOpsAtomic: () => appliedResult(),
        onApplied: jest.fn(),
      }),
    );

    await loadInitialReview();
    act(() => result.current.accept(omni));
    await act(async () => {
      await Promise.resolve();
      jest.advanceTimersByTime(2000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.error).toBe(
      "Nova couldn't generate that clip. Your draft was not changed.",
    );
    expect(result.current.error).not.toContain("omni_provider");
  });

  it("does not claim a ready asset when the draft changed during generation", async () => {
    let current = snapshot();
    const omni = suggestion({
      id: "director-omni-stale",
      apply_mode: "omni_async",
      ops: [],
      omni: {
        action: "generate_insert",
        prompt: "A short visual bridge",
        insert_at_s: 2,
        duration_s: 4,
      },
    });
    suggestionsMock.mockResolvedValue({
      suggestions: [omni],
      snapshot_revision: directorSnapshotRevision(current),
      requested_model: "gemini-3.1-pro-preview",
      model_used: "gemini-3.1-pro-preview",
      fallback_reason: null,
    });
    startMock.mockResolvedValue({
      asset_id: "asset-1",
      status: "queued",
      progress: 0.02,
      model: "gemini-omni-flash-preview",
    });
    getMock.mockResolvedValue({
      asset_id: "asset-1",
      status: "ready",
      progress: 1,
      model: "gemini-omni-flash-preview",
      operation: null,
    });
    const onApplied = jest.fn();
    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: true,
        itemId: "item-1",
        variantId: "variant-1",
        materialRevision: 1,
        buildSnapshot: () => current,
        applyOpsAtomic: () => appliedResult(),
        onApplied,
      }),
    );

    await loadInitialReview();
    act(() => result.current.accept(omni));
    current = snapshot("user changed this");
    await act(async () => {
      await Promise.resolve();
      jest.advanceTimersByTime(2000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(claimMock).not.toHaveBeenCalled();
    expect(onApplied).not.toHaveBeenCalled();
    expect(result.current.error).toContain("draft changed");
  });

  it("releases a claimed asset when the draft changes while claim is in flight", async () => {
    let current = snapshot();
    let resolveClaim: ((value: Awaited<ReturnType<typeof claimOmniAsset>>) => void) | null = null;
    const omni = suggestion({
      id: "director-omni-claim-race",
      apply_mode: "omni_async",
      ops: [],
      omni: {
        action: "generate_insert",
        prompt: "A short visual bridge",
        insert_at_s: 2,
        duration_s: 4,
      },
    });
    suggestionsMock.mockResolvedValue({
      suggestions: [omni],
      snapshot_revision: directorSnapshotRevision(current),
      requested_model: "gemini-3.1-pro-preview",
      model_used: "gemini-3.1-pro-preview",
      fallback_reason: null,
    });
    startMock.mockResolvedValue({
      asset_id: "asset-1",
      status: "queued",
      progress: 0.02,
      model: "gemini-omni-flash-preview",
    });
    getMock.mockResolvedValue({
      asset_id: "asset-1",
      status: "ready",
      progress: 1,
      model: "gemini-omni-flash-preview",
      operation: null,
    });
    claimMock.mockReturnValue(new Promise((resolve) => {
      resolveClaim = resolve;
    }));
    const applyOpsAtomic = jest.fn(() => appliedResult());
    const onApplied = jest.fn();
    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: true,
        itemId: "item-1",
        variantId: "variant-1",
        materialRevision: 1,
        buildSnapshot: () => current,
        applyOpsAtomic,
        onApplied,
      }),
    );

    await loadInitialReview();
    act(() => result.current.accept(omni));
    await act(async () => {
      await Promise.resolve();
      jest.advanceTimersByTime(2000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(claimMock).toHaveBeenCalled();

    current = snapshot("edited during claim");
    await act(async () => {
      resolveClaim?.({
        asset_id: "asset-1",
        status: "ready",
        progress: 1,
        model: "gemini-omni-flash-preview",
        operation: {
          op: "insert_generated_asset",
          asset_id: "asset-1",
          clip_index: 3,
          insert_at_s: 2,
          duration_s: 4,
        },
      });
      await Promise.resolve();
    });

    expect(applyOpsAtomic).not.toHaveBeenCalled();
    expect(onApplied).not.toHaveBeenCalled();
    expect(cancelMock).toHaveBeenCalledWith("item-1", "variant-1", "asset-1");
    expect(result.current.error).toContain("draft changed");
  });

  it("cancels an active generation without changing the draft", async () => {
    const current = snapshot();
    const omni = suggestion({
      id: "director-omni-cancel",
      apply_mode: "omni_async",
      ops: [],
      omni: {
        action: "generate_insert",
        prompt: "A short visual bridge",
        insert_at_s: 2,
        duration_s: 4,
      },
    });
    suggestionsMock.mockResolvedValue({
      suggestions: [omni],
      snapshot_revision: directorSnapshotRevision(current),
      requested_model: "gemini-3.1-pro-preview",
      model_used: "gemini-3.1-pro-preview",
      fallback_reason: null,
    });
    startMock.mockResolvedValue({
      asset_id: "asset-1",
      status: "queued",
      progress: 0.02,
      model: "gemini-omni-flash-preview",
    });
    const onApplied = jest.fn();
    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: true,
        itemId: "item-1",
        variantId: "variant-1",
        materialRevision: 1,
        buildSnapshot: () => current,
        applyOpsAtomic: () => appliedResult(),
        onApplied,
      }),
    );

    await loadInitialReview();
    act(() => result.current.accept(omni));
    await act(async () => {
      await Promise.resolve();
    });
    act(() => result.current.cancelGeneration());
    await act(async () => {
      await Promise.resolve();
    });

    expect(cancelMock).toHaveBeenCalledWith("item-1", "variant-1", "asset-1");
    expect(onApplied).not.toHaveBeenCalled();
    expect(result.current.generation).toBeNull();
  });
});
