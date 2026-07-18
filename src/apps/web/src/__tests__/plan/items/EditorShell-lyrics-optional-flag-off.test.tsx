/**
 * Lyrics-optional "elements" model with NEXT_PUBLIC_LYRICS_OPTIONAL_ENABLED
 * left UNSET (jest.setup.ts default — see EditorShell-lyrics-optional.test.tsx
 * for the flag-on suite). A variant whose backend capabilities already carry
 * lyrics_model: "elements" must render byte-identical to a variant with no
 * lyrics capability at all when the FE flag is off: no Lyrics toggle, no
 * lyric-seeds fetch, ordinary text_elements-only commit.
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

const mockGetLyricSeeds = jest.fn();
jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  getPlanItem: jest.fn(),
  getPlanItemJobStatus: jest.fn(),
  getLyricSeeds: (...args: unknown[]) => mockGetLyricSeeds(...args),
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

import EditorShell from "@/app/plan/items/[id]/_editor/EditorShell";
import { getPlanItem, getPlanItemJobStatus } from "@/lib/plan-api";

const mockGetPlanItem = getPlanItem as jest.MockedFunction<typeof getPlanItem>;
const mockGetPlanItemJobStatus = getPlanItemJobStatus as jest.MockedFunction<
  typeof getPlanItemJobStatus
>;

const ITEM = {
  id: "item-1",
  theme: "My video",
  current_job_id: "job-1",
} as unknown as PlanItem;

const ELEMENTS_MODEL_CAPABILITIES: EditorCapabilities = {
  text_elements: true,
  timeline: true,
  split_clips: true,
  mix: true,
  sfx: true,
  overlays: true,
  visual_blocks: true,
  suggestions: true,
  lyrics: {
    editable: true,
    enabled: false,
    can_toggle_on: true,
    reason: null,
    lyrics_model: "elements",
  },
};

function makeElementsVariant(): PlanItemVariant {
  return {
    variant_id: "montage-1",
    output_url: "https://storage.example/variant.mp4",
    render_status: "ready",
    text_mode: "agent_text",
    style_set_id: null,
    intro_text_size_px: null,
    text_elements: [
      {
        id: "title-1",
        role: "generative_intro",
        text: "Title",
        start_s: 0,
        end_s: 2,
        x_frac: 0.5,
        y_frac: 0.5,
      },
    ],
    resolved_archetype: "montage",
    render_generation_id: "gen-current",
    editor_capabilities: ELEMENTS_MODEL_CAPABILITIES,
  } as unknown as PlanItemVariant;
}

afterEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
});

describe("EditorShell — lyrics-optional flag OFF", () => {
  it("renders no Lyrics toggle for an elements-model variant and never fetches seeds", async () => {
    mockGetPlanItem.mockResolvedValue(ITEM);
    mockGetPlanItemJobStatus.mockResolvedValue({
      variants: [makeElementsVariant()],
    } as unknown as Awaited<ReturnType<typeof getPlanItemJobStatus>>);
    mockCommitEditorSession.mockResolvedValue({ ok: true, generation: "gen-next", sections: {} });

    await act(async () => {
      render(<EditorShell itemId="item-1" variantParam="montage-1" />);
    });

    fireEvent.click(screen.getByRole("button", { name: "Text tool" }));
    expect(screen.queryByRole("switch", { name: "Lyrics" })).toBeNull();
    expect(mockGetLyricSeeds).not.toHaveBeenCalled();

    // Ordinary edit still works and commits without any lyrics-shaped payload.
    fireEvent.click(screen.getByRole("button", { name: "Add text" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalled());
    const body = mockCommitEditorSession.mock.calls[0][2];
    expect(body.lyrics).toBeUndefined();
    const ids = (body.text_elements as Array<{ id: string }>).map((el) => el.id);
    expect(ids).not.toContain("lyr-L0");
  });
});
