process.env.NEXT_PUBLIC_MOBILE_EDITOR_ENABLED = "true";
// The Nova dock slot is gated at RENDER time on NEXT_PUBLIC_EDIT_COPILOT_ENABLED;
// keep it deterministically OFF here so the nova-absent assertion is stable.
delete process.env.NEXT_PUBLIC_EDIT_COPILOT_ENABLED;

/**
 * Pocket editor — flag ON contract (NEXT_PUBLIC_MOBILE_EDITOR_ENABLED="true",
 * set on line 1 BEFORE anything transitively loads EditorShell; EditorShell
 * reads the flag into the module const POCKET_UI at load time, and is
 * require()d after the mocks below — the deletion-test pattern).
 *
 * Pinned in light mode (wideViewport=false):
 *  1. "pocket-dock" renders with the tool buttons (text / captions / sounds
 *     at minimum, plus visuals / overlays / styles); nova is absent while the
 *     copilot flag is off.
 *  2. "pocket-ministrip" renders when the clip timeline carries slots.
 *  3. Tapping pocket-dock-text opens a "pocket-sheet" titled "Text"; while a
 *     sheet is open the bottom cluster (dock + ministrip) hides; Escape
 *     closes the sheet and the cluster returns.
 *     NOTE — the spec's "clicking pocket-dock-text again toggles the sheet
 *     closed" is UNREACHABLE through the UI: the dock unmounts whenever a
 *     sheet is open (EditorShell renders it under `!pocketSheetOpen`), so a
 *     second dock tap cannot happen. The TOGGLE_TOOL close branch exists in
 *     pocketReducer but is dead via the dock; the reachable close affordances
 *     (Escape, the sheet's Close button) are pinned instead.
 *  4. Legacy displacement: double-tapping a canvas text element opens the
 *     POCKET inspector sheet ("Edit text"), NOT the legacy LightEditSheet —
 *     the legacy-only "Full timeline editing on desktop" copy never appears.
 *  5. Closing the inspector keeps the selection and surfaces the
 *     "pocket-context-strip" (Edit / Style / Timing / Delete pills); the Edit
 *     pill re-opens the inspector sheet.
 */

import "@testing-library/jest-dom";
import React from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { EditorCapabilities, PlanItem, PlanItemVariant } from "@/lib/plan-api";

// jsdom lacks ResizeObserver (EditorCanvas / EditorTimelineBody measure loops).
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

// Layout switch: min-width media queries match ⇒ "full", none match ⇒ "light".
// The pocket editor only activates in light mode (pocketActive = POCKET_UI &&
// layoutMode === "light"), so this suite stays narrow-viewport throughout.
const wideViewport = false;
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: jest.fn().mockImplementation((query: string) => ({
    matches: query.includes("min-width") ? wideViewport : false,
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

// One ready 5s clip slot so the pocket MiniStrip has a segment to render
// (miniStripSegments derives from state.slots via sequentialSlotLayout).
jest.mock("@/app/plan/_components/useClipTimeline", () => ({
  useClipTimeline: () => ({
    state: {
      grid: [],
      clipDurations: {},
      baseline: [],
      slots: [
        {
          key: "slot-1",
          slotId: null,
          clipIndex: 0,
          inS: 0,
          durationBeats: null,
          durationS: 5,
          removed: false,
          momentDescription: null,
        },
      ],
      past: [],
      future: [],
      clampNonce: 0,
      clampedKey: null,
    },
    dispatch: jest.fn(),
    clips: [],
    windows: [],
    totalS: 5,
    loadState: "ready",
    reload: jest.fn(),
  }),
}));

// Deletion-test pattern: require AFTER the env line + mocks so the module
// const POCKET_UI is evaluated with the flag deterministically set.
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
  carousel: true,
  suggestions: true,
};

function makeVariant(overrides: Record<string, unknown> = {}): PlanItemVariant {
  return {
    variant_id: "var-1",
    output_url: "https://storage.example/variant.mp4",
    render_status: "ready",
    text_mode: "none",
    style_set_id: null,
    intro_text_size_px: null,
    text_elements: [],
    resolved_archetype: "montage",
    editor_capabilities: EDITABLE_CAPABILITIES,
    ...overrides,
  } as unknown as PlanItemVariant;
}

/** Variant with one on-canvas text bar visible at t=0 (start 0 .. end 4). */
function makeTextVariant(): PlanItemVariant {
  return makeVariant({
    text_mode: "agent_text",
    text_elements: [
      {
        id: "title-1",
        role: "generative_intro",
        text: "Title 1",
        start_s: 0,
        end_s: 4,
        x_frac: 0.5,
        y_frac: 0.5,
      },
    ],
  });
}

async function renderShell(variant: PlanItemVariant) {
  mockGetPlanItem.mockResolvedValue(ITEM);
  mockGetPlanItemJobStatus.mockResolvedValue({
    variants: [variant],
  } as unknown as Awaited<ReturnType<typeof getPlanItemJobStatus>>);
  let view: ReturnType<typeof render> | undefined;
  await act(async () => {
    view = render(<EditorShell itemId="item-1" variantParam="var-1" />);
  });
  return view!;
}

/** Settle jsdom's timer-backed requestAnimationFrame (Sheet phase, focus). */
async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 30));
  });
}

afterEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
});

// Do not leak the flag into later test files scheduled on the same Jest
// worker (module registries are per-file; process.env is per-process).
afterAll(() => {
  delete process.env.NEXT_PUBLIC_MOBILE_EDITOR_ENABLED;
});

describe("EditorShell — pocket editor flag ON (light mode)", () => {
  it("renders the pocket dock with the tool buttons and the ministrip", async () => {
    await renderShell(makeVariant());

    // The dock is present where legacy light mode had none.
    expect(screen.getByTestId("pocket-dock")).toBeInTheDocument();

    // Required tool minimum + the rest of the dock set (per-tool testids).
    expect(screen.getByTestId("pocket-dock-text")).toBeInTheDocument();
    expect(screen.getByTestId("pocket-dock-captions")).toBeInTheDocument();
    expect(screen.getByTestId("pocket-dock-sounds")).toBeInTheDocument();
    expect(screen.getByTestId("pocket-dock-visuals")).toBeInTheDocument();
    expect(screen.getByTestId("pocket-dock-overlays")).toBeInTheDocument();
    expect(screen.getByTestId("pocket-dock-styles")).toBeInTheDocument();
    // Roles too: the dock is a labelled toolbar-style nav of tool buttons.
    expect(screen.getByRole("navigation", { name: "Editor tools" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Text tool" })).toBeInTheDocument();

    // Nova slot is gated on NEXT_PUBLIC_EDIT_COPILOT_ENABLED — off here.
    expect(screen.queryByTestId("pocket-dock-nova")).toBeNull();

    // Clips exist in the harness fixture ⇒ the ministrip renders.
    expect(screen.getByTestId("pocket-ministrip")).toBeInTheDocument();

    // Nothing is open/selected yet: no sheet, no context strip.
    expect(screen.queryByTestId("pocket-sheet")).toBeNull();
    expect(screen.queryByTestId("pocket-context-strip")).toBeNull();
  });

  it("moves Carousel from the Visuals discovery sheet into the shared inspector", async () => {
    await renderShell(
      makeVariant({
        carousel_moment: {
          effect: "scale_sweep",
          mode: "focus",
          focus_clip_index: null,
          position: "middle",
          duration_s: 6,
          transition: "crossfade",
        },
      }),
    );

    fireEvent.click(screen.getByTestId("pocket-dock-visuals"));
    await settle();
    const visualsSheet = screen.getByTestId("pocket-sheet");
    expect(within(visualsSheet).getByRole("heading", { name: "Visuals" })).toBeInTheDocument();

    fireEvent.click(within(visualsSheet).getByRole("button", { name: "Carousel" }));
    await settle();

    const inspectorSheet = screen.getByTestId("pocket-sheet");
    expect(within(inspectorSheet).getByRole("heading", { name: "Edit carousel" })).toBeInTheDocument();
    expect(within(inspectorSheet).getByTestId("carousel-inspector")).toBeInTheDocument();
    expect(
      within(inspectorSheet).getByRole("radiogroup", { name: "Carousel effect" }),
    ).toBeInTheDocument();
    expect(within(inspectorSheet).queryByText("Add a block")).not.toBeInTheDocument();

    fireEvent.click(within(inspectorSheet).getByRole("radio", { name: "Cover flow effect" }));
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();

    fireEvent.click(within(inspectorSheet).getByRole("button", { name: "Remove carousel" }));
    expect(screen.queryByTestId("carousel-inspector")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    fireEvent.keyDown(document, { key: "Escape" });
    await settle();
    fireEvent.click(screen.getByTestId("pocket-dock-visuals"));
    await settle();
    fireEvent.click(
      within(screen.getByTestId("pocket-sheet")).getByRole("button", { name: "Carousel" }),
    );
    await settle();
    expect(screen.getByRole("radio", { name: "Cover flow effect" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    mockCommitEditorSession.mockResolvedValue({
      ok: true,
      generation: "gen-next",
      sections: {},
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });
    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalled());
    expect(mockCommitEditorSession.mock.calls[0][2].carousel_moment).toEqual(
      expect.objectContaining({ effect: "cover_flow" }),
    );
  });

  it("opens a persisted Carousel from its pocket mini-strip region", async () => {
    await renderShell(
      makeVariant({
        carousel_moment: {
          effect: "scale_sweep",
          mode: "focus",
          focus_clip_index: null,
          position: "intro",
          duration_s: 4,
          transition: "crossfade",
        },
      }),
    );

    const carouselMark = within(screen.getByTestId("pocket-ministrip")).getByRole("button", {
      name: /Carousel, 0\.0–4\.0 seconds/,
    });
    fireEvent.click(carouselMark);
    await settle();

    expect(screen.getByRole("heading", { name: "Edit carousel" })).toBeInTheDocument();
    expect(screen.getByTestId("carousel-inspector")).toBeInTheDocument();
  });

  it("dock text tool opens the Text sheet; the bottom cluster hides; Escape closes", async () => {
    await renderShell(makeVariant());

    fireEvent.click(screen.getByTestId("pocket-dock-text"));
    await settle();

    // The sheet is a dialog named by its title, with the title as a heading.
    const sheet = screen.getByTestId("pocket-sheet");
    expect(sheet).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Text" })).toBe(sheet);
    expect(within(sheet).getByRole("heading", { name: "Text" })).toBeInTheDocument();

    // While the sheet is open, the bottom cluster hides (dock + ministrip).
    // This is also why a second pocket-dock-text tap cannot toggle the sheet
    // closed — the dock is unmounted (see file header NOTE).
    expect(screen.queryByTestId("pocket-dock")).toBeNull();
    expect(screen.queryByTestId("pocket-ministrip")).toBeNull();

    // Escape closes the sheet; the cluster returns.
    fireEvent.keyDown(document, { key: "Escape" });
    await settle();
    expect(screen.queryByTestId("pocket-sheet")).toBeNull();
    expect(screen.getByTestId("pocket-dock")).toBeInTheDocument();
    expect(screen.getByTestId("pocket-ministrip")).toBeInTheDocument();
  });

  it("dock sounds tool opens the Sounds sheet; its Close button closes it", async () => {
    await renderShell(makeVariant());

    fireEvent.click(screen.getByTestId("pocket-dock-sounds"));
    await settle();

    const sheet = screen.getByTestId("pocket-sheet");
    expect(screen.getByRole("dialog", { name: "Sounds" })).toBe(sheet);

    // The Sheet's own close affordance is the first "Close"-named button in
    // the sheet (title row renders before the body content).
    fireEvent.click(within(sheet).getAllByRole("button", { name: "Close" })[0]);
    await settle();
    expect(screen.queryByTestId("pocket-sheet")).toBeNull();
    expect(screen.getByTestId("pocket-dock")).toBeInTheDocument();
  });

  it("double-tapping canvas text routes to the pocket inspector sheet, not the LightEditSheet", async () => {
    await renderShell(makeTextVariant());

    const textEl = document.querySelector('[data-text-id="title-1"]');
    expect(textEl).not.toBeNull();

    fireEvent.doubleClick(textEl as Element);
    await settle();

    // Pocket inspector sheet opens, titled for the text selection.
    const sheet = screen.getByTestId("pocket-sheet");
    expect(screen.getByRole("dialog", { name: "Edit text" })).toBe(sheet);
    expect(within(sheet).getByRole("heading", { name: "Edit text" })).toBeInTheDocument();

    // Legacy LightEditSheet is displaced — its unique strapline never renders.
    expect(screen.queryByText("Full timeline editing on desktop")).toBeNull();
  });

  it("closing the inspector keeps the selection and surfaces the context strip; Edit re-opens it", async () => {
    await renderShell(makeTextVariant());

    fireEvent.doubleClick(document.querySelector('[data-text-id="title-1"]') as Element);
    await settle();
    const sheet = screen.getByTestId("pocket-sheet");
    fireEvent.click(within(sheet).getAllByRole("button", { name: "Close" })[0]);
    await settle();
    expect(screen.queryByTestId("pocket-sheet")).toBeNull();

    // Selection persists ⇒ the floating context strip appears over the canvas.
    const strip = screen.getByTestId("pocket-context-strip");
    expect(screen.getByRole("toolbar", { name: "Selection actions" })).toBe(strip);
    expect(within(strip).getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(within(strip).getByRole("button", { name: "Style" })).toBeInTheDocument();
    expect(within(strip).getByRole("button", { name: "Timing" })).toBeInTheDocument();
    expect(within(strip).getByRole("button", { name: "Delete" })).toBeInTheDocument();

    // The primary pill re-opens the inspector sheet for the same selection.
    fireEvent.click(within(strip).getByRole("button", { name: "Edit" }));
    await settle();
    expect(screen.getByRole("dialog", { name: "Edit text" })).toBeInTheDocument();
    // …and the strip hides while a sheet is open (one surface at a time).
    expect(screen.queryByTestId("pocket-context-strip")).toBeNull();
  });
});
