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

jest.mock("@/app/plan/items/[id]/_editor/MotionCanvasLayer", () => ({ __esModule: true, default: () => null }));
process.env.NEXT_PUBLIC_MOTION_SCENES_ENABLED = "true";
const EditorShell = require("@/app/plan/items/[id]/_editor/EditorShell").default;
import { createCreatorBlockInstance } from "@nova/motion-runtime";
const mockCommit = jest.fn();
jest.mock("@/lib/editor-commit", () => ({
  ...jest.requireActual("@/lib/editor-commit"),
  commitEditorSession: (...args: unknown[]) => mockCommit(...args),
}));
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

const motion = createCreatorBlockInstance({ id: "block-1", presetId: "kinetic_word", startFrame: 0, endFrameExclusive: 60 });
motion.text_appearance = { stroke_width: 2, shadow_enabled: true };
const variant = {
  variant_id: "var-sub", output_url: "https://storage.example/video.mp4",
  base_video_url: "https://storage.example/base.mp4", base_video_path: "users/u/base.mp4", render_status: "ready", duration_s: 4,
  text_mode: "agent_text", resolved_archetype: "subtitled", render_generation_id: "gen-current",
  caption_cues: [{ text: "Caption", start_s: 0, end_s: 3 }],
  caption_stroke_width: 4, caption_shadow_enabled: true,
  text_elements: [{ id: "title", role: "generative_intro", text: "Title", start_s: 0, end_s: 3, stroke_width: 3, shadow_enabled: true }],
  motion_scenes: [motion],
  editor_capabilities: { text_elements: true, timeline: true, split_clips: true, mix: true,
    sfx: true, overlays: true, suggestions: true, motion_scenes: true, text_appearance_version: 1 },
} as unknown as PlanItemVariant;

afterEach(() => { jest.clearAllMocks(); window.sessionStorage.clear(); });
it("stages every text lane in one Undo/Redo transaction and saves zero/false", async () => {
  mockGetPlanItem.mockResolvedValue({ id: "item-1", theme: "Video", current_job_id: "job-1" } as PlanItem);
  mockGetPlanItemJobStatus.mockResolvedValue({ variants: [variant] } as Awaited<ReturnType<typeof getPlanItemJobStatus>>);
  mockCommit.mockResolvedValue({ ok: true, generation: "gen-next", sections: {} });
  await act(async () => { render(<EditorShell itemId="item-1" variantParam="var-sub" />); });
  const video = document.querySelector("video")!;
  Object.defineProperty(video, "duration", { configurable: true, value: 4 });
  fireEvent.loadedMetadata(video);
  const options = mockDirectorOptions!;
  const before = options.buildSnapshot();
  const result = options.applyOpsAtomic([{
    op: "patch_text_appearance", text_appearance_version: 1,
    selector: { scope: "editable_text", quantifier: "all" },
    patch: { stroke_width: 0, shadow_enabled: false },
  }], before);
  expect(result.rejected).toEqual([]);
  expect(result.nextMotionScenes?.[0].text_appearance).toEqual({ stroke_width: 0, shadow_enabled: false });
  await act(async () => { options.onApplied(result); });
  const staged = mockDirectorOptions!.buildSnapshot();
  expect(staged.text_appearance?.targets.every(t => t.values.stroke_width === 0 && t.values.shadow_enabled === false)).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  expect(mockDirectorOptions!.buildSnapshot().text_appearance).toEqual(before.text_appearance);
  fireEvent.click(screen.getByRole("button", { name: "Redo" }));
  expect(mockDirectorOptions!.buildSnapshot().text_appearance).toEqual(staged.text_appearance);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Save" })); });
  expect(mockCommit).toHaveBeenCalled();
  const body = mockCommit.mock.calls[0][2];
  expect(body.caption_meta).toEqual(expect.objectContaining({ stroke_width: 0, shadow_enabled: false }));
  expect(body.caption_cues[0]).toEqual(expect.objectContaining({ stroke_width: 0, shadow_enabled: false }));
  expect(body.text_elements[0]).toEqual(expect.objectContaining({ stroke_width: 0, shadow_enabled: false }));
  expect(body.motion_scenes[0].text_appearance).toEqual({ stroke_width: 0, shadow_enabled: false });
});
