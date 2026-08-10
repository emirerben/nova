import { act, renderHook } from "@testing-library/react";
import { useState } from "react";
import {
  applySpeechCutCandidate,
  cancelOmniAsset,
  claimOmniAsset,
  editDirectorFeedback,
  editDirectorSuggestions,
  getOmniAsset,
  restoreOriginalSpeechTiming,
  startOmniAsset,
  type EditorSuggestion,
} from "@/lib/plan-api";
import {
  DIRECTOR_CAPABILITY_MISMATCH_MESSAGE,
  directorSnapshotRevision,
  useEditDirector,
} from "@/lib/edit-copilot/useEditDirector";
import type { ApplyCopilotOpsResult } from "@/lib/edit-copilot/apply-ops";
import type { CopilotSnapshot } from "@/lib/edit-copilot/snapshot";

jest.mock("@/lib/plan-api", () => ({
  applySpeechCutCandidate: jest.fn(),
  editDirectorSuggestions: jest.fn(),
  editDirectorFeedback: jest.fn(),
  startOmniAsset: jest.fn(),
  getOmniAsset: jest.fn(),
  cancelOmniAsset: jest.fn(),
  claimOmniAsset: jest.fn(),
  restoreOriginalSpeechTiming: jest.fn(),
}));

const applySpeechCutMock = applySpeechCutCandidate as jest.MockedFunction<
  typeof applySpeechCutCandidate
>;
const suggestionsMock = editDirectorSuggestions as jest.MockedFunction<typeof editDirectorSuggestions>;
const feedbackMock = editDirectorFeedback as jest.MockedFunction<typeof editDirectorFeedback>;
const startMock = startOmniAsset as jest.MockedFunction<typeof startOmniAsset>;
const getMock = getOmniAsset as jest.MockedFunction<typeof getOmniAsset>;
const cancelMock = cancelOmniAsset as jest.MockedFunction<typeof cancelOmniAsset>;
const claimMock = claimOmniAsset as jest.MockedFunction<typeof claimOmniAsset>;
const restoreSpeechTimingMock = restoreOriginalSpeechTiming as jest.MockedFunction<
  typeof restoreOriginalSpeechTiming
>;

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
    jest.advanceTimersByTime(1200);
    await Promise.resolve();
  });
}

describe("useEditDirector", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.clearAllMocks();
    window.sessionStorage.clear();
    feedbackMock.mockResolvedValue(undefined);
    applySpeechCutMock.mockResolvedValue({
      status: "rendering",
      request: {
        operation: "apply_speech_cut_candidate",
        operation_id: "operation-1",
        candidate_id: "candidate-1",
        revision: "cut-revision-2",
      },
    });
    restoreSpeechTimingMock.mockResolvedValue({
      status: "rendering",
      request: {
        operation: "restore_original_timing",
        operation_id: "restore-operation-1",
        revision: "cut-revision-3",
      },
    });
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
        buildSnapshot: () => current,
        applyOpsAtomic,
        onApplied,
      }),
    );

    await loadInitialReview();
    expect(result.current.suggestions).toHaveLength(2);
    expect(result.current.modelUsed).toBe("gemini-3.1-pro-preview");
    expect(suggestionsMock).toHaveBeenCalledWith(
      "item-1",
      "variant-1",
      expect.objectContaining({ omni_enabled: false }),
      expect.any(AbortSignal),
    );

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

  it("treats an empty review as settled until an explicit refresh", async () => {
    const current = snapshot();
    suggestionsMock.mockImplementation(async (_itemId, _variantId, body) => ({
      suggestions: [],
      snapshot_revision: body.snapshot_revision,
      requested_model: "gemini-3.1-pro-preview",
      model_used: "gemini-3.1-pro-preview",
      fallback_reason: null,
    }));
    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: false,
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => current,
        applyOpsAtomic: jest.fn(() => appliedResult()),
        onApplied: jest.fn(),
      }),
    );

    await loadInitialReview();
    await loadInitialReview();
    expect(result.current.suggestions).toEqual([]);
    expect(suggestionsMock).toHaveBeenCalledTimes(1);

    act(() => result.current.refresh());
    await loadInitialReview();
    expect(suggestionsMock).toHaveBeenCalledTimes(2);
  });

  it("sends server speech-cut operations to the API without applying them locally", async () => {
    const current = snapshot();
    const cut = suggestion({
      id: "director-cut",
      category: "hook_pacing",
      apply_mode: "server_async",
      ops: [{ op: "apply_speech_cut_candidate", candidate_id: "candidate-1" }],
    });
    suggestionsMock.mockResolvedValue({
      suggestions: [cut],
      snapshot_revision: directorSnapshotRevision(current),
      requested_model: "gemini-3.1-pro-preview",
      model_used: "gemini-3.1-pro-preview",
      fallback_reason: null,
    });
    const applyOpsAtomic = jest.fn(() => appliedResult());
    const onServerRenderStarted = jest.fn();
    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: false,
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => current,
        applyOpsAtomic,
        onApplied: jest.fn(),
        speechCutRevision: "cut-revision-1",
        serverOperationsEnabled: true,
        onServerRenderStarted,
      }),
    );

    await loadInitialReview();
    act(() => result.current.accept(cut));
    expect(result.current.serverRendering).toBe(true);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(applySpeechCutMock).toHaveBeenCalledWith(
      "item-1",
      "variant-1",
      "candidate-1",
      "cut-revision-1",
    );
    expect(applyOpsAtomic).not.toHaveBeenCalled();
    expect(onServerRenderStarted).toHaveBeenCalledTimes(1);
    expect(result.current.suggestions).toEqual([]);
    expect(result.current.serverRendering).toBe(true);
  });

  it("requires a clean saved draft for apply and restore server operations", async () => {
    const current = snapshot();
    const cut = suggestion({
      id: "director-cut",
      apply_mode: "server_async",
      ops: [{ op: "apply_speech_cut_candidate", candidate_id: "candidate-1" }],
    });
    suggestionsMock.mockResolvedValue({
      suggestions: [cut],
      snapshot_revision: directorSnapshotRevision(current),
      requested_model: "gemini-3.1-pro-preview",
      model_used: "gemini-3.1-pro-preview",
      fallback_reason: null,
    });
    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: false,
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => current,
        applyOpsAtomic: jest.fn(() => appliedResult()),
        onApplied: jest.fn(),
        speechCutRevision: "cut-revision-1",
        serverOperationsEnabled: false,
        canRestoreOriginalTiming: true,
      }),
    );

    await loadInitialReview();
    act(() => result.current.accept(cut));
    expect(result.current.error).toContain("Save your draft");
    expect(applySpeechCutMock).not.toHaveBeenCalled();

    act(() => result.current.restoreOriginalTiming());
    expect(result.current.error).toContain("Save your draft");
    expect(restoreSpeechTimingMock).not.toHaveBeenCalled();
  });

  it("restores original speech timing through the server rerender", async () => {
    const current = snapshot();
    suggestionsMock.mockResolvedValue({
      suggestions: [],
      snapshot_revision: directorSnapshotRevision(current),
      requested_model: "gemini-3.1-pro-preview",
      model_used: "gemini-3.1-pro-preview",
      fallback_reason: null,
    });
    const onServerRenderStarted = jest.fn();
    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: false,
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => current,
        applyOpsAtomic: jest.fn(() => appliedResult()),
        onApplied: jest.fn(),
        speechCutRevision: "cut-revision-2",
        serverOperationsEnabled: true,
        canRestoreOriginalTiming: true,
        onServerRenderStarted,
      }),
    );

    await loadInitialReview();
    act(() => result.current.restoreOriginalTiming());
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(restoreSpeechTimingMock).toHaveBeenCalledWith(
      "item-1",
      "variant-1",
      "cut-revision-2",
    );
    expect(onServerRenderStarted).toHaveBeenCalledTimes(1);
    expect(result.current.serverRendering).toBe(true);
  });

  it("keeps the rebuild locked until the polled winning receipt arrives", async () => {
    const current = snapshot();
    const cut = suggestion({
      id: "director-cut",
      apply_mode: "server_async",
      ops: [{ op: "apply_speech_cut_candidate", candidate_id: "candidate-1" }],
    });
    suggestionsMock.mockResolvedValue({
      suggestions: [cut],
      snapshot_revision: directorSnapshotRevision(current),
      requested_model: "gemini-3.1-pro-preview",
      model_used: "gemini-3.1-pro-preview",
      fallback_reason: null,
    });
    let renderPending = false;
    let lastReceipt: Parameters<typeof useEditDirector>[0]["speechCutLastReceipt"] = null;
    const { result, rerender } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: false,
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => current,
        applyOpsAtomic: jest.fn(() => appliedResult()),
        onApplied: jest.fn(),
        speechCutRevision: "cut-revision-1",
        serverOperationsEnabled: true,
        serverRenderPending: renderPending,
        speechCutLastReceipt: lastReceipt,
      }),
    );

    await loadInitialReview();
    act(() => result.current.accept(cut));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.serverRendering).toBe(true);
    expect(result.current.appliedReceipts).toEqual([]);

    renderPending = true;
    rerender();
    expect(result.current.serverRendering).toBe(true);

    renderPending = false;
    lastReceipt = {
      operation: "apply_speech_cut_candidate",
      operation_id: "operation-1",
      revision: "cut-revision-2",
      status: "applied",
      render_generation_id: "generation-1",
      removed: { start_s: 2, end_s: 2.8, reason: "retake_review" },
      time_saved_s: 0.8,
    };
    rerender();

    expect(result.current.serverRendering).toBe(false);
    expect(result.current.appliedReceipts).toEqual([
      expect.objectContaining({
        title: "Reviewed speech cut applied",
        startS: 2,
        endS: 2.8,
        changes: [
          expect.objectContaining({
            from: "2.000-2.800s",
            to: "0.800s removed and downstream timing rebuilt",
          }),
        ],
      }),
    ]);
    expect(feedbackMock).toHaveBeenCalledWith(
      "item-1",
      "variant-1",
      expect.objectContaining({ suggestion_id: "director-cut", action: "accepted" }),
    );
  });

  it("surfaces an operation-matched worker failure after polling unlocks", async () => {
    const current = snapshot();
    const cut = suggestion({
      id: "director-cut",
      apply_mode: "server_async",
      ops: [{ op: "apply_speech_cut_candidate", candidate_id: "candidate-1" }],
    });
    suggestionsMock.mockResolvedValue({
      suggestions: [cut],
      snapshot_revision: directorSnapshotRevision(current),
      requested_model: "gemini-3.1-pro-preview",
      model_used: "gemini-3.1-pro-preview",
      fallback_reason: null,
    });
    let renderPending = false;
    let lastError: Parameters<typeof useEditDirector>[0]["speechCutLastError"] = null;
    const { result, rerender } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: false,
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => current,
        applyOpsAtomic: jest.fn(() => appliedResult()),
        onApplied: jest.fn(),
        speechCutRevision: "cut-revision-1",
        serverOperationsEnabled: true,
        serverRenderPending: renderPending,
        speechCutLastError: lastError,
      }),
    );

    await loadInitialReview();
    act(() => result.current.accept(cut));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    renderPending = true;
    rerender();
    expect(result.current.serverRendering).toBe(true);

    renderPending = false;
    lastError = { operation_id: "operation-1", message: "render exploded" };
    await act(async () => {
      rerender();
      await Promise.resolve();
    });

    expect(result.current.serverRendering).toBe(false);
    expect(result.current.error).toBe(
      "Nova couldn't complete that timing change. The current video is unchanged.",
    );
    expect(result.current.appliedReceipts).toEqual([]);
  });

  it("surfaces an old-server Omni-only response instead of silently showing an empty rail", async () => {
    const current = snapshot();
    const omni = suggestion({
      id: "director-omni",
      apply_mode: "omni_async",
      ops: [],
      omni: {
        action: "generate_insert",
        prompt: "A restrained visual bridge",
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

    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: false,
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => current,
        applyOpsAtomic: jest.fn(() => appliedResult()),
        onApplied: jest.fn(),
      }),
    );

    await loadInitialReview();

    expect(result.current.suggestions).toEqual([]);
    expect(result.current.error).toBe(DIRECTOR_CAPABILITY_MISMATCH_MESSAGE);
  });

  it("accepts every non-overlapping recommendation and retains an exact receipt for each", async () => {
    let current = snapshot();
    const review = [
      suggestion({ id: "director-text", title: "Sharper hook" }),
      suggestion({
        id: "director-title",
        title: "Set a working title",
        ops: [{ op: "set_title", title: "Building Nova" }],
      }),
      suggestion({
        id: "director-sound",
        title: "Add a hook sound",
        ops: [{ op: "add_sfx", effect_id: "sfx-pop", at_s: 0.5, gain: 0.6 }],
      }),
    ];
    suggestionsMock.mockResolvedValue({
      suggestions: review,
      snapshot_revision: directorSnapshotRevision(current),
      requested_model: "gemini-3.1-pro-preview",
      model_used: "gemini-3.1-pro-preview",
      fallback_reason: null,
    });
    const applyOpsAtomic = jest.fn((ops: EditorSuggestion["ops"]) => ({
      ...appliedResult(),
      applied: [{
        label: String(ops[0]?.op),
        from: "before",
        to: "preview",
      }],
    }));

    const { result } = renderHook(() => {
      const [previewChanges, setPreviewChanges] = useState<string[]>([]);
      const director = useEditDirector({
        enabled: true,
        omniEnabled: false,
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => current,
        applyOpsAtomic,
        onApplied: (applied) => {
          current = snapshot(`preview-${previewChanges.length + 1}`);
          setPreviewChanges((changes) => [
            ...changes,
            ...applied.applied.map((change) => change.label),
          ]);
          return {
            previewFocus: { kind: "text", id: "bar-1", seekS: 1 },
          };
        },
      });
      return { director, previewChanges };
    });

    await loadInitialReview();

    for (const recommendation of review) {
      act(() => result.current.director.accept(recommendation));
    }

    expect(applyOpsAtomic).toHaveBeenCalledTimes(3);
    expect(result.current.previewChanges).toEqual([
      "edit_text",
      "set_title",
      "add_sfx",
    ]);
    expect(result.current.director.suggestions).toEqual([]);
    expect(result.current.director.appliedReceipts).toEqual([
      expect.objectContaining({
        suggestionId: "director-text",
        title: "Sharper hook",
        changes: [{ label: "edit_text", from: "before", to: "preview" }],
        previewFocus: { kind: "text", id: "bar-1", seekS: 1 },
      }),
      expect.objectContaining({
        suggestionId: "director-title",
        title: "Set a working title",
        changes: [{ label: "set_title", from: "before", to: "preview" }],
      }),
      expect.objectContaining({
        suggestionId: "director-sound",
        title: "Add a hook sound",
        changes: [{ label: "add_sfx", from: "before", to: "preview" }],
      }),
    ]);

    await act(async () => {
      jest.advanceTimersByTime(1200);
      await Promise.resolve();
    });
    expect(suggestionsMock).toHaveBeenCalledTimes(2);
  });

  it("keeps the recommendation visible when the editor cannot commit it", async () => {
    const current = snapshot();
    const recommendation = suggestion();
    suggestionsMock.mockResolvedValue({
      suggestions: [recommendation],
      snapshot_revision: directorSnapshotRevision(current),
      requested_model: "gemini-3.1-pro-preview",
      model_used: "gemini-3.1-pro-preview",
      fallback_reason: null,
    });
    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: false,
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => current,
        applyOpsAtomic: () => appliedResult(),
        onApplied: () => {
          throw new Error("editor reducer failed");
        },
      }),
    );

    await loadInitialReview();
    act(() => result.current.accept(recommendation));

    expect(result.current.suggestions).toEqual([recommendation]);
    expect(result.current.appliedReceipts).toEqual([]);
    expect(result.current.error).toContain("couldn't confirm");
    expect(feedbackMock).not.toHaveBeenCalledWith(
      "item-1",
      "variant-1",
      expect.objectContaining({ action: "accepted" }),
    );
  });

  it("lets a manual refresh replace a visible review", async () => {
    const current = snapshot();
    suggestionsMock
      .mockResolvedValueOnce({
        suggestions: [suggestion({ id: "old-review" })],
        snapshot_revision: directorSnapshotRevision(current),
        requested_model: "gemini-3.1-pro-preview",
        model_used: "gemini-3.1-pro-preview",
        fallback_reason: null,
      })
      .mockResolvedValueOnce({
        suggestions: [suggestion({ id: "fresh-review" })],
        snapshot_revision: directorSnapshotRevision(current),
        requested_model: "gemini-3.1-pro-preview",
        model_used: "gemini-3.1-pro-preview",
        fallback_reason: null,
      });
    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: false,
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => current,
        applyOpsAtomic: () => appliedResult(),
        onApplied: jest.fn(),
      }),
    );

    await loadInitialReview();
    expect(result.current.suggestions.map((item) => item.id)).toEqual(["old-review"]);

    act(() => result.current.refresh());
    await loadInitialReview();

    expect(suggestionsMock).toHaveBeenCalledTimes(2);
    expect(result.current.suggestions.map((item) => item.id)).toEqual(["fresh-review"]);
  });

  it("replaces a rejected stale card instead of letting the card guard veto refresh", async () => {
    const current = snapshot();
    const stale = suggestion({ id: "stale-card" });
    suggestionsMock
      .mockResolvedValueOnce({
        suggestions: [stale],
        snapshot_revision: directorSnapshotRevision(current),
        requested_model: "gemini-3.1-pro-preview",
        model_used: "gemini-3.1-pro-preview",
        fallback_reason: null,
      })
      .mockResolvedValueOnce({
        suggestions: [suggestion({ id: "replacement-card" })],
        snapshot_revision: directorSnapshotRevision(current),
        requested_model: "gemini-3.1-pro-preview",
        model_used: "gemini-3.1-pro-preview",
        fallback_reason: null,
      });
    const { result } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: false,
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => current,
        applyOpsAtomic: () => ({
          textActions: [],
          nextSlots: null,
          applied: [],
          rejected: [{
            op: "remove_text",
            label: "Remove text",
            reason: "user_changed",
            detail: "text changed after Nova read it",
          }],
        }),
        onApplied: jest.fn(),
      }),
    );

    await loadInitialReview();
    act(() => result.current.accept(stale));
    await loadInitialReview();

    expect(suggestionsMock).toHaveBeenCalledTimes(2);
    expect(result.current.loading).toBe(false);
    expect(result.current.suggestions.map((item) => item.id)).toEqual(["replacement-card"]);
  });

  it("keeps an explicit refresh armed when hydration aborts its in-flight request", async () => {
    let current = snapshot();
    let resolveInterrupted: ((value: Awaited<ReturnType<typeof editDirectorSuggestions>>) => void) | null = null;
    suggestionsMock
      .mockResolvedValueOnce({
        suggestions: [suggestion({ id: "old-card" })],
        snapshot_revision: directorSnapshotRevision(current),
        requested_model: "gemini-3.1-pro-preview",
        model_used: "gemini-3.1-pro-preview",
        fallback_reason: null,
      })
      .mockReturnValueOnce(new Promise((resolve) => {
        resolveInterrupted = resolve;
      }))
      .mockImplementationOnce(async (_itemId, _variantId, body) => ({
        suggestions: [suggestion({ id: "hydrated-card" })],
        snapshot_revision: body.snapshot_revision,
        requested_model: "gemini-3.1-pro-preview",
        model_used: "gemini-3.1-pro-preview",
        fallback_reason: null,
      }));
    const { result, rerender } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: false,
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => current,
        applyOpsAtomic: () => appliedResult(),
        onApplied: jest.fn(),
      }),
    );

    await loadInitialReview();
    act(() => result.current.refresh());
    await act(async () => {
      jest.advanceTimersByTime(1200);
    });
    expect(result.current.loading).toBe(true);

    current = snapshot("hydrated while refreshing");
    rerender();
    await loadInitialReview();

    await act(async () => {
      resolveInterrupted?.({
        suggestions: [suggestion({ id: "interrupted-card" })],
        snapshot_revision: directorSnapshotRevision(snapshot()),
        requested_model: "gemini-3.1-pro-preview",
        model_used: "gemini-3.1-pro-preview",
        fallback_reason: null,
      });
      await Promise.resolve();
    });

    expect(suggestionsMock).toHaveBeenCalledTimes(3);
    expect(result.current.loading).toBe(false);
    expect(result.current.suggestions.map((item) => item.id)).toEqual(["hydrated-card"]);
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
        buildSnapshot: () => current,
        applyOpsAtomic: () => appliedResult(),
        onApplied: jest.fn(),
      }),
    );

    await act(async () => {
      jest.advanceTimersByTime(1200);
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

  it("restarts an in-flight initial review when async editor hydration changes the snapshot", async () => {
    let current = snapshot();
    let resolveFirst: ((value: Awaited<ReturnType<typeof editDirectorSuggestions>>) => void) | null = null;
    suggestionsMock
      .mockReturnValueOnce(new Promise((resolve) => {
        resolveFirst = resolve;
      }))
      .mockImplementationOnce(async (_itemId, _variantId, body) => ({
        suggestions: [suggestion({ id: "director-hydrated" })],
        snapshot_revision: body.snapshot_revision,
        requested_model: "gemini-3.1-pro-preview",
        model_used: "gemini-3.1-pro-preview",
        fallback_reason: null,
      }));

    const { result, rerender } = renderHook(() =>
      useEditDirector({
        enabled: true,
        omniEnabled: false,
        itemId: "item-1",
        variantId: "variant-1",
        buildSnapshot: () => current,
        applyOpsAtomic: () => appliedResult(),
        onApplied: jest.fn(),
      }),
    );

    await act(async () => {
      jest.advanceTimersByTime(1200);
    });
    current = snapshot("hydrated hook");
    rerender();

    await act(async () => {
      resolveFirst?.({
        suggestions: [suggestion({ id: "director-stale" })],
        snapshot_revision: directorSnapshotRevision(snapshot()),
        requested_model: "gemini-3.1-pro-preview",
        model_used: "gemini-3.1-pro-preview",
        fallback_reason: null,
      });
      jest.advanceTimersByTime(1200);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(suggestionsMock).toHaveBeenCalledTimes(2);
    expect(result.current.suggestions.map((item) => item.id)).toEqual([
      "director-hydrated",
    ]);
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
        buildSnapshot: () => current,
        applyOpsAtomic,
        onApplied,
        onGeneratedAssetReady,
      }),
    );

    await loadInitialReview();
    expect(suggestionsMock).toHaveBeenCalledWith(
      "item-1",
      "variant-1",
      expect.objectContaining({ omni_enabled: true }),
      expect.any(AbortSignal),
    );
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
