/**
 * Remove-music end-to-end (prod gap: the SoundsDrawer's "Remove music" button
 * existed but onRemoveMusic was never passed by EditorShell, so the click
 * silently no-oped — and the backend had no removal contract at all).
 *
 * Mounted through the REAL shell (house style of
 * EditorShell-undo-capability-gating): open the Sounds drawer, click
 * "Remove music", assert the UI reflects "no music" instantly (the removed
 * state hides the button and deselects the track), then Save and assert the
 * commit payload carries `remove_music: true` — never `music_track_id: null`
 * (which the server cannot distinguish from "omitted").
 */

import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
      },
    ],
  }),
}));

jest.mock("@/lib/sfx-api", () => ({
  ...jest.requireActual("@/lib/sfx-api"),
  getSoundEffects: jest.fn().mockResolvedValue([]),
}));

const mockCommitEditorSession = jest.fn();
jest.mock("@/lib/editor-commit", () => ({
  ...jest.requireActual("@/lib/editor-commit"),
  commitEditorSession: (...args: unknown[]) => mockCommitEditorSession(...args),
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
    variant_id: "song_text",
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
  mockCommitEditorSession.mockResolvedValue({
    ok: true,
    generation: "gen-next",
    sections: {},
  });
  await act(async () => {
    render(<EditorShell itemId="item-1" variantParam="song_text" />);
  });
}

afterEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
});

describe("EditorShell — remove music end-to-end", () => {
  it("Remove music fires the handler, reflects instantly, and commits remove_music", async () => {
    await renderShell();

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();

    // Open the Sounds drawer and wait for the track list.
    fireEvent.click(screen.getByRole("button", { name: /Sounds/ }));
    const removeBtn = await screen.findByRole("button", { name: "Remove music" });

    fireEvent.click(removeBtn);

    // UI reflects "no music" immediately: the removed state clears the
    // effective track, so the remove affordance disappears and Save arms.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Remove music" })).not.toBeInTheDocument(),
    );
    expect(save).toBeEnabled();

    await act(async () => {
      fireEvent.click(save);
    });

    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalled());
    const body = mockCommitEditorSession.mock.calls[0][2];
    expect(body.remove_music).toBe(true);
    expect(body.music_track_id).toBeUndefined();
    expect(body.music_window).toBeUndefined();
  });

  it("re-picking a track after removal swaps instead of removing", async () => {
    await renderShell();

    fireEvent.click(screen.getByRole("button", { name: /Sounds/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Remove music" }));

    // Picking the (still listed) track again cancels the removal.
    fireEvent.click(await screen.findByRole("button", { name: /Old Song/ }));
    await screen.findByRole("button", { name: "Remove music" });

    const save = screen.getByRole("button", { name: "Save" });
    await act(async () => {
      fireEvent.click(save);
    });

    // Re-picking the variant's own track is a no-op music section: no
    // remove_music, no music_track_id (it matches the persisted track).
    if (mockCommitEditorSession.mock.calls.length > 0) {
      const body = mockCommitEditorSession.mock.calls[0][2];
      expect(body.remove_music).toBeUndefined();
      expect(body.music_track_id).toBeUndefined();
    }
  });
});
