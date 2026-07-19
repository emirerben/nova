process.env.NEXT_PUBLIC_VISUAL_BLOCKS_ENABLED = "true";

import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type {
  EditorCapabilities,
  PlanItem,
  PlanItemVariant,
  TextElement,
  VisualBlock,
} from "@/lib/plan-api";

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

const CAPABILITIES: EditorCapabilities = {
  text_elements: true,
  timeline: true,
  split_clips: true,
  mix: true,
  sfx: true,
  overlays: true,
  visual_blocks: true,
  suggestions: true,
};

const TEXT_CARD: VisualBlock = {
  version: 1,
  id: "card-1",
  kind: "text_card",
  start_s: 0,
  end_s: 4,
  timing_mode: "manual",
  origin: "ai",
  transition_in: "fade",
  transition_out: "fade",
  audio_policy: { base: "continue", sfx: "continue" },
  background: { type: "solid", color: "#111111" },
};

function linkedText(id: string, text: string): TextElement {
  return {
    id,
    text,
    role: "generative_intro",
    visual_block_id: TEXT_CARD.id,
    start_s: 0,
    end_s: 4,
    x_frac: 0.5,
    y_frac: 0.5,
  };
}

function makeVariant(textElements: TextElement[], visualBlocks: VisualBlock[] = [TEXT_CARD]) {
  return {
    variant_id: "song_text",
    output_url: "https://storage.example/variant.mp4",
    render_status: "ready",
    text_mode: "agent_text",
    style_set_id: null,
    intro_text_size_px: null,
    text_elements: textElements,
    visual_blocks: visualBlocks,
    resolved_archetype: "montage",
    render_generation_id: "gen-current",
    editor_capabilities: CAPABILITIES,
  } as unknown as PlanItemVariant;
}

async function renderShell(variant: PlanItemVariant) {
  mockGetPlanItem.mockResolvedValue(ITEM);
  mockGetPlanItemJobStatus.mockResolvedValue({
    variants: [variant],
  } as unknown as Awaited<ReturnType<typeof getPlanItemJobStatus>>);
  mockCommitEditorSession.mockResolvedValue({
    ok: true,
    generation: "gen-next",
    sections: { text_elements: true, visual_blocks: true },
  });
  await act(async () => {
    render(<EditorShell itemId="item-1" variantParam="song_text" />);
  });
}

afterEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
});

describe("EditorShell linked text-card deletion", () => {
  it("deletes the parent card with its final linked text, restores both with Undo, and saves both sections", async () => {
    await renderShell(makeVariant([linkedText("title-1", "Card title")]));

    fireEvent.click(screen.getByRole("button", { name: /^Text row 1, Card title,/ }));
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    expect(screen.queryByRole("button", { name: /^Text row 1, Card title,/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Text card,/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.getByRole("button", { name: /^Text row 1, Card title,/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Text card,/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalled());
    expect(mockCommitEditorSession.mock.calls[0][2]).toMatchObject({
      text_elements: [],
      visual_blocks: [],
    });
  });

  it("deleting one of multiple linked texts retains the card and sibling text", async () => {
    await renderShell(
      makeVariant([
        linkedText("title-1", "Primary title"),
        linkedText("title-2", "Supporting title"),
      ]),
    );

    fireEvent.click(screen.getByRole("button", { name: /^Text row 1, Primary title,/ }));
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    expect(screen.queryByRole("button", { name: /Primary title/ })).toBeNull();
    expect(screen.getByRole("button", { name: /Supporting title/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Text card,/ })).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });
    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalled());
    const body = mockCommitEditorSession.mock.calls[0][2];
    expect(body.text_elements).toHaveLength(1);
    expect(body.text_elements[0].id).toBe("title-2");
    expect(body.visual_blocks).toBeUndefined();
  });

  it("deleting the parent visual removes every linked text", async () => {
    await renderShell(
      makeVariant([
        linkedText("title-1", "Primary title"),
        linkedText("title-2", "Supporting title"),
      ]),
    );

    fireEvent.click(screen.getByRole("button", { name: /^Text card,/ }));
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    expect(screen.queryByRole("button", { name: /^Text card,/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Primary title/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Supporting title/ })).toBeNull();
  });

});
