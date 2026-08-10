/**
 * Component-level integration test for CarouselBlockPreviewImpl: card count/
 * effect/mode wiring, video src assignment, rolling-vs-focus video-target
 * routing, and out-of-range time clamping. The per-frame CSS pose math and
 * the video play/pause/seek heuristic each have their own dedicated,
 * dependency-free unit tests (card-style.test.ts, video-sync.test.tsx) —
 * this file only checks the parts that require actually mounting the
 * component tree.
 */
import React from "react";
import { render } from "@testing-library/react";
import "@testing-library/jest-dom";

import type { CarouselMoment, TimelineClip } from "@/lib/generative-api";
import { CANVAS_H, CANVAS_W } from "@/lib/carousel-preview";
import CarouselBlockPreviewImpl from "@/app/plan/items/[id]/_editor/carousel-preview-impl/CarouselBlockPreviewImpl";

const pausedState = new WeakMap<HTMLMediaElement, boolean>();

beforeAll(() => {
  Object.defineProperty(window.HTMLMediaElement.prototype, "paused", {
    get(this: HTMLMediaElement) {
      return pausedState.get(this) ?? true;
    },
    configurable: true,
  });
  window.HTMLMediaElement.prototype.play = jest.fn(function (this: HTMLMediaElement) {
    pausedState.set(this, false);
    return Promise.resolve();
  }) as unknown as HTMLMediaElement["play"];
  window.HTMLMediaElement.prototype.pause = jest.fn(function (this: HTMLMediaElement) {
    pausedState.set(this, true);
  });
});

beforeEach(() => {
  jest.spyOn(performance, "now").mockReturnValue(0);
  // play()/pause() are assigned once on the shared HTMLMediaElement.prototype
  // (not per-test spies), so their call history persists across tests unless
  // explicitly cleared here — restoreAllMocks() only affects jest.spyOn spies.
  (window.HTMLMediaElement.prototype.play as jest.Mock).mockClear();
  (window.HTMLMediaElement.prototype.pause as jest.Mock).mockClear();
});

afterEach(() => {
  jest.restoreAllMocks();
});

function makeClips(n: number): Pick<TimelineClip, "clip_index" | "signed_url">[] {
  return Array.from({ length: n }, (_, i) => ({
    clip_index: i,
    signed_url: `https://signed/clip-${i}.mp4`,
  }));
}

describe("CarouselBlockPreviewImpl", () => {
  it("renders a root box sized exactly CANVAS_W x CANVAS_H and tags effect/mode as data attributes", () => {
    const config: CarouselMoment = { effect: "flipbook", mode: "rolling", duration_s: 4 };
    const { container } = render(
      <CarouselBlockPreviewImpl
        config={config}
        clips={makeClips(3)}
        currentTimeS={1}
        blockStartS={0}
        durationS={4}
        isPlaying={false}
      />,
    );
    const root = container.querySelector('[data-carousel-preview="true"]') as HTMLElement;
    expect(root).toBeInTheDocument();
    expect(root.style.width).toBe(`${CANVAS_W}px`);
    expect(root.style.height).toBe(`${CANVAS_H}px`);
    expect(root.getAttribute("data-carousel-effect")).toBe("flipbook");
    expect(root.getAttribute("data-carousel-mode")).toBe("rolling");
  });

  it("renders exactly one card per clip, capped at MAX_CARDS (5), array order = card order", () => {
    const config: CarouselMoment = { mode: "rolling", duration_s: 3 };
    const { container } = render(
      <CarouselBlockPreviewImpl
        config={config}
        clips={makeClips(8)}
        currentTimeS={0}
        blockStartS={0}
        durationS={3}
        isPlaying={false}
      />,
    );
    const cards = container.querySelectorAll("[data-carousel-card-index]");
    expect(cards).toHaveLength(5);
    const videos = container.querySelectorAll("video");
    expect(videos).toHaveLength(5);
    expect(videos[0]).toHaveAttribute("src", "https://signed/clip-0.mp4");
    expect(videos[4]).toHaveAttribute("src", "https://signed/clip-4.mp4");
  });

  it("defaults to effect=scale_sweep and mode=focus when config omits them (mirrors CarouselPanel's prefill defaults)", () => {
    const { container } = render(
      <CarouselBlockPreviewImpl
        config={{}}
        clips={makeClips(3)}
        currentTimeS={0}
        blockStartS={0}
        durationS={4}
        isPlaying={false}
      />,
    );
    const root = container.querySelector('[data-carousel-preview="true"]') as HTMLElement;
    expect(root.getAttribute("data-carousel-effect")).toBe("scale_sweep");
    expect(root.getAttribute("data-carousel-mode")).toBe("focus");
  });

  it("renders a graceful empty root when there are no clips, without crashing", () => {
    const { container } = render(
      <CarouselBlockPreviewImpl
        config={{ mode: "rolling" }}
        clips={[]}
        currentTimeS={0}
        blockStartS={0}
        durationS={4}
        isPlaying={false}
      />,
    );
    const empty = container.querySelector('[data-carousel-preview-empty="true"]') as HTMLElement;
    expect(empty).toBeInTheDocument();
    expect(empty.style.width).toBe(`${CANVAS_W}px`);
    expect(container.querySelector("video")).toBeNull();
  });

  it("rolling mode: every card's video target is block-local time, regardless of card index", () => {
    const config: CarouselMoment = { mode: "rolling", duration_s: 3 };
    const { container } = render(
      <CarouselBlockPreviewImpl
        config={config}
        clips={makeClips(2)}
        currentTimeS={11.5} // blockStartS 10 -> local time 1.5
        blockStartS={10}
        durationS={3}
        isPlaying
      />,
    );
    const videos = Array.from(container.querySelectorAll("video")) as HTMLVideoElement[];
    expect(videos).toHaveLength(2);
    for (const v of videos) {
      expect(v.currentTime).toBeCloseTo(1.5, 5);
    }
  });

  it("focus mode: the non-focused card is held/paused; the focused card's target is local time SINCE focus began", () => {
    // A 2-card focus choreography's natural length (lead-in + flick converge
    // + settle + zoom-in + hold + zoom-out + settle + trailing flick) runs
    // to ~12s — durationS is set to the CarouselMoment contract's max (15s)
    // so the fitted (see geometry.ts's documented duration-fit divergence)
    // window actually reaches the focus phase instead of truncating before it.
    const config: CarouselMoment = { mode: "focus", focus_clip_index: 1, duration_s: 15 };
    const { container, rerender } = render(
      <CarouselBlockPreviewImpl
        config={config}
        clips={makeClips(2)}
        currentTimeS={0}
        blockStartS={0}
        durationS={15}
        isPlaying={false}
      />,
    );

    // Scan forward until card 1 is reported focused, then assert routing.
    let focusedTimeS: number | null = null;
    for (let t = 0; t <= 15; t += 1 / 30) {
      rerender(
        <CarouselBlockPreviewImpl
          config={config}
          clips={makeClips(2)}
          currentTimeS={t}
          blockStartS={0}
          durationS={15}
          isPlaying={false}
        />,
      );
      const focusedCard = container.querySelector('[data-carousel-card-focused="true"]');
      if (focusedCard) {
        focusedTimeS = t;
        break;
      }
    }
    expect(focusedTimeS).not.toBeNull();

    const cardEls = container.querySelectorAll("[data-carousel-card-index]");
    const focusedEl = container.querySelector('[data-carousel-card-focused="true"]') as HTMLElement;
    const nonFocusedEl = Array.from(cardEls).find((el) => el !== focusedEl) as HTMLElement;

    expect(focusedEl.getAttribute("data-carousel-card-index")).toBe("1");
    const focusedVideo = focusedEl.querySelector("video") as HTMLVideoElement;
    const nonFocusedVideo = nonFocusedEl.querySelector("video") as HTMLVideoElement;

    // Non-focused card is held at 0 (paused frame).
    expect(nonFocusedVideo.currentTime).toBe(0);
    // Focused card's local time is small (just past the focus transition),
    // NOT equal to the block-local time (which is >= leadInS by now).
    expect(focusedVideo.currentTime).toBeGreaterThanOrEqual(0);
    expect(focusedVideo.currentTime).toBeLessThan(0.2);
  });

  it("maps authored source clip indices onto the active preview card order", () => {
    const clips = [makeClips(3)[2], makeClips(3)[0]];
    const config: CarouselMoment = {
      mode: "focus",
      timing_model: "ripple_v1",
      duration_s: 2.2,
      sequence: [
        { clip_index: 0, hold_s: 0.5 },
        { clip_index: 2, hold_s: 0.5 },
      ],
      move_duration_s: 0.2,
      zoom_duration_s: 0.2,
    };
    const { container, rerender } = render(
      <CarouselBlockPreviewImpl
        config={config}
        clips={clips}
        currentTimeS={0}
        blockStartS={0}
        durationS={2.2}
        isPlaying={false}
      />,
    );

    let firstFocused: HTMLElement | null = null;
    for (let t = 0; t <= 2.2; t += 1 / 30) {
      rerender(
        <CarouselBlockPreviewImpl
          config={config}
          clips={clips}
          currentTimeS={t}
          blockStartS={0}
          durationS={2.2}
          isPlaying={false}
        />,
      );
      firstFocused = container.querySelector('[data-carousel-card-focused="true"]');
      if (firstFocused) break;
    }

    expect(firstFocused).not.toBeNull();
    expect(firstFocused).toHaveAttribute("data-carousel-card-index", "1");
    expect(firstFocused?.querySelector("video")).toHaveAttribute(
      "src",
      "https://signed/clip-0.mp4",
    );
  });

  it("clamps currentTimeS outside [blockStartS, blockStartS + durationS] to the block's own [0, durationS] range", () => {
    const config: CarouselMoment = { mode: "rolling", duration_s: 3 };
    const { container: before } = render(
      <CarouselBlockPreviewImpl
        config={config}
        clips={makeClips(2)}
        currentTimeS={-50} // way before the block starts
        blockStartS={10}
        durationS={3}
        isPlaying={false}
      />,
    );
    const beforeVideo = before.querySelector("video") as HTMLVideoElement;
    expect(beforeVideo.currentTime).toBe(0);

    const { container: after } = render(
      <CarouselBlockPreviewImpl
        config={config}
        clips={makeClips(2)}
        currentTimeS={999} // way past the block's end
        blockStartS={10}
        durationS={3}
        isPlaying={false}
      />,
    );
    const afterVideo = after.querySelector("video") as HTMLVideoElement;
    expect(afterVideo.currentTime).toBeCloseTo(3, 5);
  });

  it("isPlaying threads straight through to every active card's video-sync: true calls play(), false never does", () => {
    const config: CarouselMoment = { mode: "rolling", duration_s: 3 };

    const { container: playing } = render(
      <CarouselBlockPreviewImpl
        config={config}
        clips={makeClips(2)}
        currentTimeS={1}
        blockStartS={0}
        durationS={3}
        isPlaying
      />,
    );
    for (const v of Array.from(playing.querySelectorAll("video")) as HTMLVideoElement[]) {
      expect(v.play).toHaveBeenCalled();
      expect(v.pause).not.toHaveBeenCalled();
    }

    (window.HTMLMediaElement.prototype.play as jest.Mock).mockClear();
    (window.HTMLMediaElement.prototype.pause as jest.Mock).mockClear();

    const { container: paused } = render(
      <CarouselBlockPreviewImpl
        config={config}
        clips={makeClips(2)}
        currentTimeS={1}
        blockStartS={0}
        durationS={3}
        isPlaying={false}
      />,
    );
    for (const v of Array.from(paused.querySelectorAll("video")) as HTMLVideoElement[]) {
      expect(v.pause).toHaveBeenCalled();
      expect(v.play).not.toHaveBeenCalled();
    }
  });
});
