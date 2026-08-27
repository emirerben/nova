/**
 * Pocket-editor chrome tests (mobile editor Lane A).
 *
 *  1. pocketReducer decision table — every action, one-sheet-at-a-time,
 *     toggle-closes, SET_DETENT no-op when closed, no-ops return the SAME
 *     state reference.
 *  2. ToolDock — 7 tools with Nova / 6 without, active underline +
 *     aria-pressed, focusable-disabled tools route to onDisabledTap with a
 *     full-opacity readable label.
 *  3. ContextStrip — shadcn action sets + primary action per selection type, null
 *     renders nothing, Delete is always the word "Delete", disabled Split
 *     routes to onDisabledTap.
 *  4. MiniStrip — fixed-playhead padding, thumbnail clips, sub-8px tap,
 *     scroll-to-scrub, zoom controls, and both 44px source trim handles.
 */

import "@testing-library/jest-dom";
import React from "react";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

import {
  initialPocketState,
  pocketReducer,
  type PocketState,
} from "@/app/plan/items/[id]/_editor/mobile-editor-state";
import { ToolDock } from "@/app/plan/items/[id]/_editor/ToolDock";
import {
  ContextStrip,
  type StripSelection,
} from "@/app/plan/items/[id]/_editor/ContextStrip";
import {
  MiniStrip,
  POCKET_TIMELINE_BASE_PX_PER_SECOND,
  miniStripTimeAtX,
  pocketTimelineTimeAtTap,
  type MiniStripSegment,
} from "@/app/plan/items/[id]/_editor/MiniStrip";
import { installPointerEventPolyfill } from "@/__tests__/utils/viewport-mocks";

let restorePointerEvents: () => void;

beforeAll(() => {
  restorePointerEvents = installPointerEventPolyfill();
});

afterAll(() => {
  restorePointerEvents();
});

// ── pocketReducer ─────────────────────────────────────────────────────────────

describe("pocketReducer", () => {
  it("starts closed at half detent", () => {
    expect(initialPocketState).toEqual({ sheet: null, detent: "half" });
  });

  it("OPEN_TOOL opens the tool sheet at half", () => {
    const next = pocketReducer(initialPocketState, {
      type: "OPEN_TOOL",
      tool: "text",
    });
    expect(next).toEqual({ sheet: { kind: "tool", tool: "text" }, detent: "half" });
  });

  it("OPEN_TOOL replaces another sheet and resets detent (one sheet at a time)", () => {
    const state: PocketState = {
      sheet: { kind: "inspector" },
      detent: "full",
    };
    const next = pocketReducer(state, { type: "OPEN_TOOL", tool: "sounds" });
    expect(next).toEqual({
      sheet: { kind: "tool", tool: "sounds" },
      detent: "half",
    });
  });

  it("OPEN_INSPECTOR replaces a tool sheet and resets detent", () => {
    const state: PocketState = {
      sheet: { kind: "tool", tool: "text" },
      detent: "full",
    };
    const next = pocketReducer(state, { type: "OPEN_INSPECTOR" });
    expect(next).toEqual({ sheet: { kind: "inspector" }, detent: "half" });
  });

  it("TOGGLE_TOOL opens when closed", () => {
    const next = pocketReducer(initialPocketState, {
      type: "TOGGLE_TOOL",
      tool: "captions",
    });
    expect(next).toEqual({
      sheet: { kind: "tool", tool: "captions" },
      detent: "half",
    });
  });

  it("TOGGLE_TOOL closes when that tool's sheet is already open (any detent)", () => {
    const state: PocketState = {
      sheet: { kind: "tool", tool: "captions" },
      detent: "full",
    };
    const next = pocketReducer(state, { type: "TOGGLE_TOOL", tool: "captions" });
    expect(next).toEqual({ sheet: null, detent: "half" });
  });

  it("TOGGLE_TOOL switches tools and resets detent", () => {
    const state: PocketState = {
      sheet: { kind: "tool", tool: "captions" },
      detent: "full",
    };
    const next = pocketReducer(state, { type: "TOGGLE_TOOL", tool: "styles" });
    expect(next).toEqual({
      sheet: { kind: "tool", tool: "styles" },
      detent: "half",
    });
  });

  it("TOGGLE_TOOL over the inspector opens the tool (inspector is not that tool)", () => {
    const state: PocketState = { sheet: { kind: "inspector" }, detent: "half" };
    const next = pocketReducer(state, { type: "TOGGLE_TOOL", tool: "overlays" });
    expect(next).toEqual({
      sheet: { kind: "tool", tool: "overlays" },
      detent: "half",
    });
  });

  it("CLOSE_SHEET closes and resets detent", () => {
    const state: PocketState = {
      sheet: { kind: "tool", tool: "visuals" },
      detent: "full",
    };
    expect(pocketReducer(state, { type: "CLOSE_SHEET" })).toEqual({
      sheet: null,
      detent: "half",
    });
  });

  it("SET_DETENT changes detent while a sheet is open, preserving the sheet", () => {
    const state: PocketState = {
      sheet: { kind: "tool", tool: "text" },
      detent: "half",
    };
    const next = pocketReducer(state, { type: "SET_DETENT", detent: "full" });
    expect(next.detent).toBe("full");
    expect(next.sheet).toBe(state.sheet);
  });

  it("no-ops return the SAME state reference", () => {
    // SET_DETENT with no sheet open.
    expect(
      pocketReducer(initialPocketState, { type: "SET_DETENT", detent: "full" }),
    ).toBe(initialPocketState);
    // SET_DETENT to the current detent.
    const open: PocketState = {
      sheet: { kind: "tool", tool: "text" },
      detent: "half",
    };
    expect(pocketReducer(open, { type: "SET_DETENT", detent: "half" })).toBe(open);
    // CLOSE_SHEET when already closed.
    expect(pocketReducer(initialPocketState, { type: "CLOSE_SHEET" })).toBe(
      initialPocketState,
    );
    // OPEN_* that would not change anything.
    expect(pocketReducer(open, { type: "OPEN_TOOL", tool: "text" })).toBe(open);
    const inspector: PocketState = { sheet: { kind: "inspector" }, detent: "half" };
    expect(pocketReducer(inspector, { type: "OPEN_INSPECTOR" })).toBe(inspector);
  });
});

// ── ToolDock ──────────────────────────────────────────────────────────────────

describe("ToolDock", () => {
  function renderDock(
    overrides: Partial<React.ComponentProps<typeof ToolDock>> = {},
  ) {
    const onToggleTool = jest.fn();
    const onDisabledTap = jest.fn();
    const utils = render(
      <ToolDock
        activeTool={null}
        disabledTools={{}}
        novaEnabled
        onToggleTool={onToggleTool}
        onDisabledTap={onDisabledTap}
        {...overrides}
      />,
    );
    return { ...utils, onToggleTool, onDisabledTap };
  }

  it("renders 7 tools in desktop order when novaEnabled", () => {
    renderDock();
    const nav = screen.getByRole("navigation", { name: "Editor tools" });
    const buttons = within(nav).getAllByRole("button");
    expect(buttons).toHaveLength(7);
    expect(buttons.map((b) => b.textContent)).toEqual([
      "Kria",
      "Text",
      "Captions",
      "Visuals",
      "Sounds",
      "Overlays",
      "Styles",
    ]);
    expect(screen.getByTestId("pocket-dock")).toBe(nav);
  });

  it("renders 6 tools without Nova when novaEnabled is false", () => {
    renderDock({ novaEnabled: false });
    expect(screen.getAllByRole("button")).toHaveLength(6);
    expect(screen.queryByTestId("pocket-dock-nova")).not.toBeInTheDocument();
  });

  it("marks the active tool with aria-pressed and an ink underline", () => {
    renderDock({ activeTool: "text" });
    const active = screen.getByTestId("pocket-dock-text");
    expect(active).toHaveAttribute("aria-pressed", "true");
    expect(active.querySelector('[class*="bg-foreground"]')).not.toBeNull();

    const inactive = screen.getByTestId("pocket-dock-sounds");
    expect(inactive).toHaveAttribute("aria-pressed", "false");
    expect(inactive.querySelector('[class*="bg-foreground"]')).toBeNull();
  });

  it("routes disabled-tool taps to onDisabledTap with the reason, never onToggleTool", () => {
    const reason = "Captions are still transcribing";
    const { onToggleTool, onDisabledTap } = renderDock({
      disabledTools: { captions: reason },
    });
    const button = screen.getByTestId("pocket-dock-captions");
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).not.toBeDisabled(); // focusable-disabled: tap must still fire
    fireEvent.click(button);
    expect(onDisabledTap).toHaveBeenCalledTimes(1);
    expect(onDisabledTap).toHaveBeenCalledWith(reason);
    expect(onToggleTool).not.toHaveBeenCalled();
  });

  it("keeps the disabled label readable at full opacity", () => {
    renderDock({ disabledTools: { captions: "Unavailable" } });
    const button = screen.getByTestId("pocket-dock-captions");
    const label = within(button).getByText("Captions");
    expect(label.className).toContain("text-muted-foreground");
    expect(label.className).not.toContain("opacity-50");
    // The icon (not the label) carries the dimming.
    expect(button.querySelector('[class*="opacity-50"]')).not.toBeNull();
  });

  it("toggles enabled tools", () => {
    const { onToggleTool } = renderDock();
    fireEvent.click(screen.getByTestId("pocket-dock-styles"));
    expect(onToggleTool).toHaveBeenCalledWith("styles");
  });

  it("keeps all six tools reachable at 375px via a horizontally scrollable dock", () => {
    // At 375–430px the label text overflows a flex-1 row (min-width: auto
    // wins over the 1/7th share) and the trailing tools clip off-screen. The
    // dock must instead scroll horizontally so every tool stays reachable by
    // its accessible name, not just visible in the initial viewport.
    renderDock({ novaEnabled: false });
    const nav = screen.getByRole("navigation", { name: "Editor tools" });
    expect(nav.className).toContain("overflow-x-auto");
    expect(nav.className).toContain("scrollbar-none");

    for (const label of ["Text", "Captions", "Visuals", "Sounds", "Overlays", "Styles"]) {
      expect(screen.getByRole("button", { name: `${label} tool` })).toBeInTheDocument();
    }
  });
});

// ── ContextStrip ──────────────────────────────────────────────────────────────

describe("ContextStrip", () => {
  function renderStrip(selection: StripSelection | null) {
    const onDisabledTap = jest.fn();
    const utils = render(
      <ContextStrip selection={selection} onDisabledTap={onDisabledTap} />,
    );
    return { ...utils, onDisabledTap };
  }

  it("renders nothing for a null selection", () => {
    const { container } = renderStrip(null);
    expect(container.firstChild).toBeNull();
  });

  it("text selection: Edit (primary) / Style / Timing / Delete", () => {
    const onEdit = jest.fn();
    renderStrip({
      type: "text",
      onEdit,
      onStyle: jest.fn(),
      onTiming: jest.fn(),
      onDelete: jest.fn(),
    });
    const toolbar = screen.getByRole("toolbar", { name: "Selection actions" });
    const pills = within(toolbar).getAllByRole("button");
    expect(pills.map((p) => p.textContent)).toEqual([
      "Edit",
      "Style",
      "Timing",
      "Delete",
    ]);
    expect(pills[0].className).toContain("bg-secondary");
    fireEvent.click(pills[0]);
    expect(onEdit).toHaveBeenCalledTimes(1);
  });

  it("caption selection: Edit cue (primary) / All captions", () => {
    const onAllCaptions = jest.fn();
    renderStrip({ type: "caption", onEditCue: jest.fn(), onAllCaptions });
    const pills = screen.getAllByRole("button");
    expect(pills.map((p) => p.textContent)).toEqual(["Edit cue", "All captions"]);
    expect(pills[0].className).toContain("bg-secondary");
    fireEvent.click(pills[1]);
    expect(onAllCaptions).toHaveBeenCalledTimes(1);
  });

  it("overlay selection: Edit (primary) / Timing / Delete", () => {
    renderStrip({
      type: "overlay",
      onEdit: jest.fn(),
      onTiming: jest.fn(),
      onDelete: jest.fn(),
    });
    const pills = screen.getAllByRole("button");
    expect(pills.map((p) => p.textContent)).toEqual(["Edit", "Timing", "Delete"]);
    expect(pills[0].className).toContain("bg-secondary");
  });

  it("motion selection: Edit (primary) / Timing / Delete", () => {
    renderStrip({
      type: "motion",
      onEdit: jest.fn(),
      onTiming: jest.fn(),
      onDelete: jest.fn(),
    });
    const pills = screen.getAllByRole("button");
    expect(pills.map((p) => p.textContent)).toEqual(["Edit", "Timing", "Delete"]);
    expect(pills[0].className).toContain("bg-secondary");
  });

  it("carousel selection: Edit (primary) / Delete", () => {
    const onEdit = jest.fn();
    const onDelete = jest.fn();
    renderStrip({ type: "carousel", onEdit, onDelete });

    const pills = screen.getAllByRole("button");
    expect(pills.map((pill) => pill.textContent)).toEqual(["Edit", "Delete"]);
    expect(pills[0].className).toContain("bg-secondary");

    fireEvent.click(pills[0]);
    fireEvent.click(pills[1]);
    expect(onEdit).toHaveBeenCalledTimes(1);
    expect(onDelete).toHaveBeenCalledTimes(1);
  });

  it("keeps unavailable Carousel deletion focusable and explains why", () => {
    const onDelete = jest.fn();
    const { onDisabledTap } = renderStrip({
      type: "carousel",
      onEdit: jest.fn(),
      onDelete,
      deleteDisabledReason: "Carousel is unavailable for this video.",
    });

    const deleteButton = screen.getByRole("button", { name: "Delete" });
    expect(deleteButton).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(deleteButton);
    expect(onDelete).not.toHaveBeenCalled();
    expect(onDisabledTap).toHaveBeenCalledWith(
      "Carousel is unavailable for this video.",
    );
  });

  it("clip selection: Adjust (primary) / Split / Mute / Delete, Unmute when muted", () => {
    const onToggleMute = jest.fn();
    const clip = {
      type: "clip" as const,
      onAdjust: jest.fn(),
      onSplit: jest.fn(),
      splitDisabledReason: null,
      muted: false,
      onToggleMute,
      onDelete: jest.fn(),
    };
    const { unmount } = renderStrip(clip);
    const pills = screen.getAllByRole("button");
    expect(pills.map((p) => p.textContent)).toEqual([
      "Adjust",
      "Split",
      "Mute",
      "Delete",
    ]);
    expect(pills[0].className).toContain("bg-secondary");
    fireEvent.click(screen.getByRole("button", { name: "Mute" }));
    expect(onToggleMute).toHaveBeenCalledTimes(1);
    unmount();

    renderStrip({ ...clip, muted: true });
    expect(screen.getByRole("button", { name: "Unmute" })).toBeInTheDocument();
  });

  it("Delete is always explicit and uses the shadcn destructive tone", () => {
    renderStrip({
      type: "text",
      onEdit: jest.fn(),
      onStyle: jest.fn(),
      onTiming: jest.fn(),
      onDelete: jest.fn(),
    });
    const del = screen.getByRole("button", { name: "Delete" });
    expect(del.textContent).toBe("Delete");
    expect(del.className).toContain("text-destructive");
  });

  it("disabled Split stays tappable and routes to onDisabledTap", () => {
    const reason = "Playhead is too close to a cut";
    const onSplit = jest.fn();
    const { onDisabledTap } = renderStrip({
      type: "clip",
      onAdjust: jest.fn(),
      onSplit,
      splitDisabledReason: reason,
      muted: false,
      onToggleMute: jest.fn(),
      onDelete: jest.fn(),
    });
    const split = screen.getByRole("button", { name: "Split" });
    expect(split).toHaveAttribute("aria-disabled", "true");
    expect(split).not.toBeDisabled();
    expect(split.className).toContain("opacity-50");
    fireEvent.click(split);
    expect(onDisabledTap).toHaveBeenCalledTimes(1);
    expect(onDisabledTap).toHaveBeenCalledWith(reason);
    expect(onSplit).not.toHaveBeenCalled();
  });

  it("keeps final-clip deletion focusable and explains the floor", () => {
    const onDelete = jest.fn();
    const { onDisabledTap } = renderStrip({
      type: "clip",
      onAdjust: jest.fn(),
      onSplit: jest.fn(),
      splitDisabledReason: null,
      muted: false,
      onToggleMute: jest.fn(),
      onDelete,
      deleteDisabledReason: "At least one clip must remain",
    });

    const deleteButton = screen.getByRole("button", { name: "Delete" });
    expect(deleteButton).toHaveAttribute("aria-disabled", "true");
    expect(deleteButton).not.toBeDisabled();
    fireEvent.click(deleteButton);
    expect(onDelete).not.toHaveBeenCalled();
    expect(onDisabledTap).toHaveBeenCalledWith("At least one clip must remain");
  });
});

// ── MiniStrip ─────────────────────────────────────────────────────────────────

describe("miniStripTimeAtX", () => {
  const cases: Array<{
    name: string;
    clientX: number;
    rectLeft: number;
    rectWidth: number;
    durationS: number;
    expected: number;
  }> = [
    { name: "left edge", clientX: 0, rectLeft: 0, rectWidth: 200, durationS: 10, expected: 0 },
    { name: "midpoint", clientX: 100, rectLeft: 0, rectWidth: 200, durationS: 10, expected: 5 },
    { name: "right edge", clientX: 200, rectLeft: 0, rectWidth: 200, durationS: 10, expected: 10 },
    { name: "clamps below 0", clientX: -80, rectLeft: 0, rectWidth: 200, durationS: 10, expected: 0 },
    { name: "clamps above duration", clientX: 900, rectLeft: 0, rectWidth: 200, durationS: 10, expected: 10 },
    { name: "honours rectLeft offset", clientX: 130, rectLeft: 30, rectWidth: 200, durationS: 10, expected: 5 },
    { name: "zero-width rect is safe", clientX: 50, rectLeft: 0, rectWidth: 0, durationS: 10, expected: 0 },
  ];

  it.each(cases)("$name", ({ clientX, rectLeft, rectWidth, durationS, expected }) => {
    expect(miniStripTimeAtX(clientX, rectLeft, rectWidth, durationS)).toBeCloseTo(
      expected,
      5,
    );
  });
});

describe("pocketTimelineTimeAtTap", () => {
  it("maps a tap through center padding and clamps to the canonical duration", () => {
    expect(
      pocketTimelineTimeAtTap({
        scrollLeft: 96,
        clientX: 148,
        rectLeft: 0,
        viewportWidth: 200,
        pixelsPerSecond: 48,
        durationS: 10,
      }),
    ).toBeCloseTo(3, 5);
    expect(
      pocketTimelineTimeAtTap({
        scrollLeft: 480,
        clientX: 300,
        rectLeft: 0,
        viewportWidth: 200,
        pixelsPerSecond: 48,
        durationS: 10,
      }),
    ).toBe(10);
  });
});

describe("MiniStrip", () => {
  const segments: MiniStripSegment[] = [
    { id: "clip-a", startS: 0, endS: 4 },
    { id: "clip-b", startS: 4, endS: 7, hasMarks: true },
    { id: "clip-c", startS: 7, endS: 10 },
  ];

  function renderStrip(
    overrides: Partial<React.ComponentProps<typeof MiniStrip>> = {},
  ) {
    const onScrubStart = jest.fn();
    const onScrub = jest.fn();
    const onSelectClip = jest.fn();
    const utils = render(
      <MiniStrip
        segments={segments}
        durationS={10}
        currentTimeS={2}
        onScrubStart={onScrubStart}
        onScrub={onScrub}
        onSelectClip={onSelectClip}
        {...overrides}
      />,
    );
    const strip = screen.queryByTestId("pocket-ministrip");
    const viewport = screen.queryByTestId("pocket-timeline-viewport");
    if (viewport) {
      // jsdom has no layout — pin the gesture math to a 200px viewport.
      viewport.getBoundingClientRect = () =>
        ({
          left: 0,
          top: 0,
          right: 200,
          bottom: 44,
          width: 200,
          height: 44,
          x: 0,
          y: 0,
          toJSON: () => ({}),
        }) as DOMRect;
      viewport.scrollLeft = 2 * POCKET_TIMELINE_BASE_PX_PER_SECOND;
    }
    return { ...utils, strip, viewport, onScrubStart, onScrub, onSelectClip };
  }

  it("renders nothing when duration or segments are empty", () => {
    const empty = renderStrip({ segments: [] });
    expect(empty.strip).toBeNull();
    empty.unmount();

    const zeroDuration = renderStrip({ durationS: 0 });
    expect(zeroDuration.strip).toBeNull();
  });

  it("sub-8px tap selects the clip under the finger once AND seeks there", () => {
    const { viewport, onScrub, onScrubStart, onSelectClip } = renderStrip();
    fireEvent.pointerDown(viewport!, { pointerId: 1, clientX: 146, clientY: 10 });
    fireEvent.pointerMove(viewport!, { pointerId: 1, clientX: 148, clientY: 10 });
    fireEvent.pointerUp(viewport!, { pointerId: 1, clientX: 148, clientY: 10 });

    // scrollLeft 96 + tap 148 - center 100 = 144px / 48 = 3s.
    expect(onSelectClip).toHaveBeenCalledTimes(1);
    expect(onSelectClip).toHaveBeenCalledWith("clip-a", expect.any(Number));
    expect(onSelectClip.mock.calls[0][1]).toBeCloseTo(3, 5);
    expect(onScrub).toHaveBeenCalledTimes(1);
    expect(onScrub.mock.calls[0][0]).toBeCloseTo(3, 5);
    expect(onScrubStart).not.toHaveBeenCalled();

    // The browser's synthetic click after the tap must NOT double-select.
    fireEvent.click(
      screen.getByRole("button", { name: "Clip 1, 0.0–4.0 seconds" }),
    );
    expect(onSelectClip).toHaveBeenCalledTimes(1);
  });

  it("keyboard/AT click on a segment button selects at the clip start", () => {
    const { onSelectClip } = renderStrip();
    fireEvent.click(
      screen.getByRole("button", { name: "Clip 2, 4.0–7.0 seconds" }),
    );
    expect(onSelectClip).toHaveBeenCalledTimes(1);
    expect(onSelectClip).toHaveBeenCalledWith("clip-b", 4);
  });

  it("selects the visually topmost incoming clip inside a transition overlap", () => {
    const { viewport, onSelectClip } = renderStrip({
      durationS: 8,
      segments: [
        { id: "outgoing", startS: 0, endS: 5 },
        { id: "incoming", startS: 4, endS: 8 },
      ],
    });
    viewport!.scrollLeft = 4.5 * POCKET_TIMELINE_BASE_PX_PER_SECOND;
    fireEvent.pointerDown(viewport!, {
      pointerId: 21,
      clientX: 100,
      clientY: 10,
    });
    fireEvent.pointerUp(viewport!, {
      pointerId: 21,
      clientX: 100,
      clientY: 10,
    });
    expect(onSelectClip).toHaveBeenCalledWith("incoming", 4.5);
  });

  it("drag beyond 8px updates the strip directly and coalesces seeks per frame", () => {
    const { viewport, onScrub, onScrubStart, onSelectClip } = renderStrip();
    fireEvent.pointerDown(viewport!, { pointerId: 1, clientX: 140, clientY: 10 });
    fireEvent.pointerMove(viewport!, { pointerId: 1, clientX: 120, clientY: 10 });
    fireEvent.pointerMove(viewport!, { pointerId: 1, clientX: 90, clientY: 10 });
    fireEvent.pointerMove(viewport!, { pointerId: 1, clientX: 40, clientY: 10 });
    fireEvent.pointerUp(viewport!, { pointerId: 1, clientX: 40, clientY: 10 });

    expect(onScrubStart).toHaveBeenCalledTimes(1);
    const times = onScrub.mock.calls.map(([t]) => t as number);
    expect(times).toHaveLength(1);
    expect(times[0]).toBeCloseTo(196 / 48, 5);
    expect(onSelectClip).not.toHaveBeenCalled();
  });

  it("movement under the 8px slop before release stays a tap", () => {
    const { viewport, onScrubStart, onSelectClip } = renderStrip();
    viewport!.scrollLeft = 7 * POCKET_TIMELINE_BASE_PX_PER_SECOND;
    fireEvent.pointerDown(viewport!, { pointerId: 1, clientX: 100, clientY: 10 });
    fireEvent.pointerMove(viewport!, { pointerId: 1, clientX: 104, clientY: 12 });
    fireEvent.pointerUp(viewport!, { pointerId: 1, clientX: 104, clientY: 12 });
    expect(onScrubStart).not.toHaveBeenCalled();
    // 7s at center + 4px / 48 = 7.08s → clip-c.
    expect(onSelectClip).toHaveBeenCalledWith("clip-c", expect.any(Number));
  });

  it("restores scroll-to-scrub after a cancelled pointer gesture", async () => {
    const { viewport, onScrub, onScrubStart } = renderStrip();
    fireEvent.pointerDown(viewport!, {
      pointerId: 13,
      clientX: 140,
      clientY: 10,
    });
    fireEvent.pointerMove(viewport!, {
      pointerId: 13,
      clientX: 100,
      clientY: 10,
    });
    fireEvent.pointerCancel(viewport!, { pointerId: 13 });

    onScrub.mockClear();
    onScrubStart.mockClear();
    viewport!.scrollLeft = 144;
    await waitFor(() => {
      fireEvent.scroll(viewport!);
      expect(onScrubStart).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(onScrub).toHaveBeenCalledWith(3);
    });
  });

  it("marks the selected segment and renders presence dots + playhead", () => {
    const { strip } = renderStrip({ selectedClipId: "clip-b" });
    const selected = screen.getByTestId("pocket-timeline-clip-clip-b");
    expect(selected.querySelector('[class*="border-lime-600"]')).not.toBeNull();
    expect(selected.querySelector('[class*="bg-lime-600"]')).not.toBeNull(); // dot
    const unmarked = screen.getByRole("button", { name: "Clip 1, 0.0–4.0 seconds" });
    expect(unmarked.querySelector('[class*="bg-lime-600"]')).toBeNull();
    expect(
      strip!.querySelector('[data-testid="pocket-ministrip-playhead"]'),
    ).not.toBeNull();
    expect(screen.getByTestId("pocket-ministrip-playhead").className).toContain(
      "left-1/2",
    );
  });

  it("renders leading/trailing center padding and real filmstrip surfaces", () => {
    renderStrip();
    const first = screen.getByTestId("pocket-timeline-clip-clip-a");
    expect(first.style.left).toBe("calc(50vw + 0px)");
    expect(within(first).getByTestId("editor-filmstrip")).toBeInTheDocument();
  });

  it("trims both source edges with 44px handles and records once per drag", () => {
    const onTrimStart = jest.fn();
    const onPreviewTrim = jest.fn();
    renderStrip({
      selectedClipId: "clip-b",
      segments: segments.map((segment) =>
        segment.id === "clip-b"
          ? {
              ...segment,
              sourceStartS: 2,
              sourceDurationS: 10,
              minDurationS: 0.1,
            }
          : segment,
      ),
      onTrimStart,
      onPreviewTrim,
    });

    const left = screen.getByRole("button", { name: /Trim clip start/ });
    expect(left.className).toContain("w-11");
    fireEvent.pointerDown(left, { pointerId: 7, clientX: 100, clientY: 20 });
    fireEvent.pointerMove(left, { pointerId: 7, clientX: 124, clientY: 20 });
    fireEvent.pointerMove(left, { pointerId: 7, clientX: 148, clientY: 20 });
    fireEvent.pointerUp(left, { pointerId: 7, clientX: 148, clientY: 20 });

    expect(onTrimStart).toHaveBeenCalledTimes(1);
    expect(onPreviewTrim).toHaveBeenLastCalledWith("clip-b", {
      inS: 3,
      durationS: 2,
      durationBeats: null,
    });

    onTrimStart.mockClear();
    onPreviewTrim.mockClear();
    const right = screen.getByRole("button", { name: /Trim clip end/ });
    expect(right.className).toContain("w-11");
    fireEvent.pointerDown(right, { pointerId: 8, clientX: 100, clientY: 20 });
    fireEvent.pointerMove(right, { pointerId: 8, clientX: 124, clientY: 20 });
    fireEvent.pointerUp(right, { pointerId: 8, clientX: 124, clientY: 20 });
    expect(onTrimStart).toHaveBeenCalledTimes(1);
    expect(onPreviewTrim).toHaveBeenCalledWith("clip-b", {
      inS: 2,
      durationS: 3.5,
      durationBeats: null,
    });
  });

  it("keeps disabled trim handles focusable and surfaces the reason", () => {
    const onDisabledTap = jest.fn();
    renderStrip({
      selectedClipId: "clip-a",
      segments: [{ ...segments[0], trimDisabledReason: "Locked to voiceover" }],
      onPreviewTrim: jest.fn(),
      onDisabledTap,
    });
    const handle = screen.getByRole("button", { name: /Trim clip start/ });
    expect(handle).toHaveAttribute("aria-disabled", "true");
    expect(handle).not.toBeDisabled();
    fireEvent.pointerDown(handle, { pointerId: 9, clientX: 100, clientY: 20 });
    expect(onDisabledTap).toHaveBeenCalledWith("Locked to voiceover");
  });

  it("offers visible shadcn zoom buttons and changes filmstrip scale", async () => {
    renderStrip();
    const first = screen.getByTestId("pocket-timeline-clip-clip-a");
    expect(first).toHaveStyle({ width: "192px" });
    fireEvent.click(screen.getByRole("button", { name: "Zoom timeline in" }));
    await waitFor(() => expect(first).toHaveStyle({ width: "288px" }));
    expect(screen.getByRole("button", { name: "Zoom timeline out" })).toHaveClass(
      "size-11",
    );
    expect(screen.getByRole("button", { name: "Fit timeline" })).toBeInTheDocument();
  });

  it("Fit shows a complete 60-second timeline at the compact viewport", async () => {
    const { viewport } = renderStrip({
      durationS: 60,
      segments: [{ id: "long-clip", startS: 0, endS: 60 }],
    });
    viewport!.getBoundingClientRect = () =>
      ({
        left: 0,
        top: 0,
        right: 360,
        bottom: 44,
        width: 360,
        height: 44,
        x: 0,
        y: 0,
        toJSON: () => ({}),
      }) as DOMRect;

    fireEvent.click(screen.getByRole("button", { name: "Fit timeline" }));
    await waitFor(() =>
      expect(screen.getByTestId("pocket-timeline-clip-long-clip")).toHaveStyle({
        width: "360px",
      }),
    );
  });

  it("pinch-zooms around the fixed playhead without turning into a slip gesture", async () => {
    const { viewport, onScrubStart, onSelectClip } = renderStrip();
    const first = screen.getByTestId("pocket-timeline-clip-clip-a");
    fireEvent.pointerDown(viewport!, {
      pointerId: 11,
      clientX: 80,
      clientY: 20,
    });
    fireEvent.pointerDown(viewport!, {
      pointerId: 12,
      clientX: 180,
      clientY: 20,
    });
    fireEvent.pointerMove(viewport!, {
      pointerId: 12,
      clientX: 280,
      clientY: 20,
    });
    await waitFor(() => expect(first).toHaveStyle({ width: "384px" }));
    expect(onScrubStart).not.toHaveBeenCalled();
    fireEvent.pointerCancel(viewport!, { pointerId: 11 });
    fireEvent.pointerCancel(viewport!, { pointerId: 12 });
    await waitFor(() => {
      fireEvent.click(
        screen.getByRole("button", { name: "Clip 2, 4.0–7.0 seconds" }),
      );
      expect(onSelectClip).toHaveBeenCalledWith("clip-b", 4);
    });
  });

  it("keeps the fixed-playhead anchor after zooming near the timeline end", async () => {
    const { viewport } = renderStrip({ currentTimeS: 6.5 });
    viewport!.scrollLeft = 6.5 * POCKET_TIMELINE_BASE_PX_PER_SECOND;
    fireEvent.click(screen.getByRole("button", { name: "Zoom timeline in" }));
    await waitFor(() =>
      expect(viewport!.scrollLeft).toBeCloseTo(
        6.5 * POCKET_TIMELINE_BASE_PX_PER_SECOND * 1.5,
        5,
      ),
    );
  });
});
