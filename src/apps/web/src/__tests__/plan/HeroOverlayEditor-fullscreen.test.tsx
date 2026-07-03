/**
 * Plan 009 T4 — HeroOverlayEditor fullscreen cutaway entries.
 *
 * Covers:
 *  1. Rendering: fullscreen suggestion is full-frame (inset 0), media is
 *     cover-cropped with zero baked chrome; editor chrome is an INSET
 *     dashed-lime outline (outline-offset -2px) + the ✦ badge INSIDE the
 *     frame; the resize handle is HIDDEN; video media preload="auto".
 *  2. No spatial gestures: pointer drag on a fullscreen card patches nothing
 *     through onSuggestionEdit (no move/resize/keyboard-move for takeovers).
 *  3. Click-to-edit (E6): a click inside the top 85% click target pauses the
 *     hero video (no auto-resume) and fires onRequestEditCard with the entry
 *     id; Enter on the focused target does the same.
 *  4. Pass-through band (E6): the bottom ~15% is pointer-events:none — a
 *     click there reaches the <video>, never the edit request; while the
 *     pointer hovers the band the card visual drops to 40% opacity (visual
 *     cue only), restored when the pointer moves back up.
 *
 * Same jsdom scaffolding as HeroOverlayEditor.test.tsx: PointerEvent
 * polyfill, mocked pointer capture, fixed 270×480 (9:16) rects.
 */

import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import HeroOverlayEditor from "@/app/plan/_components/HeroOverlayEditor";
import type { SuggestionLaneEntry } from "@/app/plan/_components/UnifiedTimelineTypes";
import type { MediaOverlay } from "@/lib/plan-api";

const FLAG = "NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED";
const BOX_W = 270;
const BOX_H = 480;

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
  global.fetch = jest.fn(async () => {
    throw new Error("HeroOverlayEditor must never hit the network");
  }) as unknown as typeof fetch;
});

afterEach(() => {
  jest.restoreAllMocks();
  delete process.env[FLAG];
});

function makeOverlay(overrides: Partial<MediaOverlay> = {}): MediaOverlay {
  return {
    id: "ov-1",
    kind: "image",
    src_gcs_path: "users/u1/plan/item-1/pool/payload.png",
    position: "center",
    x_frac: 0.5,
    y_frac: 0.3,
    scale: 0.4,
    display_mode: "fullscreen",
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
    id: "sug-fs",
    overlay: makeOverlay(overlayOverrides),
    sfx: null,
    staged: false,
    ...overrides,
  };
}

function renderInHero(props: {
  entries: SuggestionLaneEntry[];
  onSuggestionEdit?: (id: string, patch: Partial<MediaOverlay>) => void;
  onRequestEditCard?: (cardId: string) => void;
  currentTimeS?: number;
  resolveCardUrl?: (overlay: MediaOverlay) => string | undefined;
}) {
  const {
    entries,
    onSuggestionEdit = jest.fn(),
    onRequestEditCard,
    currentTimeS = 6,
    resolveCardUrl = () => "https://storage.example/signed/payload.png",
  } = props;
  const utils = render(
    <div className="relative aspect-[9/16]">
      <video />
      <HeroOverlayEditor
        entries={entries}
        onSuggestionEdit={onSuggestionEdit}
        currentTimeS={currentTimeS}
        resolveCardUrl={resolveCardUrl}
        onRequestEditCard={onRequestEditCard}
      />
    </div>,
  );
  return {
    ...utils,
    hero: utils.container.firstElementChild as HTMLElement,
    video: utils.container.querySelector("video") as HTMLVideoElement,
  };
}

function mockPlayingVideo(video: HTMLVideoElement) {
  Object.defineProperty(video, "paused", { value: false, configurable: true });
  const pauseSpy = jest.spyOn(video, "pause").mockImplementation(() => {});
  const playSpy = jest.spyOn(video, "play").mockImplementation(() => Promise.resolve());
  return { pauseSpy, playSpy };
}

// ── 1. Rendering ──────────────────────────────────────────────────────────────

describe("HeroOverlayEditor — fullscreen suggestion rendering", () => {
  it("renders full-frame with inset dashed-lime outline, inside ✦ badge, and NO resize handle", () => {
    renderInHero({ entries: [makeEntry()] });

    const card = screen.getByTestId("hero-fullscreen-card-sug-fs");
    expect(card).toHaveStyle({
      position: "absolute",
      left: "0px",
      top: "0px",
      right: "0px",
      bottom: "0px",
    });
    expect(card.style.transform).toBe("");
    expect(card.style.width).toBe("");

    // Media: cover-crop, zero baked chrome.
    const img = card.querySelector("img")!;
    expect(img).toHaveClass("w-full", "h-full", "object-cover");
    expect(img.className).not.toMatch(/rounded/);

    // Editor chrome: INSET outline (never affecting layout) — dashed while
    // pending — with the provenance badge inside the frame.
    const outline = screen.getByTestId("hero-fullscreen-outline-sug-fs");
    expect(outline).toHaveClass("outline-dashed", "outline-lime-600");
    expect(outline).toHaveStyle({ outlineOffset: "-2px" });
    expect(card.textContent).toContain("✦");

    // No resize handle, and the pip card shell is not used at all.
    expect(screen.queryByTestId("hero-suggestion-resize-sug-fs")).toBeNull();
    expect(screen.queryByTestId("hero-suggestion-card-sug-fs")).toBeNull();

    // Visible affordance for the single click target.
    expect(card.textContent).toContain("⛶ Full screen · edit");
  });

  it("staged fullscreen entry keeps the inset outline but solid (not dashed)", () => {
    renderInHero({ entries: [makeEntry({ staged: true })] });
    const outline = screen.getByTestId("hero-fullscreen-outline-sug-fs");
    expect(outline.className).not.toContain("outline-dashed");
    expect(outline).toHaveClass("outline-lime-600");
  });

  it("fullscreen video media preloads auto (first-frame readiness at window entry)", () => {
    renderInHero({
      entries: [makeEntry({}, { kind: "video" })],
      resolveCardUrl: () => "https://storage.example/signed/clip.mp4",
    });
    const video = screen
      .getByTestId("hero-fullscreen-card-sug-fs")
      .querySelector("video")!;
    expect(video).toHaveAttribute("preload", "auto");
    expect(video).toHaveClass("object-cover");
  });

  it("stays time-scoped: hidden outside the card's window", () => {
    renderInHero({ entries: [makeEntry()], currentTimeS: 20 }); // window 5..14
    expect(screen.queryByTestId("hero-fullscreen-card-sug-fs")).toBeNull();
  });
});

// ── 2. No spatial gestures ────────────────────────────────────────────────────

describe("HeroOverlayEditor — fullscreen has no move/resize/keyboard-move", () => {
  it("pointer drag across the frame patches nothing through onSuggestionEdit", () => {
    const onSuggestionEdit = jest.fn();
    renderInHero({ entries: [makeEntry()], onSuggestionEdit });

    const card = screen.getByTestId("hero-fullscreen-card-sug-fs");
    fireEvent.pointerDown(card, { pointerId: 1, clientX: 100, clientY: 100, button: 0 });
    fireEvent.pointerMove(card, { pointerId: 1, clientX: 200, clientY: 200 });
    fireEvent.pointerUp(card, { pointerId: 1, clientX: 200, clientY: 200 });

    const target = screen.getByTestId("hero-fullscreen-edit-sug-fs");
    fireEvent.pointerDown(target, { pointerId: 2, clientX: 100, clientY: 100, button: 0 });
    fireEvent.pointerMove(target, { pointerId: 2, clientX: 200, clientY: 200 });

    expect(onSuggestionEdit).not.toHaveBeenCalled();
  });

  it("arrow keys / plus / minus on the focused target patch nothing", () => {
    const onSuggestionEdit = jest.fn();
    renderInHero({ entries: [makeEntry()], onSuggestionEdit });
    const target = screen.getByTestId("hero-fullscreen-edit-sug-fs");
    for (const key of ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "+", "-"]) {
      fireEvent.keyDown(target, { key });
    }
    expect(onSuggestionEdit).not.toHaveBeenCalled();
  });
});

// ── 3. Click-to-edit ──────────────────────────────────────────────────────────

describe("HeroOverlayEditor — fullscreen click-to-edit (E6)", () => {
  it("click inside the top 85% pauses the hero (no resume) and fires onRequestEditCard", () => {
    const onRequestEditCard = jest.fn();
    const { video } = renderInHero({ entries: [makeEntry()], onRequestEditCard });
    const { pauseSpy, playSpy } = mockPlayingVideo(video);

    // clientY 144 of 480 → 30% down: well inside the top-85% click target.
    fireEvent.click(screen.getByTestId("hero-fullscreen-edit-sug-fs"), {
      clientX: 135,
      clientY: 144,
    });

    expect(pauseSpy).toHaveBeenCalledTimes(1);
    expect(playSpy).not.toHaveBeenCalled(); // heading into the popover — never auto-resume
    expect(onRequestEditCard).toHaveBeenCalledTimes(1);
    expect(onRequestEditCard).toHaveBeenCalledWith("sug-fs");
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("Enter on the focused click target requests the edit too", () => {
    const onRequestEditCard = jest.fn();
    renderInHero({ entries: [makeEntry()], onRequestEditCard });
    fireEvent.keyDown(screen.getByTestId("hero-fullscreen-edit-sug-fs"), { key: "Enter" });
    expect(onRequestEditCard).toHaveBeenCalledWith("sug-fs");
  });

  it("the click target only covers the top 85% of the frame", () => {
    renderInHero({ entries: [makeEntry()], onRequestEditCard: jest.fn() });
    const target = screen.getByTestId("hero-fullscreen-edit-sug-fs");
    expect(target).toHaveClass("pointer-events-auto");
    expect(target).toHaveStyle({ bottom: "15%" });
  });
});

// ── 4. Pass-through band ──────────────────────────────────────────────────────

describe("HeroOverlayEditor — bottom 15% pointer pass-through band (E6)", () => {
  it("band is pointer-events:none so clicks over the native controls reach the video", () => {
    const onRequestEditCard = jest.fn();
    const { video } = renderInHero({ entries: [makeEntry()], onRequestEditCard });

    const band = screen.getByTestId("hero-fullscreen-band-sug-fs");
    expect(band).toHaveClass("pointer-events-none");
    expect(band).toHaveStyle({ height: "15%" });

    // A click landing on the video (as it does in a real browser when the
    // band lets it through) never turns into an edit request.
    fireEvent.click(video, { clientX: 135, clientY: 460 });
    expect(onRequestEditCard).not.toHaveBeenCalled();
  });

  it("hovering the band drops the card visual to 40% opacity; leaving restores it", () => {
    const { hero } = renderInHero({ entries: [makeEntry()], onRequestEditCard: jest.fn() });
    const card = screen.getByTestId("hero-fullscreen-card-sug-fs");
    expect(card).toHaveStyle({ opacity: "1" });

    // Pointer at 450/480 = 93.75% down → inside the bottom-15% band. The band
    // itself is pointer-events:none, so the move lands on the video and
    // bubbles to the shared container — which is what the editor listens to.
    fireEvent.pointerMove(hero, { clientX: 135, clientY: 450 });
    expect(card).toHaveStyle({ opacity: "0.4" });

    // Back above the 85% line → full opacity.
    fireEvent.pointerMove(hero, { clientX: 135, clientY: 200 });
    expect(card).toHaveStyle({ opacity: "1" });

    // Pointer leaves the hero entirely → restored as well.
    fireEvent.pointerMove(hero, { clientX: 135, clientY: 470 });
    fireEvent.pointerLeave(hero);
    expect(card).toHaveStyle({ opacity: "1" });
  });
});
