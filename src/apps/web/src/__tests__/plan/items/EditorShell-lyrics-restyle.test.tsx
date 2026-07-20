/**
 * EditorShell Styles-drawer behaviour on a lyrics-synced variant.
 *
 * A lyrics variant (text_mode "lyrics", editor_capabilities.reason
 * "lyrics_sync") can't have per-element text edited — the captions are
 * injector-generated overlays timed to vocal onsets (lyric_injector.py), and
 * the backend independently 422s any text_elements payload for it
 * (validate_text_elements_payload). But a whole-style-set swap IS safe:
 * dispatch_change_style re-renders the variant, re-deriving lyric timing
 * deterministically from the track while only the visual style changes.
 *
 * These tests confirm the Styles drawer routes through that safe
 * `changePlanItemStyle` path for lyrics instead of the local bars/
 * text_elements patch every other variant type uses — and that a non-lyrics
 * variant is unaffected (control case).
 */

import "@testing-library/jest-dom";
import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

// jsdom lacks ResizeObserver (EditorCanvas / EditorTimelineBody measure loops).
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

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

Object.defineProperty(global, "fetch", {
  writable: true,
  value: jest.fn().mockRejectedValue(new Error("preview fetch unavailable in jsdom")),
});

const mediaPause = jest
  .spyOn(HTMLMediaElement.prototype, "pause")
  .mockImplementation(() => undefined);

const mockRouterPush = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockRouterPush }),
}));

const mockChangePlanItemStyle = jest.fn();
jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  getPlanItem: jest.fn(),
  getPlanItemJobStatus: jest.fn(),
  changePlanItemStyle: (...args: unknown[]) => mockChangePlanItemStyle(...args),
}));

jest.mock("@/lib/generative-api", () => ({
  ...jest.requireActual("@/lib/generative-api"),
  getGenerativeStyleSets: jest.fn(),
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

import EditorShell from "@/app/plan/items/[id]/_editor/EditorShell";
import {
  getPlanItem,
  getPlanItemJobStatus,
  type EditorCapabilities,
  type PlanItem,
  type PlanItemVariant,
} from "@/lib/plan-api";
import { getGenerativeStyleSets, type GenerativeStyleSet } from "@/lib/generative-api";

const mockGetPlanItem = getPlanItem as jest.MockedFunction<typeof getPlanItem>;
const mockGetPlanItemJobStatus = getPlanItemJobStatus as jest.MockedFunction<
  typeof getPlanItemJobStatus
>;
const mockGetGenerativeStyleSets = getGenerativeStyleSets as jest.MockedFunction<
  typeof getGenerativeStyleSets
>;

const ITEM = {
  id: "item-1",
  theme: "My video",
  current_job_id: "job-1",
} as unknown as PlanItem;

const STYLE_SET: GenerativeStyleSet = {
  id: "ocean_drift",
  label: "Ocean Drift",
  tags: [],
};

/** Real prod shape observed for job 2a00c97d-... (song_lyrics variant). */
const LYRICS_SYNC_CAPABILITIES: EditorCapabilities = {
  text_elements: false,
  timeline: false,
  split_clips: false,
  mix: false,
  sfx: false,
  overlays: false,
  suggestions: false,
  reason: "lyrics_sync",
  music_window: {
    editable: true,
    preserve_available: true,
    video_duration_s: 3,
    track_duration_s: 12,
    recommended_start_s: 4,
    beat_timestamps_s: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    reason: null,
    preserve_reason: null,
  },
};

const EDITABLE_CAPABILITIES: EditorCapabilities = {
  text_elements: true,
  timeline: true,
  split_clips: true,
  mix: true,
  sfx: true,
  overlays: true,
  suggestions: true,
};

function makeLyricsVariant(): PlanItemVariant {
  return {
    variant_id: "song_lyrics",
    output_url: "https://storage.example/variant.mp4",
    render_status: "ready",
    text_mode: "lyrics",
    style_set_id: "ocean_drift",
    intro_text_size_px: null,
    text_elements: [],
    resolved_archetype: null,
    music_track_id: "track-1",
    music_preview_url: "https://storage.example/track.m4a",
    music_preview_start_s: 4,
    editor_capabilities: LYRICS_SYNC_CAPABILITIES,
  } as unknown as PlanItemVariant;
}

function makeAgentTextVariant(): PlanItemVariant {
  return {
    variant_id: "song_text",
    output_url: "https://storage.example/variant.mp4",
    render_status: "ready",
    text_mode: "agent_text",
    style_set_id: "ocean_drift",
    intro_text_size_px: null,
    text_elements: [
      {
        id: "title-1",
        role: "generative_intro",
        text: "Title",
        start_s: 0,
        end_s: 4,
        x_frac: 0.5,
        y_frac: 0.5,
      },
    ],
    resolved_archetype: "montage",
    editor_capabilities: EDITABLE_CAPABILITIES,
  } as unknown as PlanItemVariant;
}

async function renderShell(variant: PlanItemVariant) {
  mockGetPlanItem.mockResolvedValue(ITEM);
  mockGetPlanItemJobStatus.mockResolvedValue({
    variants: [variant],
  } as unknown as Awaited<ReturnType<typeof getPlanItemJobStatus>>);
  mockGetGenerativeStyleSets.mockResolvedValue([STYLE_SET]);
  await act(async () => {
    render(<EditorShell itemId="item-1" variantParam={variant.variant_id} />);
  });
}

async function openStylesAndPick() {
  fireEvent.click(screen.getByRole("button", { name: "Styles tool" }));
  const chip = await screen.findByRole("radio", { name: "Text style: Ocean Drift" });
  await act(async () => {
    fireEvent.click(chip);
  });
}

afterEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
});

afterAll(() => mediaPause.mockRestore());

describe("EditorShell — Styles drawer on a lyrics-synced variant", () => {
  it("Text tool is locked while Styles stays enabled", async () => {
    await renderShell(makeLyricsVariant());

    expect(screen.getByRole("button", { name: "Text tool" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.getByRole("button", { name: "Styles tool" })).toBeEnabled();
  });

  it("picking a style calls changePlanItemStyle and hands off to the item page, bypassing the bars/text_elements path", async () => {
    mockChangePlanItemStyle.mockResolvedValue(ITEM);
    await renderShell(makeLyricsVariant());

    await openStylesAndPick();

    expect(mockChangePlanItemStyle).toHaveBeenCalledWith("item-1", "song_lyrics", "ocean_drift");
    expect(mockRouterPush).toHaveBeenCalledWith("/plan/items/item-1");
    // The bars-patch path never ran: Save never lit up dirty, no draft was staged.
    expect(window.sessionStorage.getItem("nova-editor-draft:song_lyrics")).toBeNull();
  });

  it("shows an error and stays on the editor when the style-change call fails", async () => {
    mockChangePlanItemStyle.mockRejectedValue(new Error("Backend unavailable"));
    await renderShell(makeLyricsVariant());

    await openStylesAndPick();

    expect(mockRouterPush).not.toHaveBeenCalled();
    expect(screen.getByText("Backend unavailable")).toBeInTheDocument();
  });

  it("previews a moved song window over the rendered lyrics video", async () => {
    await renderShell(makeLyricsVariant());

    fireEvent.click(screen.getByRole("button", { name: "Sounds tool" }));
    const slider = await screen.findByRole("slider", { name: "Song section start" });
    fireEvent.change(slider, { target: { value: "5" } });

    const audio = await screen.findByTestId("rendered-music-window-preview");
    expect(audio).toHaveAttribute("src", "https://storage.example/track.m4a");
    await waitFor(() => {
      expect(document.querySelector("video")?.muted).toBe(true);
    });
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });
});

describe("EditorShell — Styles drawer control: non-lyrics variant is unaffected", () => {
  it("picking a style patches bars locally instead of calling changePlanItemStyle", async () => {
    await renderShell(makeAgentTextVariant());

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();

    await openStylesAndPick();

    expect(mockChangePlanItemStyle).not.toHaveBeenCalled();
    expect(mockRouterPush).not.toHaveBeenCalled();
    expect(save).toBeEnabled();
  });
});
