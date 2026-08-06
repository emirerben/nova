import "@testing-library/jest-dom";
import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
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
  text_elements: [{
    id: "closing",
    role: "generative_intro",
    text: "Closing line",
    start_s: 103.8,
    end_s: 107.8,
    font_family: "PlayfairDisplay-Bold",
  }],
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

describe("EditorShell Director acceptance preview", () => {
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

  it("shows the second accepted edit at its real moment after the first accept", async () => {
    await act(async () => {
      render(<EditorShell itemId="item-1" variantParam="var-sub" />);
    });
    await screen.findByRole("button", { name: /^Text row 1, Closing line,/ });

    const video = document.querySelector("video") as HTMLVideoElement;
    Object.defineProperty(video, "duration", { configurable: true, value: 108 });
    Object.defineProperty(video, "currentTime", {
      configurable: true,
      writable: true,
      value: 0,
    });
    fireEvent.loadedMetadata(video);

    let firstPresentation;
    await act(async () => {
      firstPresentation = mockDirectorOptions?.onApplied(result({
        nextSfx: [{
          id: "hook-accent",
          sound_effect_id: "smart-visual-enter-accent-v1",
          src_gcs_path: "sound-effects/hook-accent.mp3",
          at_s: 0,
          gain: 0.75,
        }],
        applied: [{ label: "Sound effect", from: "none", to: "Visual enter accent" }],
      }));
    });
    expect(firstPresentation).toEqual(expect.objectContaining({
      previewFocus: { kind: "sfx", id: "hook-accent", seekS: 0 },
    }));

    let secondPresentation;
    await act(async () => {
      secondPresentation = mockDirectorOptions?.onApplied(result({
        textActions: [{
          type: "PATCH_BAR",
          id: "closing",
          patch: { font_family: "Montserrat Bold" },
        }],
        applied: [{
          label: "Font",
          from: "PlayfairDisplay-Bold",
          to: "Montserrat Bold",
        }],
      }));
    });

    expect(secondPresentation).toEqual(expect.objectContaining({
      previewFocus: { kind: "text", id: "closing", seekS: 105.8 },
    }));
    expect(video.currentTime).toBeCloseTo(105.8, 3);
    const previewText = document.querySelector('[data-text-id="closing"]') as HTMLElement;
    expect(previewText).toBeInTheDocument();
    const styledText = previewText.firstElementChild as HTMLElement;
    expect(styledText.style.fontFamily).toContain("Montserrat");
  });
});
