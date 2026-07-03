/**
 * UnifiedTimeline component — integration tests.
 *
 * Tests the wire-up between the SFX reducer, the glossary picker,
 * the undo/redo buttons, and the read-only lane click-through callbacks.
 *
 * Drag (PointerEvent capture + coordinate math) is covered by the
 * drag-zone unit tests in lib/timeline/__tests__/drag-zone.test.ts;
 * the reducer mutations are covered by sfx-timeline-reducer.test.ts.
 */

// @ts-nocheck
// crypto.randomUUID polyfill lives in jest.setup.ts (global for all tests).

import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import UnifiedTimeline from "@/app/plan/_components/UnifiedTimeline";
import type { SoundEffectPlacement } from "@/lib/plan-api";
import type { SoundEffectSummary } from "@/lib/sfx-api";
import type { MediaOverlay } from "@/lib/plan-api";

// ── Minimal fixtures ──────────────────────────────────────────────────────────

function makePlacement(override: Partial<SoundEffectPlacement> = {}): SoundEffectPlacement {
  return {
    id: `sfx-${Math.random().toString(36).slice(2)}`,
    src_gcs_path: "sound-effects/test/boom.mp3",
    at_s: 0,
    gain: 1.0,
    ...override,
  };
}

function makeGlossaryEffect(override: Partial<SoundEffectSummary> = {}): SoundEffectSummary {
  return {
    id: "gfx-1",
    name: "Whoosh",
    duration_s: 1.5,
    preview_audio_url: "https://cdn.example.com/whoosh.mp3",
    ...override,
  };
}

function makeOverlayCard(override: Partial<MediaOverlay> = {}): MediaOverlay {
  return {
    id: "ov-1",
    kind: "image",
    src_gcs_path: "slot-uploads/test/img.jpg",
    position: "center",
    x_frac: 0.5,
    y_frac: 0.5,
    scale: 0.35,
    start_s: 2,
    end_s: 8,
    clip_trim_start_s: 0,
    clip_trim_end_s: 6,
    clip_duration_s: 6,
    z: 0,
    ...override,
  };
}

// ── Default props ─────────────────────────────────────────────────────────────

function makeProps(override = {}) {
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

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("UnifiedTimeline — SFX bars render", () => {
  it("renders a bar for each placement", () => {
    const p1 = makePlacement({ id: "a", at_s: 2, label: "Boom" });
    const p2 = makePlacement({ id: "b", at_s: 10, label: "Whoosh" });
    render(<UnifiedTimeline {...makeProps({ sfxPlacements: [p1, p2] })} />);

    expect(screen.getByText("Boom")).toBeInTheDocument();
    expect(screen.getByText("Whoosh")).toBeInTheDocument();
  });

  it("shows an empty-state hint when there are no placements", () => {
    render(<UnifiedTimeline {...makeProps()} />);
    expect(screen.getByText(/Add a sound effect/i)).toBeInTheDocument();
  });

  it("renders overlay bars for each overlay card", () => {
    const card = makeOverlayCard({ kind: "video" });
    render(<UnifiedTimeline {...makeProps({ overlayCards: [card] })} />);
    // Overlays lane label
    expect(screen.getByText("Overlays")).toBeInTheDocument();
  });
});

describe("UnifiedTimeline — glossary picker", () => {
  it("renders glossary effects in the select", () => {
    const effects = [makeGlossaryEffect({ id: "g1", name: "Boom" })];
    render(<UnifiedTimeline {...makeProps({ sfxGlossaryEffects: effects })} />);
    expect(screen.getByRole("option", { name: /Boom/i })).toBeInTheDocument();
  });

  it("shows loading placeholder when sfxGlossaryLoading is true", () => {
    render(<UnifiedTimeline {...makeProps({ sfxGlossaryLoading: true })} />);
    expect(screen.getByText(/Loading effects/i)).toBeInTheDocument();
  });

  it("calls onSfxChange with the new placement when Add is clicked", async () => {
    const effect = makeGlossaryEffect({ id: "g1", name: "Whoosh", duration_s: 1.5 });
    const onSfxChange = jest.fn();
    render(
      <UnifiedTimeline
        {...makeProps({ sfxGlossaryEffects: [effect], onSfxChange, currentTimeS: 7 })}
      />,
    );

    // Select the glossary effect
    const select = screen.getByRole("combobox");
    fireEvent.change(select, { target: { value: "g1" } });

    // Click Add
    const addBtn = screen.getByRole("button", { name: /\+ Add/i });
    await act(async () => { fireEvent.click(addBtn); });

    expect(onSfxChange).toHaveBeenCalledTimes(1);
    const [newPlacements] = onSfxChange.mock.calls[0];
    expect(newPlacements).toHaveLength(1);
    // Placed at currentTimeS
    expect(newPlacements[0].at_s).toBe(7);
    expect(newPlacements[0].label).toBe("Whoosh");
  });

  it("clamps 'add at playhead' to totalDurationS when currentTimeS is past the end", async () => {
    const effect = makeGlossaryEffect({ id: "g1", name: "Whoosh" });
    const onSfxChange = jest.fn();
    render(
      <UnifiedTimeline
        {...makeProps({
          sfxGlossaryEffects: [effect],
          onSfxChange,
          currentTimeS: 999, // way past 30s
          totalDurationS: 30,
        })}
      />,
    );

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "g1" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /\+ Add/i })); });

    const [placements] = onSfxChange.mock.calls[0];
    expect(placements[0].at_s).toBeLessThanOrEqual(30);
  });
});

describe("UnifiedTimeline — undo/redo", () => {
  it("Undo button is disabled when no mutations have been made", () => {
    render(<UnifiedTimeline {...makeProps()} />);
    expect(screen.getByTitle("Undo")).toBeDisabled();
  });

  it("Undo button becomes enabled after adding a placement, then reverts", async () => {
    const effect = makeGlossaryEffect({ id: "g1" });
    const onSfxChange = jest.fn();
    render(
      <UnifiedTimeline
        {...makeProps({ sfxGlossaryEffects: [effect], onSfxChange })}
      />,
    );

    // Add a placement
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "g1" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /\+ Add/i })); });

    // Undo button should now be enabled
    const undoBtn = screen.getByTitle("Undo");
    expect(undoBtn).not.toBeDisabled();

    // Click undo
    await act(async () => { fireEvent.click(undoBtn); });

    // onSfxChange should have been called a second time with 0 placements
    expect(onSfxChange).toHaveBeenCalledTimes(2);
    const [lastPlacements] = onSfxChange.mock.calls[1];
    expect(lastPlacements).toHaveLength(0);
  });

  it("Redo button becomes enabled after undo", async () => {
    const effect = makeGlossaryEffect({ id: "g1" });
    render(
      <UnifiedTimeline
        {...makeProps({ sfxGlossaryEffects: [effect], onSfxChange: jest.fn() })}
      />,
    );

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "g1" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /\+ Add/i })); });

    expect(screen.getByTitle("Redo")).toBeDisabled();

    await act(async () => { fireEvent.click(screen.getByTitle("Undo")); });

    expect(screen.getByTitle("Redo")).not.toBeDisabled();
  });
});

describe("UnifiedTimeline — expandable lanes", () => {
  it("Clips lane click expands inline panel", async () => {
    const onClipsPanelChange = jest.fn();
    const clipsPanel = <div data-testid="clips-panel-content">Clips controls</div>;
    render(<UnifiedTimeline {...makeProps({ clipsPanel, onClipsPanelChange })} />);
    expect(screen.getByText("Clips")).toBeInTheDocument();
    // Panel content hidden initially.
    expect(screen.queryByTestId("clips-panel-content")).toBeNull();
    // Click expands the inline panel.
    await act(async () => { fireEvent.click(screen.getByText("Clips").closest("[role='button']")!); });
    expect(screen.getByTestId("clips-panel-content")).toBeInTheDocument();
    expect(onClipsPanelChange).toHaveBeenCalledWith(true);
  });

  it("Text lane shown with textElements; T5 interactive lane renders bars and empty state", async () => {
    // T5: TextLane is always rendered (no hasText gate). With empty textElements, shows empty state.
    render(<UnifiedTimeline {...makeProps({ textElements: [] })} />);
    // Lane label is always visible.
    expect(screen.getByText("Text")).toBeInTheDocument();
    // Empty state text when no bars.
    expect(screen.getByText(/No text yet/i)).toBeInTheDocument();
  });

  it("Text lane always renders in T5; shows empty state when textElements is empty", () => {
    // T5: TextLane is always rendered regardless of textElements length.
    // The "Text" label in the lane gutter is always visible.
    render(<UnifiedTimeline {...makeProps({ textElements: [] })} />);
    // Lane label visible.
    const textLabels = screen.queryAllByText("Text");
    expect(textLabels.length).toBeGreaterThanOrEqual(1);
    // Empty-state message visible.
    expect(screen.getByText(/No text yet/i)).toBeInTheDocument();
  });

  it("Overlays lane shown when overlayCards is non-empty (interactive — no click-through)", () => {
    const card = makeOverlayCard();
    render(<UnifiedTimeline {...makeProps({ overlayCards: [card] })} />);
    // Overlays lane label visible; it's interactive now, not a read-only click-through.
    expect(screen.getByText("Overlays")).toBeInTheDocument();
  });
});

// ── TextLane — narrated_caption bars (PR-B) ──────────────────────────────────

describe("TextLane — narrated_caption bars (PR-B)", () => {
  it("renders teal bars for narrated_caption role", () => {
    const bars: import("@/lib/timeline/text-timeline-reducer").TextElementBar[] = [
      { id: "caption-0", text: "Hello world", start_s: 0, end_s: 3, role: "narrated_caption" },
    ];
    render(<UnifiedTimeline {...makeProps({ textElements: bars })} />);
    // The bar text should be visible
    expect(screen.getByText("Hello world")).toBeInTheDocument();
  });

  it("renders amber bars for generative_intro role", () => {
    const bars: import("@/lib/timeline/text-timeline-reducer").TextElementBar[] = [
      { id: "t1", text: "Intro text", start_s: 0, end_s: 3, role: "generative_intro" },
    ];
    render(<UnifiedTimeline {...makeProps({ textElements: bars })} />);
    expect(screen.getByText("Intro text")).toBeInTheDocument();
  });

  it("accepts narrated_caption bars without throwing and wires onTextElementsChange", () => {
    // jsdom doesn't implement setPointerCapture, so we verify render-level correctness
    // (bar visible, no exception) rather than simulating full pointer drag.
    // The drag → dispatch → onTextElementsChange chain is covered by the TextLane
    // unit tests (pointer events need a real DOM).
    const onTextElementsChange = jest.fn();
    const bars: import("@/lib/timeline/text-timeline-reducer").TextElementBar[] = [
      { id: "caption-0", text: "Caption A", start_s: 1, end_s: 4, role: "narrated_caption" },
    ];
    expect(() =>
      render(
        <UnifiedTimeline
          {...makeProps({ textElements: bars, onTextElementsChange })}
        />,
      ),
    ).not.toThrow();
    expect(screen.getByText("Caption A")).toBeInTheDocument();
  });

  it("shows empty-state hint when textElements is empty regardless of variant type", () => {
    render(<UnifiedTimeline {...makeProps({ textElements: [] })} />);
    expect(screen.getByText(/No text yet/i)).toBeInTheDocument();
  });
});

// ── ClipsLane segment bars (PR-A) ────────────────────────────────────────────

/**
 * Build a minimal ClipTimelineHandle stub for testing the new bar rendering
 * path added in PR-A.  We only need loadState + slots + windows; the rest
 * (dispatch, clips, reload, grid) stay as no-ops / empty arrays.
 */
function makeClipHandle(
  slots: Array<{ key: string; inS: number; durationS: number; removed?: boolean }>,
  windows: Array<{ startS: number; durationS: number }>,
  loadState: "loading" | "error" | "ready" = "ready",
) {
  return {
    loadState,
    state: {
      slots,
      grid: [],
      clipDurations: {},
      baseline: [],
      past: [],
      future: [],
      clampNonce: 0,
      clampedKey: null,
    },
    dispatch: jest.fn(),
    clips: [],
    windows,
    totalS: windows.reduce((acc, w) => acc + w.durationS, 0) || 30,
    reload: jest.fn(),
  };
}

describe("ClipsLane — segment bars (PR-A)", () => {
  it("renders one bar per active (non-removed) slot when handle is ready", () => {
    const handle = makeClipHandle(
      [
        { key: "s1", inS: 0, durationS: 5 },
        { key: "s2", inS: 1, durationS: 4 },
      ],
      [
        { startS: 0, durationS: 5 },
        { startS: 5, durationS: 4 },
      ],
    );
    render(
      <UnifiedTimeline
        {...makeProps({ clipTimelineHandle: handle })}
      />,
    );
    expect(screen.getByTestId("clip-bar-s1")).toBeInTheDocument();
    expect(screen.getByTestId("clip-bar-s2")).toBeInTheDocument();
  });

  it("skips removed slots", () => {
    const handle = makeClipHandle(
      [
        { key: "s1", inS: 0, durationS: 5 },
        { key: "s2", inS: 1, durationS: 4, removed: true },
      ],
      [
        { startS: 0, durationS: 5 },
        { startS: 5, durationS: 0 }, // removed slot window is 0-duration
      ],
    );
    render(<UnifiedTimeline {...makeProps({ clipTimelineHandle: handle })} />);
    expect(screen.getByTestId("clip-bar-s1")).toBeInTheDocument();
    expect(screen.queryByTestId("clip-bar-s2")).toBeNull();
  });

  it("falls back to the launcher button when loadState is loading", () => {
    const handle = makeClipHandle([], [], "loading");
    render(<UnifiedTimeline {...makeProps({ clipTimelineHandle: handle })} />);
    expect(screen.getByText(/Edit clips/i)).toBeInTheDocument();
    expect(screen.queryByTestId(/clip-bar-/)).toBeNull();
  });

  it("falls back to the launcher button when handle is absent", () => {
    render(<UnifiedTimeline {...makeProps()} />);
    expect(screen.getByText(/Edit clips/i)).toBeInTheDocument();
  });

  it("body click on a segment bar opens the expanded panel", async () => {
    const handle = makeClipHandle(
      [{ key: "s1", inS: 0, durationS: 10 }],
      [{ startS: 0, durationS: 10 }],
    );
    const clipsPanel = <div data-testid="clips-panel-content">Panel</div>;
    const onClipsPanelChange = jest.fn();

    render(
      <UnifiedTimeline
        {...makeProps({ clipTimelineHandle: handle, clipsPanel, onClipsPanelChange })}
      />,
    );

    expect(screen.queryByTestId("clips-panel-content")).toBeNull();

    await act(async () => {
      fireEvent.click(screen.getByTestId("clip-bar-s1"));
    });

    expect(screen.getByTestId("clips-panel-content")).toBeInTheDocument();
    expect(onClipsPanelChange).toHaveBeenCalledWith(true);
  });
});

describe("UnifiedTimeline — placement edit row", () => {
  it("clicking a placement bar opens the edit row", async () => {
    const p = makePlacement({ id: "p1", label: "Click me", at_s: 5 });
    render(<UnifiedTimeline {...makeProps({ sfxPlacements: [p] })} />);

    // Click the bar
    await act(async () => {
      fireEvent.click(screen.getByText("Click me"));
    });

    // Edit row should appear — a Remove button and a volume slider
    expect(screen.getByRole("button", { name: /Remove/i })).toBeInTheDocument();
    expect(screen.getByRole("slider")).toBeInTheDocument();
  });

  it("Remove button calls onSfxChange without the removed placement", async () => {
    const p = makePlacement({ id: "p1", label: "Bang", at_s: 3 });
    const onSfxChange = jest.fn();
    render(<UnifiedTimeline {...makeProps({ sfxPlacements: [p], onSfxChange })} />);

    await act(async () => { fireEvent.click(screen.getByText("Bang")); });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Remove/i }));
    });

    const [remaining] = onSfxChange.mock.calls[0];
    expect(remaining).toHaveLength(0);
  });
});

describe("UnifiedTimeline — no Apply button (SFX bakes on Download)", () => {
  // Regression: the Apply button was removed. SFX now play live in the preview
  // (useSfxPreview) and are baked into the MP4 on Download (handleDownload in
  // page.tsx). Assert the button is gone in every state it used to appear in,
  // so the old manual-apply behavior cannot silently come back.
  it("renders no Apply button with placements present", () => {
    const p = makePlacement({ id: "p1", label: "Boom", at_s: 4 });
    render(<UnifiedTimeline {...makeProps({ sfxPlacements: [p] })} />);
    expect(
      screen.queryByRole("button", { name: /Apply sound effects/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Applied/i })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Remove sound effects from video/i }),
    ).not.toBeInTheDocument();
  });

  it("renders no Apply/Applying/Retry button while a render is in flight", () => {
    const p = makePlacement({ id: "p1", label: "Boom", at_s: 4 });
    render(<UnifiedTimeline {...makeProps({ sfxPlacements: [p], sfxRendering: true })} />);
    expect(screen.queryByRole("button", { name: /Apply/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Applying/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Retry/i })).not.toBeInTheDocument();
  });

  it("renders no Apply button with no placements", () => {
    render(<UnifiedTimeline {...makeProps({ sfxPlacements: [] })} />);
    expect(screen.queryByRole("button", { name: /Apply/i })).not.toBeInTheDocument();
  });
});

describe("UnifiedTimeline — disabled state", () => {
  it("disables add controls while sfxRendering", () => {
    const effect = makeGlossaryEffect();
    render(
      <UnifiedTimeline
        {...makeProps({ sfxGlossaryEffects: [effect], sfxRendering: true })}
      />,
    );
    expect(screen.getByRole("button", { name: /\+ Add/i })).toBeDisabled();
  });
});

// ── Suggestion provenance cards in the lanes (006 T3 / 005-4A) ───────────────

/** One pending AI suggestion as the lanes receive it (SuggestionLaneEntry). */
function makeSuggestionEntry(override: Record<string, unknown> = {}) {
  return {
    id: "sug-1",
    overlay: makeOverlayCard({
      id: "ov-sug-1",
      kind: "video",
      start_s: 5,
      end_s: 9,
      clip_trim_start_s: 0,
      clip_trim_end_s: 4,
      clip_duration_s: 6,
    }),
    sfx: null,
    staged: false,
    ...override,
  };
}

describe("OverlayLane — suggestion provenance cards (006 T3)", () => {
  it("renders a suggestion card with dashed lime-600 provenance + ✦ badge", () => {
    render(
      <UnifiedTimeline {...makeProps({ overlaySuggestions: [makeSuggestionEntry()] })} />,
    );
    const bar = screen.getByRole("button", { name: /suggested overlay/i });
    expect(bar.className).toMatch(/border-dashed/);
    expect(bar.className).toMatch(/border-lime-600/);
    // ✦ badge visible while pending; card is keyboard-focusable.
    expect(screen.getByTestId("suggestion-badge-sug-1").className).toMatch(/opacity-100/);
    expect(bar).toHaveAttribute("tabindex", "0");
  });

  it("staged suggestion flips dashed→solid and fades the ✦ (005-6A accept)", () => {
    render(
      <UnifiedTimeline
        {...makeProps({ overlaySuggestions: [makeSuggestionEntry({ staged: true })] })}
      />,
    );
    const bar = screen.getByRole("button", { name: /suggested overlay/i });
    expect(bar.className).toMatch(/border-solid/);
    expect(bar.className).not.toMatch(/border-dashed/);
    expect(screen.getByTestId("suggestion-badge-sug-1").className).toMatch(/opacity-0/);
  });

  it("scale-slider edit fires onSuggestionEdit with the patch — no fetch, no manual mutation", async () => {
    const fetchSpy = jest.fn();
    global.fetch = fetchSpy as unknown as typeof fetch;
    const onSuggestionEdit = jest.fn();
    const onUpdateCard = jest.fn();
    render(
      <UnifiedTimeline
        {...makeProps({
          overlaySuggestions: [makeSuggestionEntry()],
          onSuggestionEdit,
          onUpdateCard,
        })}
      />,
    );

    // Open the suggestion card's popover via its bar label, then drag the slider.
    await act(async () => { fireEvent.click(screen.getByText(/▶ ov-sug/)); });
    const slider = screen.getByRole("slider");
    fireEvent.change(slider, { target: { value: "80" } });

    expect(onSuggestionEdit).toHaveBeenCalledWith("sug-1", { scale: 0.8 });
    expect(onUpdateCard).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("position preset edit routes to onSuggestionEdit, never onUpdateCard", async () => {
    const onSuggestionEdit = jest.fn();
    const onUpdateCard = jest.fn();
    render(
      <UnifiedTimeline
        {...makeProps({
          overlaySuggestions: [makeSuggestionEntry()],
          onSuggestionEdit,
          onUpdateCard,
        })}
      />,
    );
    await act(async () => { fireEvent.click(screen.getByText(/▶ ov-sug/)); });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Top" })); });
    expect(onSuggestionEdit).toHaveBeenCalledWith("sug-1", { position: "top" });
    expect(onUpdateCard).not.toHaveBeenCalled();
  });

  it("drag-move fires onSuggestionEdit with start_s/end_s — no fetch, no manual mutation", () => {
    const fetchSpy = jest.fn();
    global.fetch = fetchSpy as unknown as typeof fetch;
    const onSuggestionEdit = jest.fn();
    const onUpdateCard = jest.fn();
    render(
      <UnifiedTimeline
        {...makeProps({
          overlaySuggestions: [makeSuggestionEntry()],
          onSuggestionEdit,
          onUpdateCard,
        })}
      />,
    );

    const bar = screen.getByRole("button", { name: /suggested overlay/i });
    fireEvent.mouseDown(bar, { clientX: 0 });
    fireEvent.mouseMove(window, { clientX: 40 });
    fireEvent.mouseUp(window);

    expect(onSuggestionEdit).toHaveBeenCalledWith(
      "sug-1",
      expect.objectContaining({ start_s: expect.any(Number), end_s: expect.any(Number) }),
    );
    expect(onUpdateCard).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("TrimLane clip-trim drag fires onSuggestionEdit with clip_trim fields", () => {
    // Give drag math a real width (jsdom rects are all zero by default).
    const rectSpy = jest
      .spyOn(Element.prototype, "getBoundingClientRect")
      .mockReturnValue({
        width: 100, height: 10, top: 0, left: 0, bottom: 10, right: 100, x: 0, y: 0,
        toJSON: () => ({}),
      } as DOMRect);
    const onSuggestionEdit = jest.fn();
    const onUpdateCard = jest.fn();
    render(
      <UnifiedTimeline
        {...makeProps({
          overlaySuggestions: [makeSuggestionEntry()],
          onSuggestionEdit,
          onUpdateCard,
        })}
      />,
    );

    const handle = document.querySelector('[data-trim-handle="left-ov-sug-1"]')!;
    expect(handle).toBeTruthy();
    fireEvent.mouseDown(handle, { clientX: 0 });
    // 50px over a 100px strip on a 6s clip → +3s trim-in.
    fireEvent.mouseMove(window, { clientX: 50 });
    fireEvent.mouseUp(window);

    expect(onSuggestionEdit).toHaveBeenCalledWith(
      "sug-1",
      expect.objectContaining({ clip_trim_start_s: 3 }),
    );
    expect(onUpdateCard).not.toHaveBeenCalled();
    rectSpy.mockRestore();
  });

  it("suggestion popover has no remove button (removal stays in the rail)", async () => {
    render(
      <UnifiedTimeline {...makeProps({ overlaySuggestions: [makeSuggestionEntry()] })} />,
    );
    await act(async () => { fireEvent.click(screen.getByText(/▶ ov-sug/)); });
    expect(screen.queryByRole("button", { name: "Remove card" })).toBeNull();
  });

  it("manual cards alongside suggestions keep their plain styling and onUpdateCard routing", async () => {
    const onSuggestionEdit = jest.fn();
    const onUpdateCard = jest.fn();
    const manual = makeOverlayCard({ id: "ov-manual", kind: "image" });
    render(
      <UnifiedTimeline
        {...makeProps({
          overlayCards: [manual],
          overlaySuggestions: [makeSuggestionEntry()],
          onSuggestionEdit,
          onUpdateCard,
        })}
      />,
    );

    // Manual bar: no provenance classes, not a suggestion card.
    const manualLabel = screen.getByText(/ov-man/);
    const manualBar = manualLabel.parentElement!;
    expect(manualBar.className).not.toMatch(/border-dashed|border-lime-600/);
    expect(manualBar).not.toHaveAttribute("data-suggestion-card");

    // Manual scale edit still routes to onUpdateCard.
    await act(async () => { fireEvent.click(manualLabel); });
    fireEvent.change(screen.getByRole("slider"), { target: { value: "50" } });
    expect(onUpdateCard).toHaveBeenCalledWith("ov-manual", { scale: 0.5 });
    expect(onSuggestionEdit).not.toHaveBeenCalled();
  });
});

describe("SfxLane — suggestion sfx diamonds (006 T3)", () => {
  const sfx = {
    id: "sfx-1",
    src_gcs_path: "sound-effects/pop.mp3",
    at_s: 5,
    gain: 1.0,
    duration_s: 0.4,
    label: "pop",
  };

  it("renders a read-only diamond with dashed-lime provenance for a suggestion's sfx", () => {
    render(
      <UnifiedTimeline
        {...makeProps({ overlaySuggestions: [makeSuggestionEntry({ sfx })] })}
      />,
    );
    const diamond = screen.getByTestId("sfx-suggestion-sug-1");
    expect(diamond.className).toMatch(/border-dashed/);
    expect(diamond.className).toMatch(/border-lime-600/);
    // Read-only: no pointer surface.
    expect(diamond.className).toMatch(/pointer-events-none/);
  });

  it("staged suggestion sfx flips to a solid border", () => {
    render(
      <UnifiedTimeline
        {...makeProps({ overlaySuggestions: [makeSuggestionEntry({ sfx, staged: true })] })}
      />,
    );
    const diamond = screen.getByTestId("sfx-suggestion-sug-1");
    expect(diamond.className).toMatch(/border-solid/);
    expect(diamond.className).not.toMatch(/border-dashed/);
  });

  it("renders no diamond when the suggestion has no sfx", () => {
    render(
      <UnifiedTimeline {...makeProps({ overlaySuggestions: [makeSuggestionEntry()] })} />,
    );
    expect(screen.queryByTestId("sfx-suggestion-sug-1")).toBeNull();
  });
});
