/**
 * Lyrics-optional "elements" model — the Lyrics toggle instantly
 * inserts/removes beat-synced lyric bars as ordinary editable text_elements,
 * with no render round-trip (product decision: on new-model variants lyrics
 * are NOT baked into the render).
 *
 * Dual-gated by NEXT_PUBLIC_LYRICS_OPTIONAL_ENABLED (set below, module-scope,
 * before importing EditorShell — see EditorShell-orientation.test.tsx for the
 * same pattern) AND the variant's editor_capabilities.lyrics.lyrics_model.
 * A "baked" (legacy) variant must keep the OLD boolean-visibility toggle even
 * with the flag on — see the "legacy variant is unaffected" block below.
 * Flag-off behaviour is covered by every other lyrics test file, which never
 * sets this env var (jest.setup.ts leaves it unset ⇒ false by default).
 */
process.env.NEXT_PUBLIC_LYRICS_OPTIONAL_ENABLED = "true";
// Also on, for the "legacy variant unaffected" block below: proves the new
// flag activating the elements model doesn't leak into a baked-model variant
// that's still gated by the OLD legacy-editor flag.
process.env.NEXT_PUBLIC_LYRICS_EDITOR_ENABLED = "true";

import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { EditorCapabilities, LyricSeedsResponse, PlanItem, PlanItemVariant } from "@/lib/plan-api";

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

const EditorShell =
  require("@/app/plan/items/[id]/_editor/EditorShell").default as typeof import("@/app/plan/items/[id]/_editor/EditorShell").default;
const { getPlanItem, getPlanItemJobStatus, LyricSeedsError } = require("@/lib/plan-api") as {
  getPlanItem: typeof import("@/lib/plan-api").getPlanItem;
  getPlanItemJobStatus: typeof import("@/lib/plan-api").getPlanItemJobStatus;
  LyricSeedsError: typeof import("@/lib/plan-api").LyricSeedsError;
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

const BAKED_MODEL_CAPABILITIES: EditorCapabilities = {
  text_elements: false,
  timeline: false,
  split_clips: false,
  mix: false,
  sfx: true,
  overlays: true,
  reason: "lyrics_sync",
  lyrics: {
    editable: true,
    enabled: true,
    can_toggle_on: true,
    reason: null,
    lyrics_model: "baked",
  },
};

function makeElementsVariant(overrides: Partial<PlanItemVariant> = {}): PlanItemVariant {
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
    ...overrides,
  } as unknown as PlanItemVariant;
}

function makeBakedVariant(): PlanItemVariant {
  return {
    variant_id: "song_lyrics",
    output_url: "https://storage.example/variant.mp4",
    render_status: "ready",
    text_mode: "lyrics",
    style_set_id: null,
    intro_text_size_px: null,
    text_elements: [],
    lyrics_enabled: true,
    resolved_archetype: "montage",
    render_generation_id: "gen-current",
    editor_capabilities: BAKED_MODEL_CAPABILITIES,
  } as unknown as PlanItemVariant;
}

const SEEDS: LyricSeedsResponse = {
  elements: [
    {
      id: "lyr-L0",
      role: "lyric_line",
      text: "First line",
      start_s: 2,
      end_s: 4,
      color: "#FFFFFF",
    },
    {
      id: "lyr-L1",
      role: "lyric_line",
      text: "Second line",
      start_s: 4,
      end_s: 6,
      color: "#FFFFFF",
    },
  ],
};

async function renderShell(variant: PlanItemVariant) {
  mockGetPlanItem.mockResolvedValue(ITEM);
  mockGetPlanItemJobStatus.mockResolvedValue({
    variants: [variant],
  } as unknown as Awaited<ReturnType<typeof getPlanItemJobStatus>>);
  mockCommitEditorSession.mockResolvedValue({
    ok: true,
    generation: "gen-next",
    sections: {},
  });
  await act(async () => {
    render(<EditorShell itemId="item-1" variantParam={variant.variant_id} />);
  });
}

function openTextDrawer() {
  fireEvent.click(screen.getByRole("button", { name: "Text tool" }));
}

function lyricsSwitch() {
  return screen.getByRole("switch", { name: "Lyrics" });
}

afterEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
});

describe("EditorShell — Lyrics toggle ON (elements model)", () => {
  it("fetches seeds once and inserts them as one undoable action", async () => {
    mockGetLyricSeeds.mockResolvedValue(SEEDS);
    await renderShell(makeElementsVariant());
    openTextDrawer();

    expect(lyricsSwitch()).toHaveAttribute("aria-checked", "false");

    await act(async () => {
      fireEvent.click(lyricsSwitch());
    });

    expect(mockGetLyricSeeds).toHaveBeenCalledWith("item-1", "montage-1");
    expect(lyricsSwitch()).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();

    // Toggling again re-uses the cache — no second fetch.
    await act(async () => {
      fireEvent.click(lyricsSwitch());
      fireEvent.click(lyricsSwitch());
    });
    expect(mockGetLyricSeeds).toHaveBeenCalledTimes(1);
  });

  it("is undoable — Undo removes every inserted lyric bar and reverts the toggle", async () => {
    mockGetLyricSeeds.mockResolvedValue(SEEDS);
    await renderShell(makeElementsVariant());
    openTextDrawer();

    await act(async () => {
      fireEvent.click(lyricsSwitch());
    });
    expect(lyricsSwitch()).toHaveAttribute("aria-checked", "true");

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));

    expect(lyricsSwitch()).toHaveAttribute("aria-checked", "false");
  });

  it("shows a loading state while the fetch is in flight and disables the toggle", async () => {
    let resolveSeeds: (v: LyricSeedsResponse) => void = () => {};
    mockGetLyricSeeds.mockReturnValue(
      new Promise<LyricSeedsResponse>((resolve) => {
        resolveSeeds = resolve;
      }),
    );
    await renderShell(makeElementsVariant());
    openTextDrawer();

    fireEvent.click(lyricsSwitch());
    await waitFor(() => expect(lyricsSwitch()).toBeDisabled());

    await act(async () => {
      resolveSeeds(SEEDS);
    });
    await waitFor(() => expect(lyricsSwitch()).toHaveAttribute("aria-checked", "true"));
    expect(lyricsSwitch()).toBeEnabled();
  });

  it("422 (no renderable lyrics) disables the toggle with the reason as its tooltip", async () => {
    mockGetLyricSeeds.mockRejectedValue(new LyricSeedsError("no_lyrics"));
    await renderShell(makeElementsVariant());
    openTextDrawer();

    await act(async () => {
      fireEvent.click(lyricsSwitch());
    });

    expect(lyricsSwitch()).toBeDisabled();
    expect(lyricsSwitch().closest("[title]")).toHaveAttribute(
      "title",
      "This song doesn't have synced lyrics",
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "This song doesn't have synced lyrics",
    );
  });
});

describe("EditorShell — Lyrics toggle OFF (elements model)", () => {
  it("removes exactly the lyric bars in one undoable action, keeping other text", async () => {
    mockGetLyricSeeds.mockResolvedValue(SEEDS);
    await renderShell(makeElementsVariant());
    openTextDrawer();

    await act(async () => {
      fireEvent.click(lyricsSwitch());
    });
    expect(lyricsSwitch()).toHaveAttribute("aria-checked", "true");

    await act(async () => {
      fireEvent.click(lyricsSwitch());
    });
    expect(lyricsSwitch()).toHaveAttribute("aria-checked", "false");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalled());
    const body = mockCommitEditorSession.mock.calls[0][2];
    const ids = (body.text_elements as Array<{ id: string }>).map((el) => el.id);
    expect(ids).toEqual(["title-1"]);
    expect(ids).not.toContain("lyr-L0");
    expect(ids).not.toContain("lyr-L1");
  });
});

describe("EditorShell — commit payload on an elements-model variant", () => {
  it("ships lyric elements inside text_elements and omits the legacy lyrics section", async () => {
    mockGetLyricSeeds.mockResolvedValue(SEEDS);
    await renderShell(makeElementsVariant());
    openTextDrawer();

    await act(async () => {
      fireEvent.click(lyricsSwitch());
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalled());
    const body = mockCommitEditorSession.mock.calls[0][2];
    const elements = body.text_elements as Array<{ id: string; role: string }>;
    expect(elements.map((el) => el.id).sort()).toEqual(["lyr-L0", "lyr-L1", "title-1"]);
    expect(elements.find((el) => el.id === "lyr-L0")).toMatchObject({ role: "lyric_line" });
    expect(body.lyrics).toBeUndefined();
  });
});

describe("EditorShell — legacy (baked model) variant is unaffected even with the FE flag on", () => {
  it("the toggle stays the old boolean-visibility switch — no lyric-seeds fetch, no bar insertion", async () => {
    await renderShell(makeBakedVariant());
    openTextDrawer();

    // Baked model starts "on" (persistedLyricsEnabled reads variant.lyrics_enabled).
    expect(lyricsSwitch()).toHaveAttribute("aria-checked", "true");

    await act(async () => {
      fireEvent.click(lyricsSwitch());
    });

    expect(lyricsSwitch()).toHaveAttribute("aria-checked", "false");
    expect(mockGetLyricSeeds).not.toHaveBeenCalled();
  });
});
