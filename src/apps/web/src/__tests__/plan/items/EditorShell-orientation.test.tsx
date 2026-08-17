process.env.NEXT_PUBLIC_LANDSCAPE_OUTPUT_ENABLED = "true";

import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { EditorCommitConflictError } from "@/lib/editor-commit";
import type {
  EditorCapabilities,
  PlanItem,
  PlanItemVariant,
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
  timeline: true,
  split_clips: true,
  mix: true,
  sfx: true,
  overlays: true,
  suggestions: true,
  orientation: {
    editable: true,
    value: "landscape",
    reason: null,
  },
};

function makeVariant(overrides: Partial<PlanItemVariant> = {}): PlanItemVariant {
  return {
    variant_id: "song_text",
    output_url: "https://storage.example/variant.mp4",
    render_status: "ready",
    text_mode: "agent_text",
    orientation: "landscape",
    style_set_id: null,
    intro_text_size_px: null,
    text_elements: [],
    resolved_archetype: "montage",
    render_generation_id: "gen-current",
    editor_capabilities: EDITABLE_CAPABILITIES,
    ...overrides,
  } as unknown as PlanItemVariant;
}

async function renderShell(variant: PlanItemVariant = makeVariant()) {
  mockGetPlanItem.mockResolvedValue(ITEM);
  mockGetPlanItemJobStatus.mockResolvedValue({
    variants: [variant],
  } as unknown as Awaited<ReturnType<typeof getPlanItemJobStatus>>);
  mockCommitEditorSession.mockResolvedValue({
    ok: true,
    generation: "gen-next",
    sections: { orientation: true },
  });
  await act(async () => {
    render(<EditorShell itemId="item-1" variantParam="song_text" />);
  });
}

afterEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
});

describe("EditorShell orientation", () => {
  it("seeds from the variant and restores orientation through the document snapshot", async () => {
    await renderShell();

    expect(screen.getByRole("button", { name: "Use 16:9 output" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(document.querySelector("video")).toHaveClass("object-cover");
    expect(screen.getByRole("region", { name: "Video canvas, 16:9 landscape" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Use 9:16 output" }));
    expect(screen.getByRole("button", { name: "Use 9:16 output" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(document.querySelector("video")).toHaveClass("object-contain");
    expect(screen.getByRole("region", { name: "Video canvas, 9:16 portrait" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.getByRole("button", { name: "Use 16:9 output" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(document.querySelector("video")).toHaveClass("object-cover");
  });

  it("uses the server's actual format and keeps landscape enabled for a guided story", async () => {
    await renderShell(
      makeVariant({
        variant_id: "guided_story",
        orientation: undefined,
        resolved_archetype: "guided_story",
        editor_capabilities: {
          ...EDITABLE_CAPABILITIES,
          timeline: false,
          split_clips: false,
          orientation: { editable: true, value: "landscape", reason: null },
        },
      }),
    );

    expect(screen.getByRole("button", { name: "Use 16:9 output" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Use 9:16 output" })).toBeEnabled();
    expect(screen.getByRole("region", { name: "Video canvas, 16:9 landscape" })).toBeVisible();
  });

  it("locks the format switch while Save is in flight and restores it after an error", async () => {
    let rejectSave: (reason?: unknown) => void = () => {};
    mockCommitEditorSession.mockImplementationOnce(
      () => new Promise((_, reject) => { rejectSave = reject; }),
    );
    await renderShell();

    fireEvent.click(screen.getByRole("button", { name: "Use 9:16 output" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(screen.getByRole("group", { name: "Output format" })).toHaveAttribute(
        "aria-busy",
        "true",
      );
    });
    expect(screen.getByRole("button", { name: "Use 9:16 output" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Use 16:9 output" })).toBeDisabled();

    await act(async () => {
      rejectSave(new Error("Couldn't rebuild this format."));
    });

    expect(await screen.findByText("Couldn't rebuild this format.")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Output format" })).toHaveAttribute(
      "aria-busy",
      "false",
    );
    expect(screen.getByRole("button", { name: "Use 16:9 output" })).toBeEnabled();
  });

  it("forgets the recovery draft after a successful orientation save", async () => {
    await renderShell();

    fireEvent.click(screen.getByRole("button", { name: "Use 9:16 output" }));
    await waitFor(() => {
      expect(
        window.sessionStorage.getItem("nova-editor-draft:item-1:song_text"),
      ).not.toBeNull();
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    await waitFor(() => expect(mockRouterPush).toHaveBeenCalled());
    expect(mockCommitEditorSession.mock.calls[0][2]).toMatchObject({
      orientation: "portrait",
    });
    expect(
      window.sessionStorage.getItem("nova-editor-draft:item-1:song_text"),
    ).toBeNull();
    expect(screen.queryByText("Resume your unsaved edits?")).toBeNull();
  });

  it("keeps the recovery draft when persistence succeeds but rendering does not start", async () => {
    await renderShell();
    mockCommitEditorSession.mockResolvedValueOnce({
      ok: false,
      generation: "gen-next",
      sections: { orientation: true },
    });

    fireEvent.click(screen.getByRole("button", { name: "Use 9:16 output" }));
    await waitFor(() => {
      expect(
        window.sessionStorage.getItem("nova-editor-draft:item-1:song_text"),
      ).not.toBeNull();
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    expect(
      await screen.findByText("Saved, but rendering didn't start."),
    ).toBeInTheDocument();
    expect(mockRouterPush).not.toHaveBeenCalled();
    expect(
      window.sessionStorage.getItem("nova-editor-draft:item-1:song_text"),
    ).not.toBeNull();
  });

  it("keeps the recovery draft when the save request reports an error", async () => {
    await renderShell();
    mockCommitEditorSession.mockRejectedValueOnce(new Error("Render queue unavailable"));

    fireEvent.click(screen.getByRole("button", { name: "Use 9:16 output" }));
    await waitFor(() => {
      expect(
        window.sessionStorage.getItem("nova-editor-draft:item-1:song_text"),
      ).not.toBeNull();
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    expect(await screen.findByText("Render queue unavailable")).toBeInTheDocument();
    expect(mockRouterPush).not.toHaveBeenCalled();
    expect(
      window.sessionStorage.getItem("nova-editor-draft:item-1:song_text"),
    ).not.toBeNull();
  });

  it("keeps the recovery draft when the save conflicts", async () => {
    await renderShell();
    mockCommitEditorSession.mockRejectedValueOnce(
      new EditorCommitConflictError("This video changed in another tab"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Use 9:16 output" }));
    await waitFor(() => {
      expect(
        window.sessionStorage.getItem("nova-editor-draft:item-1:song_text"),
      ).not.toBeNull();
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    expect(
      await screen.findByText("This video changed in another tab — reload to continue."),
    ).toBeInTheDocument();
    expect(mockRouterPush).not.toHaveBeenCalled();
    expect(
      window.sessionStorage.getItem("nova-editor-draft:item-1:song_text"),
    ).not.toBeNull();
  });
});
