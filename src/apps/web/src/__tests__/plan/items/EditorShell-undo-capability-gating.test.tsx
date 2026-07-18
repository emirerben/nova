/**
 * Regression test for the undo/redo blanket-dirty bug: restoring a document
 * snapshot (applyDocument in EditorShell) used to mark EVERY section dirty
 * regardless of what the active variant's editor_capabilities allow. On a
 * variant where visual_blocks capability is false (e.g. a lyrics variant, or
 * one with no clean base), the next Save then shipped an untouched
 * `visual_blocks: []` echo — which the backend editor-commit guard 422s the
 * WHOLE commit for, even though the user never touched visual blocks.
 *
 * Mounted through the REAL shell (house style of EditorShell-orientation /
 * EditorShell-text-lock): two independent, non-coalescing history commands
 * (video-mute toggles carry no coalesce tag) leave a non-empty undo stack
 * after ONE undo, so Save stays enabled and the undo path (applyDocument) is
 * actually exercised before the commit payload is inspected.
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

// Lyrics-variant-shaped capabilities: everything else editable, but
// visual_blocks is false (the server's `lyrics_variant` reason).
const LYRICS_LIKE_CAPABILITIES: EditorCapabilities = {
  text_elements: false,
  timeline: false,
  split_clips: false,
  mix: false,
  sfx: true,
  overlays: true,
  visual_blocks: false,
  suggestions: false,
  reason: "lyrics_sync",
  visual_blocks_reason: "lyrics_variant",
};

function makeVariant(): PlanItemVariant {
  return {
    variant_id: "song_lyrics",
    output_url: "https://storage.example/variant.mp4",
    render_status: "ready",
    text_mode: "lyrics",
    style_set_id: null,
    intro_text_size_px: null,
    text_elements: [],
    resolved_archetype: "montage",
    render_generation_id: "gen-current",
    editor_capabilities: LYRICS_LIKE_CAPABILITIES,
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
    render(<EditorShell itemId="item-1" variantParam="song_lyrics" />);
  });
}

afterEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
});

describe("EditorShell — undo/redo respects per-section capability gating", () => {
  it("does not ship visual_blocks after an undo-restore on a variant that can't accept blocks", async () => {
    await renderShell();

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();

    // Two independent, non-coalescing history commands (video-mute toggle
    // records with no tag, so neither collapses into the other).
    const muteToggle = screen.getByRole("button", { name: "Original audio audible" });
    fireEvent.click(muteToggle); // push #1 (doc: unmuted)
    fireEvent.click(screen.getByRole("button", { name: "Original audio muted" })); // push #2 (doc: muted)
    expect(save).toBeEnabled();

    // ONE undo restores the state right after push #1 — still dirty (the
    // undo stack isn't empty), which is exactly the applyDocument path that
    // used to blanket-mark every section dirty.
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(save).toBeEnabled();

    await act(async () => {
      fireEvent.click(save);
    });

    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalled());
    const body = mockCommitEditorSession.mock.calls[0][2];
    expect(body.visual_blocks).toBeUndefined();
  });
});
