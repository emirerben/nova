import "@testing-library/jest-dom";
import React from "react";
import { act, render, screen } from "@testing-library/react";
import type { ApplyCopilotOpsResult } from "@/lib/edit-copilot/apply-ops";
import type { EditCopilotTurnResponse } from "@/lib/plan-api";
import type { UseEditDirectorOptions } from "@/lib/edit-copilot/useEditDirector";

/**
 * Bug 2 (PR8 E2E fix): on a render turn (set_intro_layout / apply_custom_effect)
 * with the chat steps feed on, EditorShell's handleCopilotOps used to hardcode
 * the assistant reply to "That's a re-render, not an instant edit — starting
 * it now.", silently discarding the agent's ACTUAL reply — which the prompt
 * requires to carry the feeling-label, the "can't be undone from chat"
 * disclosure, and "current version stays in history". This file pins the fix
 * directly against `handleCopilotOps` (exposed here as the director's
 * `onApplied`, the same function useEditCopilot's `onApplied` receives) --
 * the agent's real reply must win, and the hardcoded string must survive only
 * as the empty-reply fallback.
 */

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;
Object.defineProperty(HTMLMediaElement.prototype, "pause", {
  configurable: true,
  value: jest.fn(),
});
Object.defineProperty(HTMLMediaElement.prototype, "play", {
  configurable: true,
  value: jest.fn().mockResolvedValue(undefined),
});

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: jest.fn().mockImplementation((query: string) => ({
    matches: query.includes("min-width"),
    media: query,
    onchange: null,
    addListener: jest.fn(),
    removeListener: jest.fn(),
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    dispatchEvent: jest.fn(),
  })),
});

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn() }),
}));

const mockEditPlanItemVariant = jest.fn().mockResolvedValue({});
const mockApplyPlanItemCustomEffect = jest.fn().mockResolvedValue({});
jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  getPlanItem: jest.fn(),
  getPlanItemJobStatus: jest.fn(),
  editPlanItemVariant: (...args: unknown[]) => mockEditPlanItemVariant(...args),
  applyPlanItemCustomEffect: (...args: unknown[]) => mockApplyPlanItemCustomEffect(...args),
}));

jest.mock("@/app/plan/_components/useClipTimeline", () => ({
  useClipTimeline: () => ({
    state: {
      grid: [],
      clipDurations: {},
      baseline: [],
      slots: [],
      past: [],
      future: [],
      clampNonce: 0,
      clampedKey: null,
    },
    dispatch: jest.fn(),
    clips: [],
    windows: [],
    totalS: 0,
    loadState: "ready",
    reload: jest.fn(),
  }),
}));

let mockDirectorOptions: UseEditDirectorOptions | null = null;
jest.mock("@/lib/edit-copilot/useEditDirector", () => ({
  useEditDirector: jest.fn((options: UseEditDirectorOptions) => {
    mockDirectorOptions = options;
    return {
      suggestions: [],
      appliedReceipts: [],
      loading: false,
      error: null,
      unavailable: false,
      modelUsed: "",
      fallbackReason: null,
      generation: null,
      refresh: jest.fn(),
      accept: jest.fn(),
      dismiss: jest.fn(),
      revealApplied: jest.fn(),
      cancelGeneration: jest.fn(),
    };
  }),
}));

import EditorShell from "@/app/plan/items/[id]/_editor/EditorShell";
import {
  getPlanItem,
  getPlanItemJobStatus,
  type PlanItem,
  type PlanItemVariant,
} from "@/lib/plan-api";

const mockGetPlanItem = getPlanItem as jest.MockedFunction<typeof getPlanItem>;
const mockGetPlanItemJobStatus = getPlanItemJobStatus as jest.MockedFunction<
  typeof getPlanItemJobStatus
>;

const variant = {
  variant_id: "var-sub",
  output_url: "https://storage.example/variant.mp4",
  render_status: "ready",
  duration_s: 108,
  text_mode: "agent_text",
  resolved_archetype: "subtitled",
  text_elements: [
    {
      id: "closing",
      role: "generative_intro",
      text: "Closing line",
      start_s: 103.8,
      end_s: 107.8,
      font_family: "PlayfairDisplay-Bold",
    },
  ],
  editor_capabilities: {
    text_elements: true,
    timeline: true,
    split_clips: true,
    mix: true,
    sfx: true,
    overlays: true,
    suggestions: true,
  },
} as unknown as PlanItemVariant;

function result(overrides: Partial<ApplyCopilotOpsResult>): ApplyCopilotOpsResult {
  return {
    textActions: [],
    nextSlots: null,
    applied: [],
    rejected: [],
    ...overrides,
  };
}

function turnResponse(overrides: Partial<EditCopilotTurnResponse>): EditCopilotTurnResponse {
  return {
    intent: "edit",
    ops: [],
    confidence: 1,
    reply: "",
    suggestions: [],
    needs_clarification: false,
    ...overrides,
  };
}

// `handleCopilotOps` accepts an optional second (`response`) argument so it
// can be shared between useEditCopilot's 3-arg onApplied and useEditDirector's
// 1-arg onApplied contract — call it with 2 args here despite the captured
// UseEditDirectorOptions type only declaring 1.
type LooseOnApplied = (
  result: ApplyCopilotOpsResult,
  response?: EditCopilotTurnResponse,
) => { assistantText?: string; isRenderTurn?: boolean } | void;

/** Renders EditorShell and waits for the async plan-item/job-status load to
 * settle (mirrors EditorShell-director-preview.test.tsx's wait) so the
 * captured `onApplied` is called against a fully mounted, act-safe tree. */
async function renderLoaded(): Promise<LooseOnApplied> {
  await act(async () => {
    render(<EditorShell itemId="item-1" variantParam="var-sub" />);
  });
  await screen.findByRole("button", { name: /^Text row 1, Closing line,/ });
  return mockDirectorOptions?.onApplied as unknown as LooseOnApplied;
}

describe("EditorShell render-turn assistant reply (chat steps feed)", () => {
  const originalFlag = process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED;

  beforeEach(() => {
    mockDirectorOptions = null;
    process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
    mockGetPlanItem.mockResolvedValue({
      id: "item-1",
      theme: "Same production video",
      current_job_id: "job-1",
    } as unknown as PlanItem);
    mockGetPlanItemJobStatus.mockResolvedValue({
      variants: [variant],
    } as unknown as Awaited<ReturnType<typeof getPlanItemJobStatus>>);
  });

  afterEach(() => {
    jest.clearAllMocks();
    window.sessionStorage.clear();
    process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = originalFlag;
  });

  it("preserves the agent's real reply on a set_intro_layout render turn", async () => {
    const onApplied = await renderLoaded();
    let presentation;
    await act(async () => {
      presentation = onApplied(
        result({ renderRequest: { kind: "set_intro_layout", layout: "cluster" } }),
        turnResponse({
          reply:
            "Feeling: confident. Starting the re-render now — this can't be undone from chat, and your current version stays in history.",
        }),
      );
    });

    expect(presentation).toEqual(
      expect.objectContaining({
        isRenderTurn: true,
        assistantText:
          "Feeling: confident. Starting the re-render now — this can't be undone from chat, and your current version stays in history.",
      }),
    );
  });

  it("preserves the agent's real reply on an apply_custom_effect render turn", async () => {
    const onApplied = await renderLoaded();
    let presentation;
    await act(async () => {
      presentation = onApplied(
        result({
          renderRequest: {
            kind: "apply_custom_effect",
            effect: { id: "vintage", label: "Vintage", filters: [], start_s: 0, end_s: 5 },
          },
        }),
        turnResponse({
          reply:
            "Feeling: excited. Applying a vintage look now — this can't be undone from chat, and your current version stays in history.",
        }),
      );
    });

    expect(presentation).toEqual(
      expect.objectContaining({
        isRenderTurn: true,
        assistantText:
          "Feeling: excited. Applying a vintage look now — this can't be undone from chat, and your current version stays in history.",
      }),
    );
  });

  it("falls back to the hardcoded copy only when the agent's reply is empty", async () => {
    const onApplied = await renderLoaded();
    let presentation;
    await act(async () => {
      presentation = onApplied(
        result({ renderRequest: { kind: "set_intro_layout", layout: "linear" } }),
        turnResponse({ reply: "   " }),
      );
    });

    expect(presentation).toEqual(
      expect.objectContaining({
        isRenderTurn: true,
        assistantText: "That's a re-render, not an instant edit — starting it now.",
      }),
    );
  });

  it("falls back to the hardcoded copy when no response is supplied at all", async () => {
    const onApplied = await renderLoaded();
    let presentation;
    await act(async () => {
      presentation = onApplied(
        result({ renderRequest: { kind: "set_intro_layout", layout: "linear" } }),
      );
    });

    expect(presentation).toEqual(
      expect.objectContaining({
        isRenderTurn: true,
        assistantText: "That's a re-render, not an instant edit — starting it now.",
      }),
    );
  });
});
