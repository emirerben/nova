/**
 * useCardVideoSync — drives one card's `<video>` off an explicit
 * `{active, timeS}` target plus the editor transport's real `isPlaying`
 * flag (see the hook's docstring). No wall-clock inference: `isPlaying`
 * comes straight from EditorCanvas's own `playing` prop.
 */
import React from "react";
import { render } from "@testing-library/react";
import "@testing-library/jest-dom";

import {
  useCardVideoSync,
  type CardVideoTarget,
} from "@/app/plan/items/[id]/_editor/carousel-preview-impl/video-sync";

// jsdom doesn't implement media playback (play()/pause() throw "not
// implemented" by default) — track paused state ourselves, same pattern as
// fullscreen-cutaway-preview.test.tsx's HTMLMediaElement stubs.
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

function Harness({ target, isPlaying }: { target: CardVideoTarget; isPlaying: boolean }) {
  const ref = React.useRef<HTMLVideoElement | null>(null);
  useCardVideoSync(ref, target, isPlaying);
  return <video data-testid="v" ref={ref} />;
}

beforeEach(() => {
  jest.restoreAllMocks();
  // play()/pause() are assigned once on the shared HTMLMediaElement.prototype
  // (not per-test spies), so their call history persists across tests unless
  // explicitly cleared here — restoreAllMocks() only affects jest.spyOn spies.
  (window.HTMLMediaElement.prototype.play as jest.Mock).mockClear();
  (window.HTMLMediaElement.prototype.pause as jest.Mock).mockClear();
});

describe("useCardVideoSync", () => {
  it("active + isPlaying: plays and seeks to the initial target precisely", () => {
    const { getByTestId } = render(
      <Harness target={{ active: true, timeS: 3 }} isPlaying />,
    );
    const video = getByTestId("v") as HTMLVideoElement;
    expect(video.play).toHaveBeenCalledTimes(1);
    expect(video.currentTime).toBe(3);
    expect(video.pause).not.toHaveBeenCalled();
  });

  it("active + !isPlaying (paused transport): pauses and seeks precisely to timeS", () => {
    const { getByTestId } = render(
      <Harness target={{ active: true, timeS: 1.5 }} isPlaying={false} />,
    );
    const video = getByTestId("v") as HTMLVideoElement;
    expect(video.pause).toHaveBeenCalled();
    expect(video.currentTime).toBe(1.5);
    expect(video.play).not.toHaveBeenCalled();
  });

  it("inactive target: pauses and holds at timeS regardless of isPlaying", () => {
    const { getByTestId } = render(
      <Harness target={{ active: false, timeS: 1.5 }} isPlaying />,
    );
    const video = getByTestId("v") as HTMLVideoElement;
    expect(video.pause).toHaveBeenCalled();
    expect(video.currentTime).toBe(1.5);
    expect(video.play).not.toHaveBeenCalled();
  });

  it("paused/inactive target skips a no-op seek within SEEK_EPS_S (avoids seek churn)", () => {
    const { getByTestId, rerender } = render(
      <Harness target={{ active: false, timeS: 1.0 }} isPlaying={false} />,
    );
    const video = getByTestId("v") as HTMLVideoElement;
    video.currentTime = 1.0;
    const setter = jest.spyOn(video, "currentTime", "set");
    rerender(<Harness target={{ active: false, timeS: 1.01 }} isPlaying={false} />); // within 0.03s epsilon
    expect(setter).not.toHaveBeenCalled();
  });

  it("active + isPlaying, video already at the target: no forced re-seek (left running natively)", () => {
    const { getByTestId, rerender } = render(
      <Harness target={{ active: true, timeS: 2.0 }} isPlaying />,
    );
    const video = getByTestId("v") as HTMLVideoElement;
    // Simulate the video's OWN clock having advanced on its own, in sync
    // with the new target (native playback, not driven by us).
    video.currentTime = 2.5;
    const setter = jest.spyOn(video, "currentTime", "set");
    (video.play as jest.Mock).mockClear();

    rerender(<Harness target={{ active: true, timeS: 2.5 }} isPlaying />);

    expect(setter).not.toHaveBeenCalled(); // no drift -> no forced seek
  });

  it("small drift while playing is corrected without a pause (drift-only, not a full seek cycle)", () => {
    const { getByTestId, rerender } = render(
      <Harness target={{ active: true, timeS: 2.0 }} isPlaying />,
    );
    const video = getByTestId("v") as HTMLVideoElement;
    video.currentTime = 2.0;
    (video.pause as jest.Mock).mockClear();

    // Video actually drifted to 2.3 instead of the expected 2.7 (> 0.15s off).
    video.currentTime = 2.3;
    rerender(<Harness target={{ active: true, timeS: 2.7 }} isPlaying />);

    expect(video.currentTime).toBe(2.7); // corrected
    expect(video.pause).not.toHaveBeenCalled(); // still playing, not paused
  });

  it("a scrub while paused (transport isPlaying=false) always pauses + seeks precisely, any distance", () => {
    const { getByTestId, rerender } = render(
      <Harness target={{ active: true, timeS: 2.0 }} isPlaying={false} />,
    );
    const video = getByTestId("v") as HTMLVideoElement;
    video.currentTime = 2.0;

    rerender(<Harness target={{ active: true, timeS: 9.0 }} isPlaying={false} />);

    expect(video.pause).toHaveBeenCalled();
    expect(video.currentTime).toBe(9.0);
  });

  it("transport pausing mid-playback (isPlaying flips true -> false) immediately pauses and holds the current target", () => {
    const { getByTestId, rerender } = render(
      <Harness target={{ active: true, timeS: 5.0 }} isPlaying />,
    );
    const video = getByTestId("v") as HTMLVideoElement;
    video.currentTime = 5.0;

    rerender(<Harness target={{ active: true, timeS: 5.0 }} isPlaying={false} />);

    expect(video.pause).toHaveBeenCalled();
  });

  it("switching from active to inactive pauses and seeks to the new (reset) timeS", () => {
    const { getByTestId, rerender } = render(
      <Harness target={{ active: true, timeS: 1.0 }} isPlaying />,
    );
    const video = getByTestId("v") as HTMLVideoElement;
    video.currentTime = 1.0;

    rerender(<Harness target={{ active: false, timeS: 0 }} isPlaying />);
    expect(video.pause).toHaveBeenCalled();
    expect(video.currentTime).toBe(0);
  });
});
