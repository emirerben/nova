/**
 * EditorShell behaviour on a text-locked (plan 010 OV-1) variant — mounted
 * through the REAL shell (review fix round on plan 010):
 *
 *  1. Add-text path: with `text_elements=false` on an otherwise-editable
 *     shell, a preset pick (the one add-text path the disabled rail can't
 *     block) adds NO bar and surfaces the honest toast — and the toast
 *     container is a polite live region (DESIGN.md §7 D17).
 *  2. Captions-tab notice: the deep-link pointer the read-only banner used to
 *     carry renders as a quiet notice line when textElementsLocked, in both
 *     the full and the light layout; absent when text is editable and when
 *     the whole shell is read-only (the banner owns the link there).
 *  3. Light-layout empty-state "Add text" CTA does not render when
 *     textElementsLocked.
 */

import "@testing-library/jest-dom";
import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";

// jsdom lacks ResizeObserver (EditorCanvas / EditorTimelineBody measure loops).
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

// Layout switch: min-width media queries match ⇒ "full", none match ⇒ "light".
let wideViewport = true;
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

// The clip lane's data hook fetches over the network — stub a ready empty draft.
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
import { CAPTIONS_TAB_REASON } from "@/app/plan/items/[id]/_editor/editor-capabilities";
import {
  getPlanItem,
  getPlanItemJobStatus,
  type EditorCapabilities,
  type PlanItem,
  type PlanItemVariant,
} from "@/lib/plan-api";

const mockGetPlanItem = getPlanItem as jest.MockedFunction<typeof getPlanItem>;
const mockGetPlanItemJobStatus = getPlanItemJobStatus as jest.MockedFunction<
  typeof getPlanItemJobStatus
>;

const ITEM = {
  id: "item-1",
  theme: "My video",
  current_job_id: "job-1",
} as unknown as PlanItem;

/** Subtitled variant after the plan-010 gate lift: effects live, text locked. */
const TEXT_LOCKED_CAPABILITIES: EditorCapabilities = {
  text_elements: false,
  timeline: false,
  split_clips: false,
  mix: false,
  sfx: true,
  overlays: true,
  suggestions: false,
  reason: "caption_archetype",
};

function makeVariant(capabilities: EditorCapabilities): PlanItemVariant {
  return {
    variant_id: "var-sub",
    output_url: "https://storage.example/variant.mp4",
    render_status: "ready",
    text_mode: "none",
    style_set_id: null,
    intro_text_size_px: null,
    text_elements: [],
    resolved_archetype: "subtitled",
    editor_capabilities: capabilities,
  } as unknown as PlanItemVariant;
}

function makeMasonryVariant(textCount = 2): PlanItemVariant {
  return {
    ...makeVariant(EDITABLE_CAPABILITIES),
    resolved_archetype: "montage",
    montage_preset_rendered: "masonry",
    text_mode: "agent_text",
    text_elements: Array.from({ length: textCount }, (_unused, index) => ({
      id: `title-${index + 1}`,
      role: "generative_intro",
      text: `Title ${index + 1}`,
      start_s: 0,
      end_s: 4,
      x_frac: 0.5,
      y_frac: 0.5 + index * 0.05,
    })),
  } as unknown as PlanItemVariant;
}

const EDITABLE_CAPABILITIES: EditorCapabilities = {
  text_elements: true,
  timeline: true,
  split_clips: true,
  mix: true,
  sfx: true,
  overlays: true,
  suggestions: true,
};

const READ_ONLY_CAPABILITIES: EditorCapabilities = {
  text_elements: false,
  timeline: false,
  split_clips: false,
  mix: false,
  sfx: false,
  overlays: false,
  suggestions: false,
  reason: "caption_archetype",
};

async function renderShell(variant: PlanItemVariant) {
  mockGetPlanItem.mockResolvedValue(ITEM);
  mockGetPlanItemJobStatus.mockResolvedValue({
    variants: [variant],
  } as unknown as Awaited<ReturnType<typeof getPlanItemJobStatus>>);
  await act(async () => {
    render(<EditorShell itemId="item-1" variantParam="var-sub" />);
  });
}

afterEach(() => {
  jest.clearAllMocks();
  wideViewport = true;
  window.sessionStorage.clear();
});

describe("EditorShell — add-text path on a text-locked shell (OV-1)", () => {
  it("a preset pick adds no bar and surfaces the honest toast in a polite live region", async () => {
    await renderShell(makeVariant(TEXT_LOCKED_CAPABILITIES));

    // Sanity: the shell is NOT read-only (effects are live), text tool locked.
    expect(screen.queryByText(/This version can('|’)t be edited\./)).toBeNull();
    expect(screen.getByRole("button", { name: "Text tool" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );

    // The preset browser stays reachable — pick a preset with no selection,
    // which routes into addTextAtPlayhead.
    fireEvent.click(screen.getByRole("button", { name: "Presets inspector tab" }));
    fireEvent.click(screen.getAllByRole("radio", { name: /^Text preset:/ })[0]);

    // No bar was added anywhere (canvas, timeline, inspector).
    expect(screen.queryByText("Add a title")).toBeNull();

    // The honest, text-specific toast — in a polite live region (D17). The
    // same copy also lives in the rail's sr-only reason elements, so pick the
    // status-role container specifically.
    const toast = screen
      .getAllByText(CAPTIONS_TAB_REASON)
      .find((el) => el.getAttribute("role") === "status");
    expect(toast).toBeDefined();
    expect(toast).toHaveAttribute("aria-live", "polite");
  });

  it("control: the same preset pick DOES add a bar on an editable shell", async () => {
    await renderShell(makeVariant(EDITABLE_CAPABILITIES));

    fireEvent.click(screen.getByRole("button", { name: "Presets inspector tab" }));
    fireEvent.click(screen.getAllByRole("radio", { name: /^Text preset:/ })[0]);

    expect(screen.getAllByText("Add a title").length).toBeGreaterThan(0);
    expect(screen.queryByText(CAPTIONS_TAB_REASON)).toBeNull();
  });
});

describe("EditorShell — Captions-tab notice (discoverability, plan 010 review)", () => {
  it("renders the quiet notice with the Captions-tab deep link when textElementsLocked (full layout)", async () => {
    await renderShell(makeVariant(TEXT_LOCKED_CAPABILITIES));

    const notice = screen.getByTestId("captions-tab-notice");
    expect(notice).toHaveTextContent(CAPTIONS_TAB_REASON);
    const link = screen.getByRole("link", { name: "Open the item page Captions tab" });
    expect(link).toHaveAttribute("href", "/plan/items/item-1");
    expect(notice.contains(link)).toBe(true);
  });

  it("renders the notice in the light layout too", async () => {
    wideViewport = false;
    await renderShell(makeVariant(TEXT_LOCKED_CAPABILITIES));

    expect(screen.getByTestId("captions-tab-notice")).toHaveTextContent(
      CAPTIONS_TAB_REASON,
    );
    expect(
      screen.getByRole("link", { name: "Open the item page Captions tab" }),
    ).toBeInTheDocument();
  });

  it("does not render the notice when text is editable", async () => {
    await renderShell(makeVariant(EDITABLE_CAPABILITIES));
    expect(screen.queryByTestId("captions-tab-notice")).toBeNull();
    expect(
      screen.queryByRole("link", { name: "Open the item page Captions tab" }),
    ).toBeNull();
  });

  it("does not double up when the whole shell is read-only (the banner owns the link)", async () => {
    await renderShell(makeVariant(READ_ONLY_CAPABILITIES));
    expect(screen.queryByTestId("captions-tab-notice")).toBeNull();
    // The read-only banner still carries the same deep link.
    expect(
      screen.getByRole("link", { name: "Open the item page Captions tab" }),
    ).toBeInTheDocument();
  });
});

describe("EditorShell — light-layout empty-state Add-text CTA", () => {
  it("does not render the CTA when textElementsLocked", async () => {
    wideViewport = false;
    await renderShell(makeVariant(TEXT_LOCKED_CAPABILITIES));
    expect(screen.queryByRole("button", { name: "Add text" })).toBeNull();
  });

  it("control: renders the CTA when text is editable and no bars exist", async () => {
    wideViewport = false;
    await renderShell(makeVariant(EDITABLE_CAPABILITIES));
    expect(screen.getByRole("button", { name: "Add text" })).toBeInTheDocument();
  });
});

describe("EditorShell — masonry smart placement history", () => {
  it("anchors selected Smart place to the current playhead", async () => {
    const variant = makeMasonryVariant();
    variant.text_elements = variant.text_elements?.map((element) => ({ ...element, end_s: 8 }));
    await renderShell(variant);

    const video = document.querySelector("video") as HTMLVideoElement;
    Object.defineProperty(video, "duration", { configurable: true, value: 8 });
    fireEvent.loadedMetadata(video);
    Object.defineProperty(video, "currentTime", { configurable: true, writable: true, value: 6 });
    fireEvent.timeUpdate(video);
    fireEvent.click(screen.getByRole("button", { name: /^Text row 1, Title 1,/ }));
    fireEvent.click(screen.getByRole("button", { name: "Smart place" }));

    const placedText = document.querySelector('[data-text-id="title-1"]') as HTMLElement;
    expect(Number.parseFloat(placedText.style.left)).toBeGreaterThan(60);
  });

  it("Smart place all becomes clean after Cmd+Z while Redo remains available", async () => {
    await renderShell(makeMasonryVariant());

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Text tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Smart place all" }));
    expect(save).toBeEnabled();
    expect(window.sessionStorage.getItem("nova-editor-draft:var-sub")).not.toBeNull();

    fireEvent.keyDown(document, { key: "z", metaKey: true });
    expect(save).toBeDisabled();
    expect(screen.getByRole("button", { name: "Redo" })).toBeEnabled();
    expect(window.sessionStorage.getItem("nova-editor-draft:var-sub")).toBeNull();
  });

  it("leaves the document unchanged when overlapping bars exceed pocket capacity", async () => {
    await renderShell(makeMasonryVariant(4));

    const save = screen.getByRole("button", { name: "Save" });
    fireEvent.click(screen.getByRole("button", { name: "Text tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Smart place all" }));

    expect(save).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Not enough empty collage pockets for all overlapping text blocks.",
    );
  });
});

// Regression guard (caption-edit discoverability): once SUBTITLED_TEXT_LANE_ENABLED
// ships, the backend sets text_elements=true for subtitled variants while their
// captions still live in the Captions tab. A signpost gated on text_elements===false
// would silently vanish for the exact archetype that needs it — so it must key off
// the archetype (+ base video) via isCaptionArchetype instead.
describe("EditorShell — Captions signpost keys off archetype, not text_elements", () => {
  const CAPTION_TEXT_LANE_ON: EditorCapabilities = {
    text_elements: true, // styled-text lane enabled (flag rolled forward)
    timeline: true,
    split_clips: true,
    mix: true,
    sfx: true,
    overlays: true,
    suggestions: true,
    reason: "caption_archetype",
  };

  function makeCaptionVariant(capabilities: EditorCapabilities): PlanItemVariant {
    return {
      variant_id: "var-sub",
      output_url: "https://storage.example/variant.mp4",
      base_video_url: "https://storage.example/variant_base.mp4",
      render_status: "ready",
      text_mode: "none",
      style_set_id: null,
      intro_text_size_px: null,
      text_elements: [],
      resolved_archetype: "subtitled",
      editor_capabilities: capabilities,
    } as unknown as PlanItemVariant;
  }

  it("shows the Captions-tab notice for a subtitled variant even when text_elements is TRUE", async () => {
    await renderShell(makeCaptionVariant(CAPTION_TEXT_LANE_ON));
    const notice = screen.getByTestId("captions-tab-notice");
    expect(notice).toHaveTextContent(CAPTIONS_TAB_REASON);
    expect(
      screen.getByRole("link", { name: "Open the item page Captions tab" }),
    ).toHaveAttribute("href", "/plan/items/item-1");
  });

  it("still shows the notice when text_elements is false, given a base video", async () => {
    await renderShell(
      makeCaptionVariant({ ...CAPTION_TEXT_LANE_ON, text_elements: false }),
    );
    expect(screen.getByTestId("captions-tab-notice")).toHaveTextContent(CAPTIONS_TAB_REASON);
  });
});
