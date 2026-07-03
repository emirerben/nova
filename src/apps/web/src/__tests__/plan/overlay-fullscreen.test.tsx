/**
 * Plan 009 T3 — fullscreen cutaways in the Overlays lane.
 *
 * Covers: the extracted OverlayCardPopover (stack order, mode toggle,
 * pip-control hiding, demote, max-scale affordance), the six-trigger
 * non-blocking warning table, fullscreen chip rendering (h-8 ink + glyph,
 * tiny-chip degradation, provenance layering, aria-labels), the F keyboard
 * shortcut (chip focus, popover open, input guard), the external-edit
 * contract (hero preview click-to-edit), the hatched intro-text band, and
 * the drag hard-stop at fullscreen boundaries (helper + gesture).
 */

// @ts-nocheck
// crypto.randomUUID polyfill lives in jest.setup.ts (global for all tests).

import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import OverlayLane, { fullscreenGapBounds } from "@/app/plan/_components/OverlayLane";
import UnifiedTimeline from "@/app/plan/_components/UnifiedTimeline";
import {
  computeFullscreenWarnings,
  demotePatch,
} from "@/app/plan/_components/OverlayCardPopover";
import type { MediaOverlay } from "@/lib/plan-api";

// ── Fixtures ──────────────────────────────────────────────────────────────────

function makeCard(override: Partial<MediaOverlay> = {}): MediaOverlay {
  return {
    id: "ov-1",
    kind: "image",
    src_gcs_path: "slot-uploads/test/img.jpg",
    position: "center",
    x_frac: 0.5,
    y_frac: 0.5,
    scale: 0.35,
    start_s: 6.2,
    end_s: 9.0,
    z: 0,
    ...override,
  };
}

function laneProps(override = {}) {
  return {
    totalDurationS: 30,
    currentTimeS: 0,
    overlayCards: [],
    overlaysEnabled: true,
    overlayUploading: false,
    localPreviewUrls: {},
    onOverlayUploadRequest: jest.fn(),
    onUpdateCard: jest.fn(),
    onRemoveCard: jest.fn(),
    onClearOverlays: jest.fn(),
    ...override,
  };
}

/** Full UnifiedTimeline props (mirrors unified-timeline.test.tsx). */
function timelineProps(override = {}) {
  return {
    totalDurationS: 30,
    currentTimeS: 5,
    sfxPlacements: [],
    sfxGlossaryEffects: [],
    sfxGlossaryLoading: false,
    sfxRendering: false,
    sfxUploading: false,
    onSfxChange: jest.fn(),
    onSfxUploadRequest: jest.fn().mockResolvedValue(undefined),
    overlayCards: [],
    overlaysEnabled: true,
    overlayUploading: false,
    localPreviewUrls: {},
    onOverlayUploadRequest: jest.fn(),
    onUpdateCard: jest.fn(),
    onRemoveCard: jest.fn(),
    onClearOverlays: jest.fn(),
    textElements: [],
    onTextElementsChange: jest.fn(),
    clipsPanel: null,
    onClipsPanelChange: jest.fn(),
    ...override,
  };
}

function chipFor(cardId: string): HTMLElement {
  const el = document.querySelector(`[data-overlay-chip="${cardId}"]`);
  expect(el).toBeTruthy();
  return el as HTMLElement;
}

/** Open a manual card's popover via its keyboard path (Enter on the chip). */
function openPopover(cardId: string) {
  fireEvent.keyDown(chipFor(cardId), { key: "Enter" });
}

function mockRectWidth(width: number) {
  return jest.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    width,
    height: 10,
    top: 0,
    left: 0,
    bottom: 10,
    right: width,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect);
}

// ── fullscreenGapBounds (drag hard-stop helper) ───────────────────────────────

describe("fullscreenGapBounds — drag hard-stop clamp helper", () => {
  const total = 30;

  it("pip mover is unconstrained by pip blockers (pip+pip overlap stays legal)", () => {
    const bounds = fullscreenGapBounds({
      movingId: "a",
      movingFullscreen: false,
      origStart: 0,
      origEnd: 2,
      cards: [
        { id: "a", start_s: 0, end_s: 2, display_mode: "pip" },
        { id: "b", start_s: 5, end_s: 10, display_mode: "pip" },
      ],
      totalDurationS: total,
    });
    expect(bounds).toEqual({ lower: 0, upper: 30 });
  });

  it("pip mover hard-stops at a fullscreen blocker on its right", () => {
    const bounds = fullscreenGapBounds({
      movingId: "a",
      movingFullscreen: false,
      origStart: 0,
      origEnd: 2,
      cards: [
        { id: "a", start_s: 0, end_s: 2 },
        { id: "b", start_s: 5, end_s: 10, display_mode: "fullscreen" },
      ],
      totalDurationS: total,
    });
    expect(bounds).toEqual({ lower: 0, upper: 5 });
  });

  it("fullscreen mover hard-stops at ANY card on its left (both directions rule)", () => {
    const bounds = fullscreenGapBounds({
      movingId: "b",
      movingFullscreen: true,
      origStart: 5,
      origEnd: 10,
      cards: [
        { id: "a", start_s: 0, end_s: 2, display_mode: "pip" },
        { id: "b", start_s: 5, end_s: 10, display_mode: "fullscreen" },
      ],
      totalDurationS: total,
    });
    expect(bounds).toEqual({ lower: 2, upper: 30 });
  });

  it("fullscreen mover between two cards is bounded on both sides", () => {
    const bounds = fullscreenGapBounds({
      movingId: "b",
      movingFullscreen: true,
      origStart: 5,
      origEnd: 10,
      cards: [
        { id: "a", start_s: 0, end_s: 3, display_mode: "pip" },
        { id: "b", start_s: 5, end_s: 10, display_mode: "fullscreen" },
        { id: "c", start_s: 14, end_s: 20, display_mode: "pip" },
      ],
      totalDurationS: total,
    });
    expect(bounds).toEqual({ lower: 3, upper: 14 });
  });
});

// ── Drag hard-stop through the gesture ────────────────────────────────────────

describe("OverlayLane — drag hard-stops at fullscreen boundaries", () => {
  let rectSpy: jest.SpyInstance;
  beforeEach(() => {
    rectSpy = mockRectWidth(300);
  });
  afterEach(() => rectSpy.mockRestore());

  it("clamps a pip chip dragged INTO a fullscreen window (no snap-back)", () => {
    const onUpdateCard = jest.fn();
    const pip = makeCard({ id: "a", start_s: 0, end_s: 2 });
    const fs = makeCard({ id: "b", start_s: 5, end_s: 10, display_mode: "fullscreen" });
    render(<OverlayLane {...laneProps({ overlayCards: [pip, fs], onUpdateCard })} />);

    fireEvent.mouseDown(chipFor("a"), { clientX: 0 });
    // 300px over a 300px lane on a 30s timeline → +30s, way past the blocker.
    fireEvent.mouseMove(window, { clientX: 300 });
    fireEvent.mouseUp(window);

    // Hard-stop at the fullscreen boundary: end_s pinned to 5.
    expect(onUpdateCard).toHaveBeenLastCalledWith("a", { start_s: 3, end_s: 5 });
  });

  it("clamps a fullscreen chip dragged ONTO other cards", () => {
    const onUpdateCard = jest.fn();
    const pip = makeCard({ id: "a", start_s: 0, end_s: 2 });
    const fs = makeCard({ id: "b", start_s: 5, end_s: 10, display_mode: "fullscreen" });
    render(<OverlayLane {...laneProps({ overlayCards: [pip, fs], onUpdateCard })} />);

    fireEvent.mouseDown(chipFor("b"), { clientX: 300 });
    fireEvent.mouseMove(window, { clientX: 0 });
    fireEvent.mouseUp(window);

    expect(onUpdateCard).toHaveBeenLastCalledWith("b", { start_s: 2, end_s: 7 });
  });
});

// ── Popover: stack order, toggle, demote, affordance ──────────────────────────

describe("OverlayCardPopover — fullscreen stack", () => {
  it("keeps Remove in the header (top), above the mode radiogroup and explainer", () => {
    const card = makeCard({ display_mode: "fullscreen" });
    render(<OverlayLane {...laneProps({ overlayCards: [card] })} />);
    openPopover("ov-1");

    const remove = screen.getByRole("button", { name: "Remove card" });
    const group = screen.getByRole("radiogroup", { name: "Display mode" });
    const explainer = screen.getByText(
      "Fills the whole frame. Your voice keeps playing underneath.",
    );

    // DOM order: Remove → radiogroup → explainer (stack items 1 → 2 → 3).
    expect(remove.compareDocumentPosition(group) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(group.compareDocumentPosition(explainer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // (4) trim + timing fields present below.
    expect(screen.getByLabelText("Start time (seconds)")).toBeInTheDocument();
    expect(screen.getByLabelText("End time (seconds)")).toBeInTheDocument();
  });

  it("segmented toggle writes exactly {display_mode} — fracs never cleared", async () => {
    const onUpdateCard = jest.fn();
    const card = makeCard(); // pip with a real layout
    render(<OverlayLane {...laneProps({ overlayCards: [card], onUpdateCard })} />);
    openPopover("ov-1");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Full screen" }));
    });
    expect(onUpdateCard).toHaveBeenCalledWith("ov-1", { display_mode: "fullscreen" });
  });

  it("fullscreen mode hides Position presets and the Scale slider", () => {
    const card = makeCard({ display_mode: "fullscreen" });
    render(<OverlayLane {...laneProps({ overlayCards: [card] })} />);
    openPopover("ov-1");

    expect(screen.queryByRole("slider")).toBeNull();
    expect(screen.queryByRole("button", { name: "Top" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Center" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Bottom" })).toBeNull();
    // Selected mode is disabled (PlanVariantEditor radiogroup semantics).
    expect(screen.getByRole("button", { name: "Full screen" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "PiP" })).not.toBeDisabled();
  });

  it("quiet demote button converts to pip, keeping the prior pip fracs", async () => {
    const onUpdateCard = jest.fn();
    const card = makeCard({ display_mode: "fullscreen" }); // has x/y/scale
    render(<OverlayLane {...laneProps({ overlayCards: [card], onUpdateCard })} />);
    openPopover("ov-1");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Show as small card instead" }));
    });
    // Fracs untouched — patch is display_mode only, prior layout restores itself.
    expect(onUpdateCard).toHaveBeenCalledWith("ov-1", { display_mode: "pip" });
  });

  it("born-fullscreen cards (no pip layout in fracs) demote to the center preset", () => {
    const card = makeCard({ display_mode: "fullscreen", x_frac: undefined, y_frac: undefined });
    expect(demotePatch(card)).toEqual({
      display_mode: "pip",
      position: "center",
      x_frac: 0.5,
      y_frac: 0.5,
    });
    // And with a prior layout, nothing but the mode changes.
    expect(demotePatch(makeCard({ display_mode: "fullscreen" }))).toEqual({
      display_mode: "pip",
    });
  });

  it("scale slider at max shows 'Full width' + 'Make full screen →' which flips the mode", async () => {
    const onUpdateCard = jest.fn();
    const card = makeCard({ scale: 1.0 });
    const { unmount } = render(
      <OverlayLane {...laneProps({ overlayCards: [card], onUpdateCard })} />,
    );
    openPopover("ov-1");

    expect(screen.getByText("Full width")).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Make full screen →" }));
    });
    expect(onUpdateCard).toHaveBeenCalledWith("ov-1", { display_mode: "fullscreen" });
    unmount();

    // Below max: percentage label, no affordance.
    render(<OverlayLane {...laneProps({ overlayCards: [makeCard({ scale: 0.5 })] })} />);
    openPopover("ov-1");
    expect(screen.getByText("50%")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Make full screen →" })).toBeNull();
  });
});

// ── Warnings — one test per trigger row (6 triggers) ─────────────────────────

describe("OverlayCardPopover — fullscreen warning triggers", () => {
  it("start_s < 2.5 → 'Covers your hook'", () => {
    const card = makeCard({ display_mode: "fullscreen", start_s: 1.0, end_s: 4.0 });
    render(<OverlayLane {...laneProps({ overlayCards: [card] })} />);
    openPopover("ov-1");
    expect(screen.getByText("Covers your hook")).toBeInTheDocument();
  });

  it("intro-text-window overlap → 'Covers your intro text'", () => {
    const card = makeCard({ display_mode: "fullscreen", start_s: 3.0, end_s: 6.0 });
    render(
      <OverlayLane
        {...laneProps({
          overlayCards: [card],
          introTextWindow: { start_s: 0, end_s: 4 },
        })}
      />,
    );
    openPopover("ov-1");
    expect(screen.getByText("Covers your intro text")).toBeInTheDocument();
    // No hook warning — start_s is past 2.5.
    expect(screen.queryByText("Covers your hook")).toBeNull();
  });

  it("trim outrun → 'Clip ends early' warning + hard end_s snap", () => {
    const onUpdateCard = jest.fn();
    const card = makeCard({
      display_mode: "fullscreen",
      kind: "video",
      start_s: 5,
      end_s: 12, // 7s window over 2s of trimmed footage
      clip_trim_start_s: 0,
      clip_trim_end_s: 2,
      clip_duration_s: 2,
    });
    render(<OverlayLane {...laneProps({ overlayCards: [card], onUpdateCard })} />);

    // Hard snap fires without the popover being open (snap, not freeze).
    expect(onUpdateCard).toHaveBeenCalledWith("ov-1", { end_s: 7 });

    openPopover("ov-1");
    expect(
      screen.getByText("Clip ends early — cutaway will be shortened"),
    ).toBeInTheDocument();
  });

  it("asset aspect > 1.2 → 'Sides will be cropped'; suppressed without metadata", () => {
    const card = makeCard({ display_mode: "fullscreen", start_s: 5, end_s: 8 });
    const { unmount } = render(
      <OverlayLane
        {...laneProps({
          overlayCards: [card],
          resolveAssetMeta: () => ({ aspect: 1.8 }),
        })}
      />,
    );
    openPopover("ov-1");
    expect(screen.getByText("Sides will be cropped")).toBeInTheDocument();
    unmount();

    // No resolver wired (or still analyzing) → suppressed, never faked.
    render(<OverlayLane {...laneProps({ overlayCards: [card] })} />);
    openPopover("ov-1");
    expect(screen.queryByText("Sides will be cropped")).toBeNull();
  });

  it("min(width,height) < 720 → low-res warning, ONLY when pixel dims are present", () => {
    const card = makeCard({ display_mode: "fullscreen", start_s: 5, end_s: 8 });
    const { unmount } = render(
      <OverlayLane
        {...laneProps({
          overlayCards: [card],
          resolveAssetMeta: () => ({ width: 600, height: 900 }),
        })}
      />,
    );
    openPopover("ov-1");
    expect(
      screen.getByText("Low resolution — this may look blurry full screen"),
    ).toBeInTheDocument();
    unmount();

    // Aspect known but dims not yet backfilled → no low-res warning.
    render(
      <OverlayLane
        {...laneProps({
          overlayCards: [card],
          resolveAssetMeta: () => ({ aspect: 0.6 }),
        })}
      />,
    );
    openPopover("ov-1");
    expect(
      screen.queryByText("Low resolution — this may look blurry full screen"),
    ).toBeNull();
  });

  it("manual fullscreen total > 15s → 'Lots of full-screen time' warning", () => {
    const a = makeCard({ id: "a", display_mode: "fullscreen", start_s: 3, end_s: 12 });
    const b = makeCard({ id: "b", display_mode: "fullscreen", start_s: 13, end_s: 20.5 });
    render(<OverlayLane {...laneProps({ overlayCards: [a, b] })} />);
    openPopover("a");
    expect(
      screen.getByText("Lots of full-screen time — this render may take longer"),
    ).toBeInTheDocument();
  });

  it("warnings stack vertically, worst first", () => {
    const a = makeCard({ id: "a", display_mode: "fullscreen", start_s: 1, end_s: 12 });
    const b = makeCard({ id: "b", display_mode: "fullscreen", start_s: 13, end_s: 19 });
    render(<OverlayLane {...laneProps({ overlayCards: [a, b] })} />);
    openPopover("a");

    const stack = screen.getByTestId("fullscreen-warnings");
    const keys = Array.from(stack.querySelectorAll("[data-warning]")).map((el) =>
      el.getAttribute("data-warning"),
    );
    expect(keys).toEqual(["hook", "total"]);
  });

  it("pip cards raise no fullscreen warnings", () => {
    expect(
      computeFullscreenWarnings({
        card: makeCard({ start_s: 0.5 }), // would trip hook if fullscreen
        introTextWindow: { start_s: 0, end_s: 4 },
        assetMeta: { aspect: 2.0, width: 100, height: 100 },
        manualFullscreenTotalS: 99,
      }),
    ).toEqual([]);
  });
});

// ── Chips: rendering, tiny degradation, provenance, aria ─────────────────────

describe("OverlayLane — fullscreen chips", () => {
  it("fullscreen chip gets the taller h-8 row, solid ink fill and '⛶ Full' glyph", () => {
    const card = makeCard({ display_mode: "fullscreen" });
    render(<OverlayLane {...laneProps({ overlayCards: [card] })} />);

    const chip = chipFor("ov-1");
    expect(chip.parentElement!.className).toContain("h-8");
    expect(chip.style.backgroundColor).toBe("rgb(12, 12, 14)");
    expect(screen.getByText("⛶ Full")).toBeInTheDocument();
    // Pip rows stay h-6.
    render(<OverlayLane {...laneProps({ overlayCards: [makeCard({ id: "ov-2" })] })} />);
    expect(chipFor("ov-2").parentElement!.className).toContain("h-6");
  });

  it("tiny fullscreen chips (<24px) hide the glyph and suppress edge handles", () => {
    const rectSpy = mockRectWidth(300);
    const tiny = makeCard({ display_mode: "fullscreen", start_s: 10, end_s: 10.2 });
    const { unmount } = render(
      <OverlayLane {...laneProps({ totalDurationS: 100, overlayCards: [tiny] })} />,
    );
    // 1% floor of a 300px lane = 3px < 24px → degraded.
    expect(screen.queryByText("⛶ Full")).toBeNull();
    expect(document.querySelector('[data-chip-handle="left-ov-1"]')).toBeNull();
    expect(document.querySelector('[data-chip-handle="right-ov-1"]')).toBeNull();
    unmount();

    // Wide fullscreen chip keeps glyph + handles.
    const wide = makeCard({ display_mode: "fullscreen", start_s: 10, end_s: 40 });
    const r2 = render(
      <OverlayLane {...laneProps({ totalDurationS: 100, overlayCards: [wide] })} />,
    );
    expect(screen.getByText("⛶ Full")).toBeInTheDocument();
    expect(document.querySelector('[data-chip-handle="left-ov-1"]')).not.toBeNull();
    r2.unmount();

    // Tiny PIP chips keep their handles — degradation is fullscreen-only.
    render(
      <OverlayLane
        {...laneProps({
          totalDurationS: 100,
          overlayCards: [makeCard({ start_s: 10, end_s: 10.2 })],
        })}
      />,
    );
    expect(document.querySelector('[data-chip-handle="left-ov-1"]')).not.toBeNull();
    rectSpy.mockRestore();
  });

  it("dashed lime + ✦ provenance layers over a fullscreen suggestion unchanged", () => {
    const suggestion = {
      id: "sug-1",
      overlay: makeCard({ id: "ov-s", display_mode: "fullscreen" }),
      sfx: null,
      staged: false,
    };
    render(<OverlayLane {...laneProps({ suggestions: [suggestion] })} />);

    const chip = chipFor("ov-s");
    expect(chip.className).toMatch(/border-dashed/);
    expect(chip.className).toMatch(/border-lime-600/);
    // Ink fill identifies the mode; lime stays exclusively provenance.
    expect(chip.style.backgroundColor).toBe("rgb(12, 12, 14)");
    expect(screen.getByTestId("suggestion-badge-sug-1")).toBeInTheDocument();
  });

  it("chips are focusable with mode-aware aria-labels", () => {
    const fs = makeCard({ id: "a", display_mode: "fullscreen", start_s: 6.2, end_s: 9.0 });
    const pip = makeCard({ id: "b", start_s: 2, end_s: 8 });
    render(<OverlayLane {...laneProps({ overlayCards: [fs, pip] })} />);

    const fsChip = screen.getByRole("button", {
      name: "Full-screen cutaway, 6.2 to 9.0 seconds",
    });
    const pipChip = screen.getByRole("button", { name: "Visual card, 2.0 to 8.0 seconds" });
    expect(fsChip).toHaveAttribute("tabindex", "0");
    expect(pipChip).toHaveAttribute("tabindex", "0");
  });
});

// ── Keyboard: F toggles display_mode ──────────────────────────────────────────

describe("OverlayLane — F shortcut", () => {
  it("F on a focused pip chip promotes to fullscreen; on a fullscreen chip demotes", () => {
    const onUpdateCard = jest.fn();
    const { unmount } = render(
      <OverlayLane {...laneProps({ overlayCards: [makeCard()], onUpdateCard })} />,
    );
    fireEvent.keyDown(chipFor("ov-1"), { key: "f" });
    expect(onUpdateCard).toHaveBeenCalledWith("ov-1", { display_mode: "fullscreen" });
    unmount();

    const onUpdateCard2 = jest.fn();
    render(
      <OverlayLane
        {...laneProps({
          overlayCards: [makeCard({ display_mode: "fullscreen" })],
          onUpdateCard: onUpdateCard2,
        })}
      />,
    );
    fireEvent.keyDown(chipFor("ov-1"), { key: "F" });
    expect(onUpdateCard2).toHaveBeenCalledWith("ov-1", { display_mode: "pip" });
  });

  it("F while the popover is open toggles the open card's mode", () => {
    const onUpdateCard = jest.fn();
    render(<OverlayLane {...laneProps({ overlayCards: [makeCard()], onUpdateCard })} />);
    openPopover("ov-1");

    fireEvent.keyDown(document.body, { key: "F" });
    expect(onUpdateCard).toHaveBeenCalledWith("ov-1", { display_mode: "fullscreen" });
  });

  it("F is ignored while typing in an input", () => {
    const onUpdateCard = jest.fn();
    render(<OverlayLane {...laneProps({ overlayCards: [makeCard()], onUpdateCard })} />);
    openPopover("ov-1");

    fireEvent.keyDown(screen.getByLabelText("Start time (seconds)"), { key: "f" });
    expect(onUpdateCard).not.toHaveBeenCalled();
  });

  it("F promote stays manual-chip only (suggestion F never patches the envelope)", () => {
    const onSuggestionEdit = jest.fn();
    const suggestion = { id: "sug-1", overlay: makeCard({ id: "ov-s" }), sfx: null, staged: false };
    render(
      <OverlayLane {...laneProps({ suggestions: [suggestion], onSuggestionEdit })} />,
    );
    fireEvent.keyDown(chipFor("ov-s"), { key: "f" });
    expect(onSuggestionEdit).not.toHaveBeenCalled();
  });

  it("Enter opens a SUGGESTION chip's popover (R4/C10 — keyboard-operable)", () => {
    // Before the fix the suggestion chip's onKeyDown returned early, so a
    // keyboard user could focus it but Enter/Space did nothing (WCAG 2.1.1).
    const onSuggestionEdit = jest.fn();
    const suggestion = { id: "sug-1", overlay: makeCard({ id: "ov-s" }), sfx: null, staged: false };
    render(
      <OverlayLane {...laneProps({ suggestions: [suggestion], onSuggestionEdit })} />,
    );
    // Popover closed initially.
    expect(screen.queryByRole("radiogroup", { name: "Display mode" })).toBeNull();
    fireEvent.keyDown(chipFor("ov-s"), { key: "Enter" });
    // Popover now open — the same edit surface manual chips get.
    expect(screen.getByRole("radiogroup", { name: "Display mode" })).toBeInTheDocument();
  });

  it("Space opens a suggestion chip's popover too", () => {
    const onSuggestionEdit = jest.fn();
    const suggestion = { id: "sug-1", overlay: makeCard({ id: "ov-s" }), sfx: null, staged: false };
    render(
      <OverlayLane {...laneProps({ suggestions: [suggestion], onSuggestionEdit })} />,
    );
    fireEvent.keyDown(chipFor("ov-s"), { key: " " });
    expect(screen.getByRole("radiogroup", { name: "Display mode" })).toBeInTheDocument();
  });
});

// ── R2/C8: fullscreenPromoteEnabled flag (version-skew guard) ────────────────

describe("OverlayLane — fullscreenPromoteEnabled flag (R2/C8)", () => {
  it("flag off hides the 'Full screen' promote option + 'Make full screen →' on a pip card", () => {
    const card = makeCard({ scale: 1.0 }); // at max-scale → affordance would show
    render(
      <OverlayLane
        {...laneProps({ overlayCards: [card], fullscreenPromoteEnabled: false })}
      />,
    );
    openPopover("ov-1");

    // The radiogroup + PiP option are still present (pip editing works).
    expect(screen.getByRole("radiogroup", { name: "Display mode" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "PiP" })).toBeInTheDocument();
    // Both NEW promote affordances are gone.
    expect(screen.queryByRole("button", { name: "Full screen" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Make full screen →" })).toBeNull();
  });

  it("flag off makes the F-promote shortcut a no-op on a pip chip", () => {
    const onUpdateCard = jest.fn();
    render(
      <OverlayLane
        {...laneProps({
          overlayCards: [makeCard()],
          onUpdateCard,
          fullscreenPromoteEnabled: false,
        })}
      />,
    );
    fireEvent.keyDown(chipFor("ov-1"), { key: "f" });
    expect(onUpdateCard).not.toHaveBeenCalled();
  });

  it("flag off still lets an EXISTING fullscreen card show its mode + demote", () => {
    const onUpdateCard = jest.fn();
    const card = makeCard({ display_mode: "fullscreen" });
    render(
      <OverlayLane
        {...laneProps({
          overlayCards: [card],
          onUpdateCard,
          fullscreenPromoteEnabled: false,
        })}
      />,
    );
    // The chip renders as a fullscreen cutaway regardless of the flag.
    expect(
      screen.getByRole("button", { name: "Full-screen cutaway, 6.2 to 9.0 seconds" }),
    ).toBeInTheDocument();

    openPopover("ov-1");
    // The Full screen option shows (selected/disabled) so the card can demote.
    expect(screen.getByRole("button", { name: "Full screen" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Show as small card instead" }));
    expect(onUpdateCard).toHaveBeenCalledWith("ov-1", { display_mode: "pip" });
  });

  it("flag ON (default) keeps the promote affordances", () => {
    const card = makeCard({ scale: 1.0 });
    render(
      <OverlayLane
        {...laneProps({ overlayCards: [card], fullscreenPromoteEnabled: true })}
      />,
    );
    openPopover("ov-1");
    expect(screen.getByRole("button", { name: "Full screen" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Make full screen →" })).toBeInTheDocument();
  });
});

// ── External-edit contract + intro band (through UnifiedTimeline) ────────────

describe("UnifiedTimeline — 009 T3 pass-through contracts", () => {
  it("externalEditCardId opens the card's popover and acks via onExternalEditHandled", () => {
    const onExternalEditHandled = jest.fn();
    render(
      <UnifiedTimeline
        {...timelineProps({
          overlayCards: [makeCard()],
          externalEditCardId: "ov-1",
          onExternalEditHandled,
        })}
      />,
    );
    // Popover is open (mode radiogroup visible) and the handoff was acked.
    expect(screen.getByRole("radiogroup", { name: "Display mode" })).toBeInTheDocument();
    expect(onExternalEditHandled).toHaveBeenCalledTimes(1);
  });

  it("renders the hatched intro-text band from introTextWindow; absent without it", () => {
    const { unmount } = render(
      <UnifiedTimeline
        {...timelineProps({
          overlayCards: [makeCard()],
          introTextWindow: { start_s: 0, end_s: 3 },
        })}
      />,
    );
    // jsdom drops gradient functions from parsed inline styles, so assert the
    // band's window-derived geometry (0–3s of 30s → left 0%, width 10%).
    const band = screen.getByTestId("intro-text-band");
    expect(band.style.left).toBe("0%");
    expect(band.style.width).toBe("10%");
    unmount();

    render(<UnifiedTimeline {...timelineProps({ overlayCards: [makeCard()] })} />);
    expect(screen.queryByTestId("intro-text-band")).toBeNull();
  });
});
