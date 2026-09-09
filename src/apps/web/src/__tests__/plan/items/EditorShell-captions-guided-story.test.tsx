/**
 * EditorShell — guided-story narration captions (KRI-18).
 *
 * Guided-story voiceover captions are persisted TextElements
 * (role "generative_sequence" + source_params.source === "caption_cue"),
 * not caption_cues — they have no Captions drawer. This suite covers the
 * end-to-end classification + selection fix: clicking a caption selects it
 * and opens the caption inspector (not the generic text one, not a broken
 * drawer collision), switching between captions retargets cleanly, and the
 * lane stays expanded once opened.
 */
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
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

const mockRouterPush = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockRouterPush }),
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

const EDITABLE_CAPABILITIES: EditorCapabilities = {
  text_elements: true,
  timeline: false,
  split_clips: false,
  mix: false,
  sfx: false,
  overlays: false,
  suggestions: false,
  orientation: { editable: false, value: "portrait", reason: null },
};

function narrationCaptionElement(index: number) {
  return {
    id: `narration-caption-${index}`,
    text: `Caption number ${index}`,
    start_s: index * 2,
    end_s: index * 2 + 1.5,
    role: "generative_sequence",
    position: "custom",
    x_frac: 0.5,
    y_frac: 0.82,
    font_family: "Inter-Bold",
    size_px: 58,
    color: "#FFFFFF",
    source_params: {
      source: "caption_cue",
      key: `${index}`,
      identity: `pinned-narration-caption-${index}`,
    },
    word_timings: [{ text: `word${index}`, start_s: index * 2, end_s: index * 2 + 1.5 }],
  };
}

function makeGuidedStoryVariant(): PlanItemVariant {
  return {
    variant_id: "guided_story",
    output_url: "https://storage.example/variant.mp4",
    render_status: "ready",
    text_mode: "agent_text",
    orientation: "portrait",
    style_set_id: null,
    intro_text_size_px: null,
    resolved_archetype: "guided_story",
    render_generation_id: "gen-current",
    editor_capabilities: EDITABLE_CAPABILITIES,
    text_elements: [narrationCaptionElement(0), narrationCaptionElement(1)],
  } as unknown as PlanItemVariant;
}

async function renderShell(variant: PlanItemVariant = makeGuidedStoryVariant()) {
  mockGetPlanItem.mockResolvedValue(ITEM);
  mockGetPlanItemJobStatus.mockResolvedValue({
    variants: [variant],
  } as unknown as Awaited<ReturnType<typeof getPlanItemJobStatus>>);
  mockCommitEditorSession.mockResolvedValue({
    ok: true,
    generation: "gen-next",
    sections: { text_elements: true },
  });
  await act(async () => {
    render(<EditorShell itemId="item-1" variantParam="guided_story" />);
  });
}

afterEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
});

describe("EditorShell — guided-story narration captions (KRI-18)", () => {
  it("clicking a caption selects it, opens the caption inspector, and never shows the (unreachable) Captions drawer", async () => {
    await renderShell();

    const captionsLane = screen.getByTestId("editor-captions-lane");
    // Lane starts collapsed — click its density tick for the first caption.
    fireEvent.click(within(captionsLane).getByRole("button", { name: /Caption at.*Caption number 0/ }));

    // AC3: the caption inspector opens with the "Captions" heading, not "Text".
    expect(await screen.findByRole("heading", { name: /Captions/ })).toBeInTheDocument();
    // Font/size/color controls (the ordinary text styling — AC5's home for
    // guided-story captions, which have no cue-only "This caption" section).
    expect(screen.getByText("Fill")).toBeInTheDocument();
    // Cue-only sections must NOT appear — there is no CaptionCue behind this bar.
    expect(screen.queryByText("This caption")).not.toBeInTheDocument();
    expect(screen.queryByText("Edit all captions")).not.toBeInTheDocument();
    // Timing is server-pinned: Start/End are disabled with an honest reason.
    expect(screen.getByLabelText("Start seconds")).toBeDisabled();
    expect(screen.getByLabelText("End seconds")).toBeDisabled();
    expect(screen.getByText("Caption timing follows your narration.")).toBeInTheDocument();

    // The Captions drawer (cue-only tool) never opens for guided_story.
    expect(screen.queryByText("Find in captions")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/Find/)).not.toBeInTheDocument();
  });

  it("switching between captions retargets the inspector cleanly, and the lane stays expanded (AC3, AC7)", async () => {
    await renderShell();

    const captionsLane = screen.getByTestId("editor-captions-lane");
    fireEvent.click(within(captionsLane).getByRole("button", { name: /Caption at.*Caption number 0/ }));
    expect(await screen.findByDisplayValue("Caption number 0")).toBeInTheDocument();

    // Now expanded: click the SECOND caption's full row button.
    fireEvent.click(screen.getByRole("button", { name: /^Caption 2,/ }));

    // No stale text — the inspector retargets to the newly selected caption.
    expect(await screen.findByDisplayValue("Caption number 1")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Caption number 0")).not.toBeInTheDocument();

    // The lane is still expanded (both captions render as full rows, not the
    // collapsed density strip) — selecting didn't collapse it back.
    expect(
      within(screen.getByTestId("editor-captions-lane")).getAllByRole("button", {
        name: /^Caption \d/,
      }),
    ).toHaveLength(2);
  });
});
