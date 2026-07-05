/**
 * Tests for plan/_components/HeroOverlayEditor.tsx + overlayCardStyle.ts
 * (plans/007 Fix 2 — on-preview direct manipulation for AI overlay suggestions).
 *
 * Covers:
 *  1. overlayCardStyle util — the ONE percent-math source (center %, width =
 *     scale·100%, translate(-50%,-50%)).
 *  2. Renders kept entries at correct percent style; dashed while !staged,
 *     solid when staged.
 *  3. Drag patches x_frac/y_frac (+ position:"custom") through onSuggestionEdit
 *     with ZERO fetch; the hero video is paused during the drag and resumed on
 *     release when it was playing.
 *  4. Resize handle patches scale, clamped to [0.05, 1.0]; x/y untouched.
 *  5. Keyboard: arrows move 1% (shift 5%), +/- scale ±0.05 — all through
 *     onSuggestionEdit.
 *  6. Time-scoping: card hidden outside its [start_s, end_s] window; the
 *     actively-dragged card stays mounted even if the playhead exits.
 *  7. Flag off or zero entries → renders nothing.
 *
 * jsdom has no PointerEvent, pointer capture, or layout: a minimal
 * PointerEvent polyfill (extends MouseEvent, so clientX/button survive),
 * mocked set/releasePointerCapture, and a fixed 270×480 (9:16)
 * getBoundingClientRect stand in.
 */

import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import HeroOverlayEditor from "@/app/plan/_components/HeroOverlayEditor";
import { overlayCardStyle } from "@/app/plan/_components/overlayCardStyle";
import type { SuggestionLaneEntry } from "@/app/plan/_components/UnifiedTimelineTypes";
import type { MediaOverlay } from "@/lib/plan-api";

const FLAG = "NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED";
// 9:16 box — matches the aspect-[9/16] hero container the percent math pins.
const BOX_W = 270;
const BOX_H = 480;

// ── jsdom polyfills / mocks ───────────────────────────────────────────────────

// jsdom 20 has no PointerEvent; @testing-library/dom falls back to plain Event,
// which drops clientX/clientY/button. Extend MouseEvent so coordinates survive.
class PointerEventPolyfill extends MouseEvent {
  pointerId: number;
  pointerType: string;
  isPrimary: boolean;
  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 0;
    this.pointerType = init.pointerType ?? "mouse";
    this.isPrimary = init.isPrimary ?? true;
  }
}

beforeAll(() => {
  (window as unknown as Record<string, unknown>).PointerEvent = PointerEventPolyfill;
  // jsdom doesn't implement pointer capture — mock it (spec: mock pointer capture).
  Object.defineProperty(HTMLElement.prototype, "setPointerCapture", {
    value: jest.fn(),
    configurable: true,
    writable: true,
  });
  Object.defineProperty(HTMLElement.prototype, "releasePointerCapture", {
    value: jest.fn(),
    configurable: true,
    writable: true,
  });
});

beforeEach(() => {
  process.env[FLAG] = "true";
  jest.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    width: BOX_W,
    height: BOX_H,
    top: 0,
    left: 0,
    right: BOX_W,
    bottom: BOX_H,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect);
  // Stage-fires-no-network contract: nothing in this component may fetch.
  global.fetch = jest.fn(async () => {
    throw new Error("HeroOverlayEditor must never hit the network");
  }) as unknown as typeof fetch;
});

afterEach(() => {
  jest.restoreAllMocks();
  delete process.env[FLAG];
});

// ── Fixtures ──────────────────────────────────────────────────────────────────

function makeOverlay(overrides: Partial<MediaOverlay> = {}): MediaOverlay {
  return {
    id: "ov-1",
    kind: "image",
    src_gcs_path: "users/u1/plan/item-1/pool/payload.png",
    position: "center",
    x_frac: 0.5,
    y_frac: 0.3,
    scale: 0.4,
    start_s: 5,
    end_s: 14,
    z: 10,
    ...overrides,
  };
}

function makeEntry(
  overrides: Partial<SuggestionLaneEntry> = {},
  overlayOverrides: Partial<MediaOverlay> = {},
): SuggestionLaneEntry {
  return {
    id: "sug-1",
    overlay: makeOverlay(overlayOverrides),
    sfx: null,
    staged: false,
    ...overrides,
  };
}

/** Mount the editor next to a <video> inside one container — the same shape as
 *  the Hero's aspect-[9/16] box (the editor finds the video via that parent). */
function renderInHero(props: {
  entries: SuggestionLaneEntry[];
  onSuggestionEdit?: (id: string, patch: Partial<MediaOverlay>) => void;
  currentTimeS?: number;
  resolveCardUrl?: (overlay: MediaOverlay) => string | undefined;
}) {
  const {
    entries,
    onSuggestionEdit = jest.fn(),
    currentTimeS = 6,
    resolveCardUrl,
  } = props;
  const ui = (t: number) => (
    <div className="relative aspect-[9/16]">
      <video />
      <HeroOverlayEditor
        entries={entries}
        onSuggestionEdit={onSuggestionEdit}
        currentTimeS={t}
        resolveCardUrl={resolveCardUrl}
      />
    </div>
  );
  const utils = render(ui(currentTimeS));
  return {
    ...utils,
    rerenderAtTime: (t: number) => utils.rerender(ui(t)),
    video: utils.container.querySelector("video") as HTMLVideoElement,
  };
}

/** Make the jsdom <video> report "playing" and spy pause/play. */
function mockPlayingVideo(video: HTMLVideoElement) {
  Object.defineProperty(video, "paused", { value: false, configurable: true });
  const pauseSpy = jest.spyOn(video, "pause").mockImplementation(() => {});
  const playSpy = jest
    .spyOn(video, "play")
    .mockImplementation(() => Promise.resolve());
  return { pauseSpy, playSpy };
}

// ── 1. overlayCardStyle util ──────────────────────────────────────────────────

describe("overlayCardStyle", () => {
  it("maps x_frac/y_frac/scale to center-anchored percent CSS", () => {
    expect(overlayCardStyle({ x_frac: 0.25, y_frac: 0.75, scale: 0.4 })).toEqual({
      position: "absolute",
      left: "25%",
      top: "75%",
      transform: "translate(-50%, -50%)",
      width: "40%",
    });
  });
});

// ── 2. Rendering ──────────────────────────────────────────────────────────────

describe("HeroOverlayEditor — rendering", () => {
  it("renders kept entries at their percent position/size", () => {
    renderInHero({
      entries: [makeEntry()],
      resolveCardUrl: () => "https://storage.example/signed/payload.png",
    });
    const card = screen.getByTestId("hero-suggestion-card-sug-1");
    expect(card).toHaveStyle({
      left: "50%",
      top: "30%",
      width: "40%",
      transform: "translate(-50%, -50%)",
    });
    // Signed pool thumbnail rendered for image cards.
    const img = card.querySelector("img");
    expect(img).toHaveAttribute("src", "https://storage.example/signed/payload.png");
    // Provenance marker.
    expect(card.textContent).toContain("✦");
  });

  it("dashed border while !staged, solid when staged (006 tokens)", () => {
    renderInHero({
      entries: [
        makeEntry({ id: "sug-a", staged: false }),
        makeEntry({ id: "sug-b", staged: true }, { id: "ov-2" }),
      ],
    });
    expect(screen.getByTestId("hero-suggestion-card-sug-a").className).toContain(
      "border-dashed",
    );
    expect(screen.getByTestId("hero-suggestion-card-sug-b").className).toContain(
      "border-solid",
    );
  });

  it("layer is pointer-events-none; cards are pointer-events-auto (video stays usable)", () => {
    renderInHero({ entries: [makeEntry()] });
    expect(screen.getByTestId("hero-overlay-editor").className).toContain(
      "pointer-events-none",
    );
    expect(screen.getByTestId("hero-suggestion-card-sug-1").className).toContain(
      "pointer-events-auto",
    );
    // No gesture active → no click-swallowing backdrop mounted.
    expect(screen.queryByTestId("hero-gesture-backdrop")).toBeNull();
  });

  it("cards are keyboard-focusable", () => {
    renderInHero({ entries: [makeEntry()] });
    expect(screen.getByTestId("hero-suggestion-card-sug-1")).toHaveAttribute(
      "tabindex",
      "0",
    );
  });
});

// ── 3. Drag ───────────────────────────────────────────────────────────────────

describe("HeroOverlayEditor — drag", () => {
  it("patches x_frac/y_frac (position: custom) via onSuggestionEdit, no fetch; pauses then resumes the video", () => {
    const onSuggestionEdit = jest.fn();
    const { video } = renderInHero({ entries: [makeEntry()], onSuggestionEdit });
    const { pauseSpy, playSpy } = mockPlayingVideo(video);

    const card = screen.getByTestId("hero-suggestion-card-sug-1");
    fireEvent.pointerDown(card, { pointerId: 1, clientX: 135, clientY: 144, button: 0 });
    expect(pauseSpy).toHaveBeenCalledTimes(1);
    expect(playSpy).not.toHaveBeenCalled();

    // +27px of 270 → +0.1 x; +48px of 480 → +0.1 y.
    fireEvent.pointerMove(card, { pointerId: 1, clientX: 162, clientY: 192 });
    expect(onSuggestionEdit).toHaveBeenCalledTimes(1);
    const [id, patch] = onSuggestionEdit.mock.calls[0];
    expect(id).toBe("sug-1");
    expect(patch.position).toBe("custom");
    expect(patch.x_frac).toBeCloseTo(0.6, 5);
    expect(patch.y_frac).toBeCloseTo(0.4, 5);
    expect(patch.scale).toBeUndefined();

    // The gesture backdrop swallows stray clicks off the native controls.
    expect(screen.getByTestId("hero-gesture-backdrop")).toBeInTheDocument();

    fireEvent.pointerUp(card, { pointerId: 1, clientX: 162, clientY: 192 });
    expect(playSpy).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("hero-gesture-backdrop")).toBeNull();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("clamps x_frac/y_frac to [0, 1]", () => {
    const onSuggestionEdit = jest.fn();
    renderInHero({ entries: [makeEntry()], onSuggestionEdit });

    const card = screen.getByTestId("hero-suggestion-card-sug-1");
    fireEvent.pointerDown(card, { pointerId: 1, clientX: 100, clientY: 100, button: 0 });
    fireEvent.pointerMove(card, { pointerId: 1, clientX: 100 + 5000, clientY: 100 - 5000 });

    const [, patch] = onSuggestionEdit.mock.calls[0];
    expect(patch.x_frac).toBe(1);
    expect(patch.y_frac).toBe(0);
  });

  it("does not resume playback when the video was already paused", () => {
    const onSuggestionEdit = jest.fn();
    const { video } = renderInHero({ entries: [makeEntry()], onSuggestionEdit });
    // jsdom default: paused === true.
    const pauseSpy = jest.spyOn(video, "pause").mockImplementation(() => {});
    const playSpy = jest.spyOn(video, "play").mockImplementation(() => Promise.resolve());

    const card = screen.getByTestId("hero-suggestion-card-sug-1");
    fireEvent.pointerDown(card, { pointerId: 1, clientX: 100, clientY: 100, button: 0 });
    fireEvent.pointerUp(card, { pointerId: 1, clientX: 100, clientY: 100 });

    expect(pauseSpy).not.toHaveBeenCalled();
    expect(playSpy).not.toHaveBeenCalled();
  });
});

// ── 4. Resize ─────────────────────────────────────────────────────────────────

describe("HeroOverlayEditor — resize handle", () => {
  it("patches scale around the card center (x/y untouched)", () => {
    const onSuggestionEdit = jest.fn();
    renderInHero({ entries: [makeEntry()], onSuggestionEdit });

    const handle = screen.getByTestId("hero-suggestion-resize-sug-1");
    fireEvent.pointerDown(handle, { pointerId: 2, clientX: 100, clientY: 100, button: 0 });
    // +27px on a 270px box → width grows 2·27/270 = +0.2 (resize around center).
    fireEvent.pointerMove(handle, { pointerId: 2, clientX: 127, clientY: 100 });

    const [id, patch] = onSuggestionEdit.mock.calls[0];
    expect(id).toBe("sug-1");
    expect(patch.scale).toBeCloseTo(0.6, 5);
    expect(patch.x_frac).toBeUndefined();
    expect(patch.y_frac).toBeUndefined();
    expect(patch.position).toBeUndefined();
  });

  it("clamps scale to [0.05, 1.0]", () => {
    const onSuggestionEdit = jest.fn();
    renderInHero({ entries: [makeEntry()], onSuggestionEdit });
    const handle = screen.getByTestId("hero-suggestion-resize-sug-1");

    fireEvent.pointerDown(handle, { pointerId: 2, clientX: 100, clientY: 100, button: 0 });
    fireEvent.pointerMove(handle, { pointerId: 2, clientX: 100 + 5000, clientY: 100 });
    expect(onSuggestionEdit.mock.calls.at(-1)![1].scale).toBe(1.0);

    fireEvent.pointerMove(handle, { pointerId: 2, clientX: 100 - 5000, clientY: 100 });
    expect(onSuggestionEdit.mock.calls.at(-1)![1].scale).toBe(0.05);
  });
});

// ── 5. Keyboard ───────────────────────────────────────────────────────────────

describe("HeroOverlayEditor — keyboard", () => {
  function lastPatch(fn: jest.Mock) {
    return fn.mock.calls.at(-1)![1] as Partial<MediaOverlay>;
  }

  it("arrows move 1% (shift 5%), position becomes custom", () => {
    const onSuggestionEdit = jest.fn();
    renderInHero({ entries: [makeEntry()], onSuggestionEdit });
    const card = screen.getByTestId("hero-suggestion-card-sug-1");

    fireEvent.keyDown(card, { key: "ArrowRight" });
    expect(lastPatch(onSuggestionEdit).x_frac).toBeCloseTo(0.51, 5);
    expect(lastPatch(onSuggestionEdit).position).toBe("custom");

    fireEvent.keyDown(card, { key: "ArrowLeft", shiftKey: true });
    expect(lastPatch(onSuggestionEdit).x_frac).toBeCloseTo(0.45, 5);

    fireEvent.keyDown(card, { key: "ArrowUp" });
    expect(lastPatch(onSuggestionEdit).y_frac).toBeCloseTo(0.29, 5);

    fireEvent.keyDown(card, { key: "ArrowDown", shiftKey: true });
    expect(lastPatch(onSuggestionEdit).y_frac).toBeCloseTo(0.35, 5);
  });

  it("+/- scale ±0.05 within clamps", () => {
    const onSuggestionEdit = jest.fn();
    renderInHero({ entries: [makeEntry()], onSuggestionEdit });
    const card = screen.getByTestId("hero-suggestion-card-sug-1");

    fireEvent.keyDown(card, { key: "+" });
    expect(lastPatch(onSuggestionEdit).scale).toBeCloseTo(0.45, 5);

    fireEvent.keyDown(card, { key: "-" });
    expect(lastPatch(onSuggestionEdit).scale).toBeCloseTo(0.35, 5);

    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("clamps keyboard scale at the floor", () => {
    const onSuggestionEdit = jest.fn();
    renderInHero({
      entries: [makeEntry({}, { scale: 0.06 })],
      onSuggestionEdit,
    });
    fireEvent.keyDown(screen.getByTestId("hero-suggestion-card-sug-1"), { key: "-" });
    expect(onSuggestionEdit.mock.calls.at(-1)![1].scale).toBe(0.05);
  });

  it("unrelated keys do not patch", () => {
    const onSuggestionEdit = jest.fn();
    renderInHero({ entries: [makeEntry()], onSuggestionEdit });
    fireEvent.keyDown(screen.getByTestId("hero-suggestion-card-sug-1"), { key: "Enter" });
    expect(onSuggestionEdit).not.toHaveBeenCalled();
  });
});

// ── 6. Time-scoping ───────────────────────────────────────────────────────────

describe("HeroOverlayEditor — time-scoping", () => {
  it("card hidden outside its [start_s, end_s] window", () => {
    renderInHero({ entries: [makeEntry()], currentTimeS: 20 }); // window is 5..14
    expect(screen.queryByTestId("hero-suggestion-card-sug-1")).toBeNull();
    // The layer itself still mounts (flag on, entries non-empty) — just no card.
    expect(screen.getByTestId("hero-overlay-editor")).toBeInTheDocument();
  });

  it("actively-dragged card stays mounted when the playhead exits its window", () => {
    const onSuggestionEdit = jest.fn();
    const { rerenderAtTime } = renderInHero({
      entries: [makeEntry()],
      onSuggestionEdit,
      currentTimeS: 6,
    });
    const card = screen.getByTestId("hero-suggestion-card-sug-1");
    fireEvent.pointerDown(card, { pointerId: 1, clientX: 100, clientY: 100, button: 0 });

    rerenderAtTime(20); // outside 5..14 mid-gesture
    expect(screen.getByTestId("hero-suggestion-card-sug-1")).toBeInTheDocument();

    fireEvent.pointerUp(screen.getByTestId("hero-suggestion-card-sug-1"), {
      pointerId: 1,
      clientX: 100,
      clientY: 100,
    });
    rerenderAtTime(20); // gesture over → time-scoping applies again
    expect(screen.queryByTestId("hero-suggestion-card-sug-1")).toBeNull();
  });
});

// ── 7. Gating ─────────────────────────────────────────────────────────────────

describe("HeroOverlayEditor — gating", () => {
  it("renders nothing when the flag is off", () => {
    delete process.env[FLAG];
    const { container } = render(
      <HeroOverlayEditor
        entries={[makeEntry()]}
        onSuggestionEdit={jest.fn()}
        currentTimeS={6}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing with zero entries", () => {
    const { container } = render(
      <HeroOverlayEditor entries={[]} onSuggestionEdit={jest.fn()} currentTimeS={6} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
