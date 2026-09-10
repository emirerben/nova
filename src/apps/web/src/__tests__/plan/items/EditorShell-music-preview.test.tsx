/**
 * KRI-19 bug 7: "switching tracks produces no sound at all."
 *
 * Root cause: preview-audio requesting was keyed off `musicWindowDirty`
 * (`!!songWindowState && musicDirty`), and `songWindowState` is null unless
 * the backend populates `capabilities.music_window` — which it only does for
 * the `song_text`/`song_lyrics` variant ids or a guided-story revision. For
 * every other variant (the common case — this fixture uses `original_text`,
 * with no `music_window` capability, mirroring most real variants), picking a
 * different track set `musicDirty` but never `musicWindowDirty`, so no
 * preview was ever requested — the swap played silently.
 *
 * Mounted through the REAL shell (house style of
 * EditorShell-remove-music.test.tsx): pick a different track and assert the
 * preview `<audio>` element actually mounts, and that playback is kicked off
 * even though the video starts paused (the second half of bug 7 — preview
 * audio only played `if (!video.paused)`).
 */

import "@testing-library/jest-dom";
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { EditorCapabilities, PlanItem, PlanItemVariant } from "@/lib/plan-api";

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

// The blob-cache effect fetches the remote preview URL best-effort; a
// rejection is the documented fallback path (keep streaming the remote URL)
// and keeps this test off the network.
global.fetch = jest.fn().mockRejectedValue(new Error("no network in tests"));

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn() }),
}));

jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  getPlanItem: jest.fn(),
  getPlanItemJobStatus: jest.fn(),
}));

jest.mock("@/lib/music-api", () => ({
  ...jest.requireActual("@/lib/music-api"),
  getMusicTracks: jest.fn().mockResolvedValue({
    tracks: [
      {
        id: "t1",
        title: "Old Song",
        artist: "Nova",
        duration_s: 30,
        preview_start_s: 0,
        preview_audio_url: "https://storage.example/t1.m4a",
      },
      {
        id: "t2",
        title: "New Song",
        artist: "Nova",
        duration_s: 30,
        preview_start_s: 0,
        preview_audio_url: "https://storage.example/t2.m4a",
      },
    ],
  }),
}));

jest.mock("@/lib/sfx-api", () => ({
  ...jest.requireActual("@/lib/sfx-api"),
  getSoundEffects: jest.fn().mockResolvedValue([]),
}));

jest.mock("@/lib/editor-commit", () => ({
  ...jest.requireActual("@/lib/editor-commit"),
  commitEditorSession: jest.fn().mockResolvedValue({ ok: true, generation: "gen-next", sections: {} }),
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

const EditorShell =
  require("@/app/plan/items/[id]/_editor/EditorShell").default as typeof import("@/app/plan/items/[id]/_editor/EditorShell").default;
const { getPlanItem, getPlanItemJobStatus } = require("@/lib/plan-api") as {
  getPlanItem: typeof import("@/lib/plan-api").getPlanItem;
  getPlanItemJobStatus: typeof import("@/lib/plan-api").getPlanItemJobStatus;
};

const mockGetPlanItem = getPlanItem as jest.MockedFunction<typeof getPlanItem>;
const mockGetPlanItemJobStatus = getPlanItemJobStatus as jest.MockedFunction<
  typeof getPlanItemJobStatus
>;

const ITEM = {
  id: "item-1",
  theme: "My video",
  current_job_id: "job-1",
} as unknown as PlanItem;

// No `music_window` capability — the common case for a variant that isn't
// song_text/song_lyrics and carries no guided-story revision.
const CAPABILITIES: EditorCapabilities = {
  text_elements: true,
  timeline: true,
  split_clips: true,
  mix: false,
  sfx: true,
  overlays: true,
  visual_blocks: false,
  suggestions: false,
  reason: null,
} as unknown as EditorCapabilities;

function makeVariant(): PlanItemVariant {
  return {
    variant_id: "original_text",
    output_url: "https://storage.example/variant.mp4",
    render_status: "ready",
    text_mode: "agent_text",
    style_set_id: null,
    intro_text_size_px: null,
    text_elements: [],
    music_track_id: "t1",
    track_title: "Old Song",
    resolved_archetype: "montage",
    render_generation_id: "gen-current",
    editor_capabilities: CAPABILITIES,
  } as unknown as PlanItemVariant;
}

async function renderShell() {
  mockGetPlanItem.mockResolvedValue(ITEM);
  mockGetPlanItemJobStatus.mockResolvedValue({
    variants: [makeVariant()],
  } as unknown as Awaited<ReturnType<typeof getPlanItemJobStatus>>);
  await act(async () => {
    render(<EditorShell itemId="item-1" variantParam="original_text" />);
  });
}

afterEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
});

describe("EditorShell — music track swap requests an audible preview (KRI-19 bug 7)", () => {
  it("mounts the preview audio element and attempts playback when a different track is picked while paused", async () => {
    const playSpy = jest
      .spyOn(window.HTMLMediaElement.prototype, "play")
      .mockImplementation(() => Promise.resolve());
    const pauseSpy = jest
      .spyOn(window.HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => {});

    await renderShell();

    expect(
      screen.queryByTestId("rendered-music-window-preview"),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Sounds/ }));
    const newTrackButton = await screen.findByRole("button", { name: /New Song/ });

    await act(async () => {
      fireEvent.click(newTrackButton);
    });

    // The preview element now mounts — this is the concrete, observable
    // signal that a preview was requested at all (before the fix,
    // musicWindowDirty stayed false for this variant and it never did).
    expect(
      await screen.findByTestId("rendered-music-window-preview"),
    ).toBeInTheDocument();

    // The video started paused; picking a track is a deliberate "let me
    // hear it" gesture and must not require the user to separately hit
    // play first.
    expect(playSpy).toHaveBeenCalled();

    playSpy.mockRestore();
    pauseSpy.mockRestore();
  });
});
