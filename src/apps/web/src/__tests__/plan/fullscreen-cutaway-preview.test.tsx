/**
 * Plan 009 T4 — fullscreen cutaways in the live CSS preview.
 *
 * Covers the component half of the live-preview contract:
 *  1. PARITY GUARD (ts half — pairs with the python builder test on
 *     display_mode): overlayCardStyle branches on display_mode exactly like
 *     the FFmpeg builder — "fullscreen" → full-frame inset positioning (all
 *     four insets 0, no translate, no width%), "pip"/absent → today's percent
 *     math, byte-identical.
 *  2. mediaClassFor — pip keeps "w-full h-auto rounded"; fullscreen is
 *     "w-full h-full object-cover" with zero chrome (no rounded, no shadow).
 *  3. LiveOverlayCardsLayer renders a fullscreen card as an inset full-frame
 *     wrapper with cover-cropped media; fullscreen card videos preload="auto".
 *  4. Asset load failure (routine — signed URLs expire in 24h): onError swaps
 *     the media for a dashed-zinc "This visual couldn't load" tile with a
 *     Remove button (full-frame for fullscreen, pip-sized for pip), lifts the
 *     card id via onCardMediaError, and Remove calls onRemoveCard.
 */

import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import LiveOverlayCardsLayer from "@/app/plan/_components/LiveOverlayCardsLayer";
import { mediaClassFor } from "@/app/plan/_components/cardMedia";
import { overlayCardStyle } from "@/app/plan/_components/overlayCardStyle";
import type { MediaOverlay } from "@/lib/plan-api";

// jsdom doesn't implement media playback — stub the methods the sync code calls.
beforeAll(() => {
  window.HTMLMediaElement.prototype.load = jest.fn();
  window.HTMLMediaElement.prototype.pause = jest.fn();
  window.HTMLMediaElement.prototype.play = jest.fn().mockResolvedValue(undefined);
});

function makeCard(overrides: Partial<MediaOverlay> = {}): MediaOverlay {
  return {
    id: "card-a",
    kind: "image",
    src_gcs_path: "users/u1/plan/item1/overlays/a.png",
    preview_url: "https://signed/a.png",
    position: "center",
    x_frac: 0.5,
    y_frac: 0.5,
    scale: 0.35,
    start_s: 0,
    end_s: 5,
    z: 0,
    ...overrides,
  };
}

const mainRef = { current: null };

function renderLayer(
  cards: MediaOverlay[],
  extra: {
    onCardMediaError?: (cardId: string) => void;
    onRemoveCard?: (cardId: string) => void;
  } = {},
) {
  return render(
    <LiveOverlayCardsLayer
      cards={cards}
      resolveCardSrc={(c) => c.preview_url ?? undefined}
      videoTimeS={1}
      timeGate
      mainVideoRef={mainRef}
      {...extra}
    />,
  );
}

// ── 1. Parity guard (ts half) ────────────────────────────────────────────────

describe("overlayCardStyle — display_mode branching", () => {
  it("parity: style util branches on display_mode like the ffmpeg builder", () => {
    // Fullscreen → full-frame inset (the CSS mirror of the builder's
    // scale=1080:1920:force_original_aspect_ratio=increase,crop + overlay=0:0).
    // No translate, no width% — x/y/scale are ignored but preserved upstream.
    expect(
      overlayCardStyle({ x_frac: 0.25, y_frac: 0.75, scale: 0.4, display_mode: "fullscreen" }),
    ).toEqual({
      position: "absolute",
      left: 0,
      top: 0,
      right: 0,
      bottom: 0,
    });

    // Pip (explicit) → today's percent math, byte-identical (fit-width branch).
    const pip = {
      position: "absolute",
      left: "25%",
      top: "75%",
      transform: "translate(-50%, -50%)",
      width: "40%",
    };
    expect(
      overlayCardStyle({ x_frac: 0.25, y_frac: 0.75, scale: 0.4, display_mode: "pip" }),
    ).toEqual(pip);

    // Absent display_mode (legacy envelope) → same pip math (coercion parity
    // with the server's coercing validator).
    expect(overlayCardStyle({ x_frac: 0.25, y_frac: 0.75, scale: 0.4 })).toEqual(pip);
  });
});

// ── 2. mediaClassFor ─────────────────────────────────────────────────────────

describe("mediaClassFor", () => {
  it("pip (and absent) keeps today's fit-width rounded media classes", () => {
    expect(mediaClassFor("pip")).toBe("w-full h-auto rounded");
    expect(mediaClassFor(undefined)).toBe("w-full h-auto rounded");
  });

  it("fullscreen is cover-crop with zero chrome (no rounded corners, no shadow)", () => {
    const cls = mediaClassFor("fullscreen");
    expect(cls).toBe("w-full h-full object-cover");
    expect(cls).not.toMatch(/rounded/);
    expect(cls).not.toMatch(/shadow/);
  });
});

// ── 3. LiveOverlayCardsLayer — fullscreen rendering ──────────────────────────

describe("LiveOverlayCardsLayer — fullscreen cards", () => {
  it("renders a fullscreen image card as inset full-frame with object-cover, no rounded", () => {
    renderLayer([makeCard({ id: "fs1", display_mode: "fullscreen" })]);

    const wrap = document.querySelector<HTMLElement>('[data-overlay-card="fs1"]')!;
    expect(wrap).toHaveStyle({
      position: "absolute",
      left: "0px",
      top: "0px",
      right: "0px",
      bottom: "0px",
      pointerEvents: "none",
    });
    // No pip positioning leftovers.
    expect(wrap.style.transform).toBe("");
    expect(wrap.style.width).toBe("");

    const img = wrap.querySelector("img")!;
    expect(img).toHaveClass("w-full", "h-full", "object-cover");
    expect(img.className).not.toMatch(/rounded/);
    expect(img.className).not.toMatch(/shadow/);
  });

  it("pip cards keep the percent math and rounded media (regression)", () => {
    renderLayer([makeCard({ id: "pip1" })]);
    const wrap = document.querySelector<HTMLElement>('[data-overlay-card="pip1"]')!;
    expect(wrap).toHaveStyle({ left: "50%", top: "50%", width: "35%" });
    expect(wrap.querySelector("img")).toHaveClass("w-full", "h-auto", "rounded");
  });

  it("fullscreen card videos preload='auto'; pip card videos do not", () => {
    renderLayer([
      makeCard({
        id: "fsv",
        kind: "video",
        display_mode: "fullscreen",
        preview_url: "https://signed/fs.mp4",
      }),
      makeCard({ id: "pipv", kind: "video", preview_url: "https://signed/pip.mp4" }),
    ]);
    const fsVideo = document.querySelector<HTMLVideoElement>('[data-overlay-card="fsv"] video')!;
    expect(fsVideo).toHaveAttribute("preload", "auto");
    expect(fsVideo).toHaveClass("w-full", "h-full", "object-cover");

    const pipVideo = document.querySelector<HTMLVideoElement>(
      '[data-overlay-card="pipv"] video',
    )!;
    expect(pipVideo).not.toHaveAttribute("preload");
    expect(pipVideo).toHaveClass("w-full", "h-auto", "rounded");
  });
});

// ── 4. Asset load failure ────────────────────────────────────────────────────

describe("LiveOverlayCardsLayer — media load failure (expired signed URLs)", () => {
  it("image onError → full-frame dashed tile + Remove; lifts onCardMediaError; Remove calls onRemoveCard", () => {
    const onCardMediaError = jest.fn();
    const onRemoveCard = jest.fn();
    renderLayer([makeCard({ id: "fs-err", display_mode: "fullscreen" })], {
      onCardMediaError,
      onRemoveCard,
    });

    fireEvent.error(document.querySelector('[data-overlay-card="fs-err"] img')!);

    expect(onCardMediaError).toHaveBeenCalledWith("fs-err");
    const tile = screen.getByTestId("overlay-card-failed-fs-err");
    expect(tile).toHaveTextContent("This visual couldn't load");
    // Full-frame at fullscreen size (the wrapper is already inset-0).
    expect(tile).toHaveClass("h-full", "w-full", "border-dashed");
    // The broken media element is swapped out entirely.
    expect(document.querySelector('[data-overlay-card="fs-err"] img')).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(onRemoveCard).toHaveBeenCalledWith("fs-err");
  });

  it("video onError → same tile; failed pip cards render the tile at pip size", () => {
    const onCardMediaError = jest.fn();
    renderLayer(
      [makeCard({ id: "pip-err", kind: "video", preview_url: "https://signed/pip.mp4" })],
      { onCardMediaError, onRemoveCard: jest.fn() },
    );

    fireEvent.error(document.querySelector('[data-overlay-card="pip-err"] video')!);

    expect(onCardMediaError).toHaveBeenCalledWith("pip-err");
    const tile = screen.getByTestId("overlay-card-failed-pip-err");
    // Pip-sized tile: the wrapper keeps the percent math; the tile fills it.
    expect(tile).toHaveClass("aspect-video", "w-full");
    expect(
      document.querySelector<HTMLElement>('[data-overlay-card="pip-err"]'),
    ).toHaveStyle({ left: "50%", width: "35%" });
    expect(screen.getByRole("button", { name: "Remove" })).toBeInTheDocument();
  });

  it("without onRemoveCard the tile still renders (no Remove button)", () => {
    renderLayer([makeCard({ id: "no-remove" })], { onCardMediaError: jest.fn() });
    fireEvent.error(document.querySelector('[data-overlay-card="no-remove"] img')!);
    expect(screen.getByTestId("overlay-card-failed-no-remove")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();
  });

  it("onCardMediaError fires once per card, not per repeated error event", () => {
    const onCardMediaError = jest.fn();
    renderLayer([makeCard({ id: "once" })], { onCardMediaError });
    const img = document.querySelector('[data-overlay-card="once"] img')!;
    fireEvent.error(img);
    // Tile replaced the img; a second error on a stale node must not re-lift.
    fireEvent.error(img);
    expect(onCardMediaError).toHaveBeenCalledTimes(1);
  });
});
