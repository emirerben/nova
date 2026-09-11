/** Applying shared-chat edits must not reopen the removed mobile Kria drawer. */

import "@testing-library/jest-dom";
import { act, render, screen } from "@testing-library/react";
import type { ApplyCopilotOpsResult } from "@/lib/edit-copilot/apply-ops";
import type { UseEditDirectorOptions } from "@/lib/edit-copilot/useEditDirector";

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

// Force the mobile "light" layout (< 1024px) for every matchMedia query —
// this is the layout mode the reported bug is specific to.
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: jest.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: jest.fn(),
    removeListener: jest.fn(),
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    dispatchEvent: jest.fn(),
  })),
});

process.env.NEXT_PUBLIC_EDIT_COPILOT_ENABLED = "true";

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn() }),
}));

jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  getPlanItem: jest.fn(),
  getPlanItemJobStatus: jest.fn(),
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

let mockCopilotOptions: import("@/lib/edit-copilot/useEditCopilot").UseEditCopilotOptions | null = null;
jest.mock("@/lib/edit-copilot/useEditCopilot", () => {
  const actual = jest.requireActual("@/lib/edit-copilot/useEditCopilot");
  return { ...actual, useEditCopilot: (options: import("@/lib/edit-copilot/useEditCopilot").UseEditCopilotOptions) => {
    mockCopilotOptions = options;
    return actual.useEditCopilot(options);
  } };
});

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
  text_elements: [],
  editor_capabilities: {
    text_elements: true,
    timeline: true,
    split_clips: true,
    mix: true,
    sfx: true,
    overlays: true,
    visual_blocks: true,
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

describe("EditorShell — chat stays open through an applied edit (KRI-19 bug 11/14)", () => {
  beforeEach(() => {
    mockDirectorOptions = null;
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
  });

  it("preserves chat revision across shell rerenders and changes it for an applied edit", async () => {
    let view!: ReturnType<typeof render>;
    await act(async () => { view = render(<EditorShell itemId="item-1" variantParam="var-sub" />); });
    const before = mockCopilotOptions?.getDraftRevision?.();
    expect(before).toBeTruthy();
    await act(async () => { view.rerender(<EditorShell itemId="item-1" variantParam="var-sub" />); });
    expect(mockCopilotOptions?.getDraftRevision?.()).toBe(before);
    await act(async () => {
      await mockDirectorOptions?.onApplied(result({
        nextSfx: [{ id: "accent", sound_effect_id: "effect", src_gcs_path: "sfx/a.mp3", at_s: 0, gain: 0.75 }],
        applied: [{ label: "Sound", from: "none", to: "accent" }],
      }));
    });
    expect(mockCopilotOptions?.getDraftRevision?.()).not.toBe(before);
  });

  it("keeps the editor free of a second chat surface when a shared-chat edit lands", async () => {
    await act(async () => {
      render(<EditorShell itemId="item-1" variantParam="var-sub" />);
    });

    expect(screen.queryByRole("button", { name: "Kria tool" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("copilot-light")).not.toBeInTheDocument();
    expect(mockDirectorOptions).not.toBeNull();

    // An applied turn whose response carries `openTool: "visuals"` — the
    // exact signal that used to force-switch the dock away from "nova" and
    // unmount the chat drawer mid-turn, regardless of which mutation
    // triggered it.
    await act(async () => {
      await mockDirectorOptions?.onApplied(
        result({
          nextSfx: [
            {
              id: "hook-accent",
              sound_effect_id: "smart-visual-enter-accent-v1",
              src_gcs_path: "sound-effects/hook-accent.mp3",
              at_s: 0,
              gain: 0.75,
            },
          ],
          applied: [{ label: "Sound effect", from: "none", to: "Visual enter accent" }],
          openTool: "visuals",
        }),
      );
    });

    expect(screen.queryByRole("button", { name: "Kria tool" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("copilot-light")).not.toBeInTheDocument();
  });
});
