/**
 * Pocket editor — flag OFF pinning (the IRON RULE regression guard).
 *
 * NEXT_PUBLIC_MOBILE_EDITOR_ENABLED is UNSET (jest.setup.ts never sets it; the
 * explicit delete below also hardens against same-worker process.env leakage
 * from the flag-on suite — Jest isolates module registries per test file, but
 * process.env is shared per worker process). EditorShell reads the flag into
 * the module const POCKET_UI at load time, so it is deleted BEFORE the
 * explicit require() of EditorShell below (deletion-test pattern).
 *
 * Pinned legacy contract in light mode (wideViewport=false):
 *  1. No pocket chrome ever renders: "pocket-dock", "pocket-ministrip",
 *     "pocket-context-strip", "pocket-sheet" are all absent — even though the
 *     mocked clip timeline carries a real slot (which WOULD produce a
 *     ministrip segment if the flag were on, so the absence assertions have
 *     teeth).
 *  2. The legacy light surface renders: the floating "Add text" empty-state
 *     CTA (no text bars) and the light transport's "Scrub video" range input.
 *  3. Tap-opens-sheet: double-clicking a canvas text element opens the legacy
 *     LightEditSheet (dialog with heading "Edit text" and the
 *     "Full timeline editing on desktop" copy), which is closable again.
 *     (Single-tap selection routes through EditorCanvas hit-testing, which
 *     needs real bounding rects + PointerEvent clientX — jsdom 20 has
 *     neither, so the deterministic double-click path, which calls
 *     onSelectText + onFocusContent directly, is the simulable selection
 *     path. Both paths funnel into setLightSheetOpen(true) in legacy mode.)
 */

delete process.env.NEXT_PUBLIC_MOBILE_EDITOR_ENABLED;

import "@testing-library/jest-dom";
import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
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
// This suite is light-mode only — the pocket editor's home turf.
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

// One ready 5s clip slot. With the pocket flag ON this would surface a
// "pocket-ministrip" segment — keeping it here makes the flag-off absence
// assertion meaningful rather than vacuous.
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
// const POCKET_UI is evaluated with the flag deterministically unset.
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

/** Settle jsdom's timer-backed requestAnimationFrame (focus rAFs etc.). */
async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 30));
  });
}

afterEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
});

function expectNoPocketChrome() {
  expect(screen.queryByTestId("pocket-dock")).toBeNull();
  expect(screen.queryByTestId("pocket-ministrip")).toBeNull();
  expect(screen.queryByTestId("pocket-context-strip")).toBeNull();
  expect(screen.queryByTestId("pocket-sheet")).toBeNull();
}

describe("EditorShell — pocket editor flag OFF (legacy light mode pinned)", () => {
  it("renders the legacy light surface with zero pocket chrome", async () => {
    await renderShell(makeVariant());

    // 1. No pocket chrome anywhere (dock, ministrip, context strip, sheet) —
    //    despite the clip-timeline mock carrying a real slot.
    expectNoPocketChrome();

    // 2. Legacy light surface: empty-state Add-text CTA + light transport.
    expect(screen.getByRole("button", { name: "Add text" })).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Scrub video" })).toBeInTheDocument();

    // 3. The LightEditSheet is closed initially — its unique copy is absent.
    expect(screen.queryByText("Full timeline editing on desktop")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("double-tapping a canvas text element opens the legacy LightEditSheet (tap-opens-sheet)", async () => {
    await renderShell(makeTextVariant());

    const textEl = document.querySelector('[data-text-id="title-1"]');
    expect(textEl).not.toBeNull();

    fireEvent.doubleClick(textEl as Element);
    await settle();

    // The legacy full-screen edit sheet — dialog + "Edit text" heading + the
    // legacy-only "Full timeline editing on desktop" strapline.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Edit text" })).toBeInTheDocument();
    expect(screen.getByText("Full timeline editing on desktop")).toBeInTheDocument();
    // The selected bar's text is loaded into the Content field.
    expect(screen.getByLabelText("Content")).toHaveValue("Title 1");

    // Selection still surfaces NO pocket chrome (no context strip / dock).
    expectNoPocketChrome();

    // The sheet closes again via its close affordance.
    fireEvent.click(screen.getByRole("button", { name: "Close text editor" }));
    expect(screen.queryByText("Full timeline editing on desktop")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
