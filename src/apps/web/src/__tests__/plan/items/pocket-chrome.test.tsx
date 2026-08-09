/**
 * Pocket-editor chrome tests (mobile editor Lane A).
 *
 *  1. pocketReducer decision table — every action, one-sheet-at-a-time,
 *     toggle-closes, SET_DETENT no-op when closed, no-ops return the SAME
 *     state reference.
 *  2. ToolDock — 7 tools with Nova / 6 without, active underline +
 *     aria-pressed, focusable-disabled tools route to onDisabledTap with a
 *     full-opacity readable label.
 *  3. ContextStrip — pill sets + primary pill per selection type, null
 *     renders nothing, Delete is always the word "Delete", disabled Split
 *     routes to onDisabledTap.
 *  4. MiniStrip — miniStripTimeAtX clamping, sub-8px tap selects once +
 *     seeks, 8px+ drag scrubs (onScrubStart once, monotonic onScrub) without
 *     selecting.
 */

import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";

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
  miniStripTimeAtX,
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
      "Nova",
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
    expect(active.querySelector('[class*="bg-[#0c0c0e]"]')).not.toBeNull();

    const inactive = screen.getByTestId("pocket-dock-sounds");
    expect(inactive).toHaveAttribute("aria-pressed", "false");
    expect(inactive.querySelector('[class*="bg-[#0c0c0e]"]')).toBeNull();
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
    expect(label.className).toContain("text-[#71717a]");
    expect(label.className).not.toContain("opacity-50");
    // The icon (not the label) carries the dimming.
    expect(button.querySelector('[class*="opacity-50"]')).not.toBeNull();
  });

  it("toggles enabled tools", () => {
    const { onToggleTool } = renderDock();
    fireEvent.click(screen.getByTestId("pocket-dock-styles"));
    expect(onToggleTool).toHaveBeenCalledWith("styles");
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
    expect(pills[0].className).toContain("bg-[#0c0c0e]");
    fireEvent.click(pills[0]);
    expect(onEdit).toHaveBeenCalledTimes(1);
  });

  it("caption selection: Edit cue (primary) / All captions", () => {
    const onAllCaptions = jest.fn();
    renderStrip({ type: "caption", onEditCue: jest.fn(), onAllCaptions });
    const pills = screen.getAllByRole("button");
    expect(pills.map((p) => p.textContent)).toEqual(["Edit cue", "All captions"]);
    expect(pills[0].className).toContain("bg-[#0c0c0e]");
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
    expect(pills[0].className).toContain("bg-[#0c0c0e]");
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
    expect(pills[0].className).toContain("bg-[#0c0c0e]");
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
    expect(pills[0].className).toContain("bg-[#0c0c0e]");
    fireEvent.click(screen.getByRole("button", { name: "Mute" }));
    expect(onToggleMute).toHaveBeenCalledTimes(1);
    unmount();

    renderStrip({ ...clip, muted: true });
    expect(screen.getByRole("button", { name: "Unmute" })).toBeInTheDocument();
  });

  it("Delete is always the literal word Delete in ink-muted text on white", () => {
    renderStrip({
      type: "text",
      onEdit: jest.fn(),
      onStyle: jest.fn(),
      onTiming: jest.fn(),
      onDelete: jest.fn(),
    });
    const del = screen.getByRole("button", { name: "Delete" });
    expect(del.textContent).toBe("Delete");
    expect(del.className).toContain("text-[#3f3f46]");
    expect(del.className).toContain("bg-white");
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
    if (strip) {
      // jsdom has no layout — pin the gesture math to a 200px-wide strip.
      strip.getBoundingClientRect = () =>
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
    }
    return { ...utils, strip, onScrubStart, onScrub, onSelectClip };
  }

  it("renders nothing when duration or segments are empty", () => {
    const empty = renderStrip({ segments: [] });
    expect(empty.strip).toBeNull();
    empty.unmount();

    const zeroDuration = renderStrip({ durationS: 0 });
    expect(zeroDuration.strip).toBeNull();
  });

  it("sub-8px tap selects the clip under the finger once AND seeks there", () => {
    const { strip, onScrub, onScrubStart, onSelectClip } = renderStrip();
    fireEvent.pointerDown(strip!, { pointerId: 1, clientX: 50, clientY: 10 });
    fireEvent.pointerMove(strip!, { pointerId: 1, clientX: 52, clientY: 10 });
    fireEvent.pointerUp(strip!, { pointerId: 1, clientX: 52, clientY: 10 });

    // 52px of 200px over 10s = 2.6s → clip-a (0–4s).
    expect(onSelectClip).toHaveBeenCalledTimes(1);
    expect(onSelectClip).toHaveBeenCalledWith("clip-a", expect.any(Number));
    expect(onSelectClip.mock.calls[0][1]).toBeCloseTo(2.6, 5);
    expect(onScrub).toHaveBeenCalledTimes(1);
    expect(onScrub.mock.calls[0][0]).toBeCloseTo(2.6, 5);
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

  it("drag beyond 8px scrubs (onScrubStart once, monotonic times) without selecting", () => {
    const { strip, onScrub, onScrubStart, onSelectClip } = renderStrip();
    fireEvent.pointerDown(strip!, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(strip!, { pointerId: 1, clientX: 30, clientY: 10 });
    fireEvent.pointerMove(strip!, { pointerId: 1, clientX: 60, clientY: 10 });
    fireEvent.pointerMove(strip!, { pointerId: 1, clientX: 120, clientY: 10 });
    fireEvent.pointerUp(strip!, { pointerId: 1, clientX: 120, clientY: 10 });

    expect(onScrubStart).toHaveBeenCalledTimes(1);
    const times = onScrub.mock.calls.map(([t]) => t as number);
    expect(times).toHaveLength(3); // one per move after crossing the slop
    expect(times[0]).toBeCloseTo(1.5, 5);
    expect(times[1]).toBeCloseTo(3, 5);
    expect(times[2]).toBeCloseTo(6, 5);
    for (let i = 1; i < times.length; i += 1) {
      expect(times[i]).toBeGreaterThan(times[i - 1]);
    }
    expect(onSelectClip).not.toHaveBeenCalled();
  });

  it("movement under the 8px slop before release stays a tap", () => {
    const { strip, onScrubStart, onSelectClip } = renderStrip();
    fireEvent.pointerDown(strip!, { pointerId: 1, clientX: 150, clientY: 10 });
    fireEvent.pointerMove(strip!, { pointerId: 1, clientX: 154, clientY: 12 });
    fireEvent.pointerUp(strip!, { pointerId: 1, clientX: 154, clientY: 12 });
    expect(onScrubStart).not.toHaveBeenCalled();
    // 154/200 * 10 = 7.7s → clip-c.
    expect(onSelectClip).toHaveBeenCalledWith("clip-c", expect.any(Number));
  });

  it("marks the selected segment and renders presence dots + playhead", () => {
    const { strip } = renderStrip({ selectedClipId: "clip-b" });
    const selected = screen.getByRole("button", { name: "Clip 2, 4.0–7.0 seconds" });
    expect(selected.className).toContain("outline-lime-600");
    expect(selected.querySelector('[class*="bg-lime-600"]')).not.toBeNull(); // dot
    const unmarked = screen.getByRole("button", { name: "Clip 1, 0.0–4.0 seconds" });
    expect(unmarked.querySelector('[class*="bg-lime-600"]')).toBeNull();
    expect(
      strip!.querySelector('[data-testid="pocket-ministrip-playhead"]'),
    ).not.toBeNull();
  });
});
