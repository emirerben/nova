import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, jest } from "@jest/globals";
import {
  shouldRouteSfxThroughWebAudio,
  useSfxPreview,
} from "@/app/plan/_components/useSfxPreview";
import type { SoundEffectPlacement } from "@/lib/plan-api";

// jsdom doesn't implement media playback. Stub the methods useSfxPreview calls on
// each <audio> element it creates via `new Audio()`.
beforeAll(() => {
  window.HTMLMediaElement.prototype.load = jest.fn<() => void>();
  window.HTMLMediaElement.prototype.pause = jest.fn<() => void>();
  window.HTMLMediaElement.prototype.play = jest.fn<() => Promise<void>>().mockResolvedValue(undefined);
});

type Listener = () => void;

// Minimal fake <video>: we drive currentTime/paused by hand and fire the events
// useSfxPreview listens for. A real looping <video> fires NO `ended`/`seeked` on
// wrap (Chrome), so this models the playhead jumping backward via `timeupdate`.
function makeFakeVideo() {
  const listeners: Record<string, Listener[]> = {};
  return {
    currentTime: 0,
    paused: false,
    duration: 12,
    addEventListener: (ev: string, cb: Listener) => {
      (listeners[ev] ||= []).push(cb);
    },
    removeEventListener: (ev: string, cb: Listener) => {
      listeners[ev] = (listeners[ev] || []).filter((f) => f !== cb);
    },
    dispatch(ev: string) {
      (listeners[ev] || []).forEach((f) => f());
    },
  };
}

const PLACEMENT = {
  id: "p1",
  src_gcs_path: "sound-effects/fah/audio.mp3",
  at_s: 2,
  gain: 1,
  duration_s: 5,
} as SoundEffectPlacement;
const AUDIO_URLS = { "sound-effects/fah/audio.mp3": "blob:fake-audio" };

describe("useSfxPreview — loop re-arm", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    (window.HTMLMediaElement.prototype.play as jest.MockedFunction<() => Promise<void>>).mockClear();
  });
  afterEach(() => {
    jest.useRealTimers();
  });

  // Regression: LiveEditPreview's preview video has `loop`. A native loop wrap
  // fires no `ended`/`seeked`, so before the fix the one-shot SFX timers were
  // never re-armed — effects played on pass 1 only, then silent on every loop.
  it("re-schedules SFX on a native loop wrap (backward time jump), not just the first pass", () => {
    const video = makeFakeVideo();
    const videoRef = { current: video as unknown as HTMLVideoElement };
    const play = window.HTMLMediaElement.prototype.play as jest.MockedFunction<() => Promise<void>>;

    renderHook(() => useSfxPreview(videoRef, [PLACEMENT], AUDIO_URLS));

    // Pass 1: mount syncs at currentTime 0 → schedules the effect at at_s=2. Fire it.
    act(() => {
      jest.advanceTimersByTime(2000);
    });
    expect(play).toHaveBeenCalledTimes(1);

    // Playback progresses, then the video loops back to ~0 (no ended/seeked fired).
    act(() => {
      video.currentTime = 4;
      video.dispatch("timeupdate"); // forward — must NOT re-arm
      video.currentTime = 0.1;
      video.dispatch("timeupdate"); // backward jump = loop wrap → re-arm
    });

    // Pass 2: the re-armed timer fires ~1.9s into the new loop.
    act(() => {
      jest.advanceTimersByTime(2000);
    });
    expect(play).toHaveBeenCalledTimes(2);
  });

  it("does not route signed cross-origin SFX through Web Audio", () => {
    expect(shouldRouteSfxThroughWebAudio("blob:fake-audio")).toBe(true);
    expect(shouldRouteSfxThroughWebAudio("/api/sfx/local-preview")).toBe(true);
    expect(
      shouldRouteSfxThroughWebAudio(
        "https://storage.googleapis.com/nova-videos-dev/sound-effects/pop/audio.wav?sig=1",
      ),
    ).toBe(false);
  });

  it("uses multiple native audio elements for cross-origin gain above 1", () => {
    const video = makeFakeVideo();
    const videoRef = { current: video as unknown as HTMLVideoElement };
    const play = window.HTMLMediaElement.prototype.play as jest.MockedFunction<() => Promise<void>>;

    renderHook(() =>
      useSfxPreview(
        videoRef,
        [{ ...PLACEMENT, gain: 2.4 }],
        {
          "sound-effects/fah/audio.mp3":
            "https://storage.googleapis.com/nova-videos-dev/sound-effects/fah/audio.wav?sig=1",
        },
      ),
    );

    act(() => {
      jest.advanceTimersByTime(2000);
    });

    expect(play).toHaveBeenCalledTimes(3);
  });

  it("uses the composed output clock once for virtual previews, not source-deck time", () => {
    const video = makeFakeVideo();
    video.currentTime = 0;
    const videoRef = { current: video as unknown as HTMLVideoElement };
    const listeners = new Set<() => void>();
    let outputTime = 0;
    const clock = {
      getSnapshot: () => outputTime,
      subscribe: (listener: () => void) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      publish: (next: number) => {
        outputTime = next;
        listeners.forEach((listener) => listener());
      },
    };
    const play = window.HTMLMediaElement.prototype.play as jest.MockedFunction<() => Promise<void>>;

    renderHook(() => useSfxPreview(videoRef, [PLACEMENT], AUDIO_URLS, { clock, playing: true }));

    act(() => clock.publish(2));
    expect(play).toHaveBeenCalledTimes(1);

    // Additional frame samples and a deck-local media time change must not
    // restart the already-playing output-clock placement.
    act(() => {
      video.currentTime = 0.1;
      clock.publish(2.1);
      clock.publish(2.2);
    });
    expect(play).toHaveBeenCalledTimes(1);
  });

  it("re-synchronizes an active virtual-preview SFX after a short scrub", () => {
    const video = makeFakeVideo();
    const videoRef = { current: video as unknown as HTMLVideoElement };
    const listeners = new Set<() => void>();
    let outputTime = 0;
    const clock = {
      getSnapshot: () => outputTime,
      subscribe: (listener: () => void) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      publish: (next: number) => {
        outputTime = next;
        listeners.forEach((listener) => listener());
      },
    };
    const play = window.HTMLMediaElement.prototype.play as jest.MockedFunction<
      () => Promise<void>
    >;

    renderHook(() => useSfxPreview(videoRef, [PLACEMENT], AUDIO_URLS, { clock, playing: true }));

    act(() => clock.publish(2));
    expect(play).toHaveBeenCalledTimes(1);

    // A 200ms scrub used to sit below the old 500ms jump threshold, leaving
    // the sound at its pre-seek offset. Re-arming starts it at the new offset.
    act(() => clock.publish(2.2));
    expect(play).toHaveBeenCalledTimes(2);
  });
});
