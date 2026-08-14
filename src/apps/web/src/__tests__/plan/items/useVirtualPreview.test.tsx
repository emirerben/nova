import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, jest } from "@jest/globals";

import type { DraftSlot } from "@/app/generative/timeline-math";
import {
  mapDeckMediaTimeToVirtualTime,
  useVirtualPreview,
} from "@/app/plan/items/[id]/_editor/useVirtualPreview";
import type { VirtualCarouselSplice } from "@/app/plan/items/[id]/_editor/virtual-timeline";

const SLOT: DraftSlot = {
  key: "slot-1",
  slotId: "slot-1",
  clipIndex: 0,
  inS: 1.4,
  durationBeats: null,
  durationS: 2,
  removed: false,
  momentDescription: null,
};

const SLOT_2: DraftSlot = {
  key: "slot-2",
  slotId: "slot-2",
  clipIndex: 1,
  inS: 0.5,
  durationBeats: null,
  durationS: 2,
  removed: false,
  momentDescription: null,
};

const DEFAULT_CLIPS = [
  { clip_index: 0, signed_url: "https://cdn.example.test/clip.mp4" },
  { clip_index: 1, signed_url: "https://cdn.example.test/clip2.mp4" },
];

// Stable slot arrays — an inline array prop rebuilds the timeline every
// render, re-firing the hook's timeline effect (production slots are stable
// state). Tests that WANT a timeline change pass a different constant.
const ONE_SLOT = [SLOT];
const EMPTY_GRID: number[] = [];
const TWO_SLOTS = [SLOT, SLOT_2];
const TRANSITION_SLOTS: DraftSlot[] = [
  { ...SLOT, transitionAfter: "crossfade", transitionDurationS: 0.3 },
  SLOT_2,
];

// Stable callbacks — inline jest.fn() props change identity every render,
// which re-fires the hook's timeline effect and skews play/pause counts
// (production passes stable useCallback/setState handlers).
const NOOP_TIME_UPDATE = () => {};
const NOOP_DURATION = () => {};
const NOOP_SOURCE_ERROR = () => {};

// Stable "no carousel" default — same rationale as the stable slot arrays
// above: a fresh `{...}` literal per render would (pre-fix) rebuild the
// timeline every render, which is exactly the EditorShell bug this file's
// carousel-clock tests sit next to. `null` was always stable.
const NO_CAROUSEL = null;

function Harness({
  onPlayingChange,
  soundMuted = false,
  videoMuted = false,
  musicTrackActive = false,
  musicAudioUrl = "https://cdn.example.test/music.m4a",
  onMusicError,
  onTimeUpdate = NOOP_TIME_UPDATE,
  slots = ONE_SLOT,
  carousel = NO_CAROUSEL,
  frameDriven = false,
  onFrameTimeUpdate,
}: {
  onPlayingChange: (playing: boolean) => void;
  soundMuted?: boolean;
  videoMuted?: boolean;
  musicTrackActive?: boolean;
  musicAudioUrl?: string | null;
  onMusicError?: () => void;
  onTimeUpdate?: (timeS: number) => void;
  slots?: DraftSlot[];
  carousel?: VirtualCarouselSplice | null;
  frameDriven?: boolean;
  onFrameTimeUpdate?: (timeS: number) => void;
}) {
  const preview = useVirtualPreview({
    enabled: true,
    slots,
    clips: DEFAULT_CLIPS,
    grid: EMPTY_GRID,
    carousel,
    currentTime: 0,
    muted: videoMuted,
    musicAudioUrl,
    musicStartS: 55.71,
    soundMuted,
    musicTrackActive,
    frameDriven,
    onFrameTimeUpdate,
    onTimeUpdate,
    onDuration: NOOP_DURATION,
    onPlayingChange,
    onSourceError: NOOP_SOURCE_ERROR,
    onMusicError,
  });
  const { ref: videoARef, ...videoAProps } = preview.videoAProps;
  const { ref: videoBRef, ...videoBProps } = preview.videoBProps;
  const music = preview.musicAudioProps;

  return (
    <>
      <video data-testid="deck-a" ref={videoARef} {...videoAProps} />
      <video data-testid="deck-b" ref={videoBRef} {...videoBProps} />
      {music ? (
        (() => {
          const { ref: audioRef, ...audioProps } = music;
          return <audio data-testid="music" ref={audioRef} {...audioProps} />;
        })()
      ) : null}
      <button type="button" onClick={preview.play}>
        play
      </button>
      <button type="button" onClick={preview.pause}>
        pause
      </button>
      <button type="button" onClick={preview.toggle}>
        toggle
      </button>
    </>
  );
}

describe("frame-driven output clock", () => {
  let callbacks: Map<HTMLVideoElement, VideoFrameRequestCallback[]>;
  let requestSpy: jest.SpiedFunction<HTMLVideoElement["requestVideoFrameCallback"]>;

  beforeEach(() => {
    callbacks = new Map();
    jest.spyOn(window.HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    jest.spyOn(window.HTMLMediaElement.prototype, "play").mockImplementation(() => Promise.resolve());
    jest.spyOn(window.HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    Object.defineProperty(window.HTMLVideoElement.prototype, "requestVideoFrameCallback", {
      configurable: true,
      writable: true,
      value: function requestVideoFrameCallback(this: HTMLVideoElement, callback: VideoFrameRequestCallback) {
        callbacks.set(this, [...(callbacks.get(this) ?? []), callback]);
        return callbacks.get(this)?.length ?? 0;
      },
    });
    Object.defineProperty(window.HTMLVideoElement.prototype, "cancelVideoFrameCallback", {
      configurable: true,
      writable: true,
      value: jest.fn(),
    });
    requestSpy = jest.spyOn(window.HTMLVideoElement.prototype, "requestVideoFrameCallback");
  });

  afterEach(() => {
    jest.restoreAllMocks();
    delete (window.HTMLVideoElement.prototype as Partial<HTMLVideoElement>)
      .requestVideoFrameCallback;
    delete (window.HTMLVideoElement.prototype as Partial<HTMLVideoElement>)
      .cancelVideoFrameCallback;
  });

  it("maps decoded source media time onto the canonical output timeline", () => {
    expect(mapDeckMediaTimeToVirtualTime({ startS: 4, durationS: 2, inS: 1.4 }, 2.15))
      .toBeCloseTo(4.75, 8);
    expect(mapDeckMediaTimeToVirtualTime({ startS: 4, durationS: 2, inS: 1.4 }, 0))
      .toBe(4);
    expect(mapDeckMediaTimeToVirtualTime({ startS: 4, durationS: 2, inS: 1.4 }, 99))
      .toBe(6);
    for (let frame = 0; frame <= 60; frame += 1) {
      expect(
        mapDeckMediaTimeToVirtualTime(
          { startS: 4, durationS: 2, inS: 1.4 },
          1.4 + frame / 30,
        ),
      ).toBeCloseTo(4 + frame / 30, 10);
    }
  });

  it("does not publish a video transport target before a decoded frame confirms it", () => {
    const onFrameTimeUpdate = jest.fn();
    render(
      <Harness
        onPlayingChange={jest.fn()}
        frameDriven
        onFrameTimeUpdate={onFrameTimeUpdate}
      />,
    );
    onFrameTimeUpdate.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "play" }));
    expect(onFrameTimeUpdate).not.toHaveBeenCalled();

    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    const callback = callbacks.get(deckA)?.at(-1);
    act(() => callback?.(0, { mediaTime: 2.4 } as VideoFrameCallbackMetadata));
    expect(onFrameTimeUpdate).toHaveBeenLastCalledWith(expect.closeTo(1, 8));
  });

  it("publishes decoded-frame time without committing every browser frame", () => {
    const onFrameTimeUpdate = jest.fn();
    const onTimeUpdate = jest.fn();
    render(
      <Harness
        onPlayingChange={jest.fn()}
        frameDriven
        onFrameTimeUpdate={onFrameTimeUpdate}
        onTimeUpdate={onTimeUpdate}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "play" }));
    onFrameTimeUpdate.mockClear();
    onTimeUpdate.mockClear();

    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    const callback = callbacks.get(deckA)?.at(-1);
    expect(callback).toBeDefined();
    act(() => callback?.(0, { mediaTime: 2.15 } as VideoFrameCallbackMetadata));

    expect(onFrameTimeUpdate).toHaveBeenLastCalledWith(expect.closeTo(0.75, 8));
    expect(onTimeUpdate).not.toHaveBeenCalled();
  });

  it("keeps timeupdate as a commit/resync signal without advancing authored frames", () => {
    const onFrameTimeUpdate = jest.fn();
    const onTimeUpdate = jest.fn();
    render(
      <Harness
        onPlayingChange={jest.fn()}
        frameDriven
        onFrameTimeUpdate={onFrameTimeUpdate}
        onTimeUpdate={onTimeUpdate}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "play" }));
    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    onFrameTimeUpdate.mockClear();
    onTimeUpdate.mockClear();

    deckA.currentTime = 2.4;
    fireEvent.timeUpdate(deckA);

    expect(onTimeUpdate).toHaveBeenLastCalledWith(expect.closeTo(1, 8));
    expect(onFrameTimeUpdate).not.toHaveBeenCalled();
  });

  it("falls back to timeupdate without inventing browser frames when rVFC is unavailable", () => {
    Object.defineProperty(window.HTMLVideoElement.prototype, "requestVideoFrameCallback", {
      configurable: true,
      writable: true,
      value: undefined,
    });
    const onFrameTimeUpdate = jest.fn();
    const onTimeUpdate = jest.fn();
    render(
      <Harness
        onPlayingChange={jest.fn()}
        frameDriven
        onFrameTimeUpdate={onFrameTimeUpdate}
        onTimeUpdate={onTimeUpdate}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "play" }));
    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    onFrameTimeUpdate.mockClear();
    onTimeUpdate.mockClear();

    deckA.currentTime = 2.4;
    fireEvent.timeUpdate(deckA);

    expect(onTimeUpdate).toHaveBeenLastCalledWith(expect.closeTo(1, 8));
    expect(onFrameTimeUpdate).toHaveBeenLastCalledWith(expect.closeTo(1, 8));
  });

  it("rejects a queued callback from the outgoing deck after a deck swap", () => {
    const onFrameTimeUpdate = jest.fn();
    render(
      <Harness
        onPlayingChange={jest.fn()}
        frameDriven
        onFrameTimeUpdate={onFrameTimeUpdate}
        slots={TWO_SLOTS}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "play" }));

    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    const staleCallback = callbacks.get(deckA)?.at(-1);
    deckA.currentTime = 3.4;
    fireEvent.ended(deckA);
    onFrameTimeUpdate.mockClear();

    act(() => staleCallback?.(0, { mediaTime: 3.1 } as VideoFrameCallbackMetadata));
    expect(onFrameTimeUpdate).not.toHaveBeenCalled();
  });

  it("keeps decoded-frame callbacks on the active deck during an overlap", () => {
    render(
      <Harness
        onPlayingChange={jest.fn()}
        frameDriven
        onFrameTimeUpdate={jest.fn()}
        slots={TRANSITION_SLOTS}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "play" }));
    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    const deckB = screen.getByTestId("deck-b") as HTMLVideoElement;
    const activeCallback = callbacks.get(deckA)?.at(-1);

    act(() =>
      activeCallback?.(0, {
        mediaTime: SLOT.inS + 1.75,
      } as VideoFrameCallbackMetadata),
    );

    expect(callbacks.get(deckB)).toBeUndefined();
  });

  it("invalidates queued frames on pause and starts a fresh generation on resume", () => {
    const onFrameTimeUpdate = jest.fn();
    render(
      <Harness
        onPlayingChange={jest.fn()}
        frameDriven
        onFrameTimeUpdate={onFrameTimeUpdate}
      />,
    );
    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    fireEvent.click(screen.getByRole("button", { name: "play" }));
    const staleCallback = callbacks.get(deckA)?.at(-1);
    const requestsBeforePause = requestSpy.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: "pause" }));
    onFrameTimeUpdate.mockClear();
    act(() => staleCallback?.(0, { mediaTime: 2.4 } as VideoFrameCallbackMetadata));
    expect(onFrameTimeUpdate).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "play" }));
    expect(requestSpy.mock.calls.length).toBeGreaterThan(requestsBeforePause);
  });

  it("resamples authoritative media time after seek and playback-rate changes", () => {
    const onFrameTimeUpdate = jest.fn();
    render(
      <Harness
        onPlayingChange={jest.fn()}
        frameDriven
        onFrameTimeUpdate={onFrameTimeUpdate}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "play" }));
    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;

    onFrameTimeUpdate.mockClear();
    deckA.currentTime = 2.4;
    fireEvent.seeked(deckA);
    expect(onFrameTimeUpdate).toHaveBeenLastCalledWith(expect.closeTo(1, 8));

    onFrameTimeUpdate.mockClear();
    deckA.currentTime = 2.9;
    fireEvent.rateChange(deckA);
    expect(onFrameTimeUpdate).toHaveBeenLastCalledWith(expect.closeTo(1.5, 8));
  });

  it("recovers from a background-tab callback gap using authoritative media time", () => {
    const visibilityDescriptor = Object.getOwnPropertyDescriptor(document, "visibilityState");
    const onFrameTimeUpdate = jest.fn();
    try {
      render(
        <Harness
          onPlayingChange={jest.fn()}
          frameDriven
          onFrameTimeUpdate={onFrameTimeUpdate}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: "play" }));
      const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
      deckA.currentTime = 2.65;
      onFrameTimeUpdate.mockClear();

      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "visible",
      });
      document.dispatchEvent(new Event("visibilitychange"));

      expect(onFrameTimeUpdate).toHaveBeenLastCalledWith(expect.closeTo(1.25, 8));
    } finally {
      if (visibilityDescriptor) {
        Object.defineProperty(document, "visibilityState", visibilityDescriptor);
      } else {
        Reflect.deleteProperty(document, "visibilityState");
      }
    }
  });

  it("preserves the legacy clock and schedules no decoded-frame callbacks when flag-off", () => {
    const onTimeUpdate = jest.fn();
    render(<Harness onPlayingChange={jest.fn()} onTimeUpdate={onTimeUpdate} />);
    fireEvent.click(screen.getByRole("button", { name: "play" }));
    expect(requestSpy).not.toHaveBeenCalled();
    onTimeUpdate.mockClear();
    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    deckA.currentTime = 2.4;
    fireEvent.seeked(deckA);
    fireEvent.rateChange(deckA);
    expect(onTimeUpdate).not.toHaveBeenCalled();
  });
});

describe("useVirtualPreview music transport", () => {
  let playSpy: ReturnType<typeof jest.spyOn>;
  let pauseSpy: ReturnType<typeof jest.spyOn>;

  beforeEach(() => {
    jest.spyOn(window.HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    pauseSpy = jest
      .spyOn(window.HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => {});
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("pauses the whole virtual transport when music playback is rejected", async () => {
    const onPlayingChange = jest.fn();
    playSpy = jest
      .spyOn(window.HTMLMediaElement.prototype, "play")
      .mockImplementation(function playMock(this: HTMLMediaElement) {
        if (this.tagName === "AUDIO") {
          return Promise.reject(new DOMException("blocked", "NotAllowedError"));
        }
        return Promise.resolve();
      });

    render(<Harness onPlayingChange={onPlayingChange} />);
    fireEvent.click(screen.getByRole("button", { name: "play" }));

    await waitFor(() => expect(onPlayingChange).toHaveBeenLastCalledWith(false));
    expect(pauseSpy).toHaveBeenCalled();

    const videoPlayCalls = playSpy.mock.instances.filter(
      (el: unknown) => (el as HTMLMediaElement).tagName === "VIDEO",
    );
    const audioPlayCalls = playSpy.mock.instances.filter(
      (el: unknown) => (el as HTMLMediaElement).tagName === "AUDIO",
    );
    const videoPauseCalls = pauseSpy.mock.instances.filter(
      (el: unknown) => (el as HTMLMediaElement).tagName === "VIDEO",
    );
    const audioPauseCalls = pauseSpy.mock.instances.filter(
      (el: unknown) => (el as HTMLMediaElement).tagName === "AUDIO",
    );
    expect(videoPlayCalls).toHaveLength(1);
    expect(audioPlayCalls).toHaveLength(1);
    expect(videoPauseCalls.length).toBeGreaterThan(0);
    expect(audioPauseCalls.length).toBeGreaterThan(0);
  });

  it("does not pause the transport when play() rejects with AbortError (src swap)", async () => {
    const onPlayingChange = jest.fn();
    playSpy = jest
      .spyOn(window.HTMLMediaElement.prototype, "play")
      .mockImplementation(function playMock(this: HTMLMediaElement) {
        if (this.tagName === "AUDIO") {
          // What the browser throws when a src change lands under a pending play().
          return Promise.reject(new DOMException("interrupted", "AbortError"));
        }
        return Promise.resolve();
      });

    render(<Harness onPlayingChange={onPlayingChange} />);
    fireEvent.click(screen.getByRole("button", { name: "play" }));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(onPlayingChange).not.toHaveBeenCalledWith(false);
    const videoPauseCalls = pauseSpy.mock.instances.filter(
      (el: unknown) => (el as HTMLMediaElement).tagName === "VIDEO",
    );
    expect(videoPauseCalls).toHaveLength(0);
  });

  it("maps the sound-lane mute to the virtual music element", () => {
    const { rerender } = render(<Harness onPlayingChange={jest.fn()} soundMuted />);
    expect(screen.getByTestId("music")).toHaveProperty("muted", true);

    rerender(<Harness onPlayingChange={jest.fn()} soundMuted={false} />);
    expect(screen.getByTestId("music")).toHaveProperty("muted", false);
  });

  it("pauses music when the active video reaches its native end before the virtual boundary", () => {
    const onPlayingChange = jest.fn();
    playSpy = jest
      .spyOn(window.HTMLMediaElement.prototype, "play")
      .mockImplementation(() => Promise.resolve());

    render(<Harness onPlayingChange={onPlayingChange} />);

    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    const music = screen.getByTestId("music") as HTMLAudioElement;

    fireEvent.click(screen.getByRole("button", { name: "play" }));
    deckA.currentTime = 2;
    music.currentTime = 57;
    fireEvent.ended(deckA);

    const audioPauseCalls = pauseSpy.mock.instances.filter(
      (el: unknown) => (el as HTMLMediaElement).tagName === "AUDIO",
    );
    expect(audioPauseCalls.length).toBeGreaterThan(0);
    expect(onPlayingChange).toHaveBeenLastCalledWith(false);
  });
});

describe("useVirtualPreview deck muting", () => {
  beforeEach(() => {
    jest.spyOn(window.HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    jest.spyOn(window.HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("mutes both decks when a music track is active even though the video lane is unmuted", () => {
    render(<Harness onPlayingChange={jest.fn()} musicTrackActive videoMuted={false} />);
    expect(screen.getByTestId("deck-a")).toHaveProperty("muted", true);
    expect(screen.getByTestId("deck-b")).toHaveProperty("muted", true);
  });

  it("mutes decks and renders no music element when a track is active but its preview URL is missing", () => {
    render(<Harness onPlayingChange={jest.fn()} musicTrackActive musicAudioUrl={null} />);
    expect(screen.getByTestId("deck-a")).toHaveProperty("muted", true);
    expect(screen.getByTestId("deck-b")).toHaveProperty("muted", true);
    expect(screen.queryByTestId("music")).toBeNull();
  });

  it("keeps native clip audio when no music track is active", () => {
    render(<Harness onPlayingChange={jest.fn()} musicTrackActive={false} />);
    expect(screen.getByTestId("deck-a")).toHaveProperty("muted", false);
    expect(screen.getByTestId("deck-b")).toHaveProperty("muted", false);
  });

  it("still honors the video-lane mute when no music track is active", () => {
    render(<Harness onPlayingChange={jest.fn()} musicTrackActive={false} videoMuted />);
    expect(screen.getByTestId("deck-a")).toHaveProperty("muted", true);
    expect(screen.getByTestId("deck-b")).toHaveProperty("muted", true);
  });

  it("re-mutes decks when a track is picked mid-preview", () => {
    const { rerender } = render(
      <Harness onPlayingChange={jest.fn()} musicTrackActive={false} />,
    );
    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    expect(deckA.muted).toBe(false);

    rerender(<Harness onPlayingChange={jest.fn()} musicTrackActive />);
    expect(deckA.muted).toBe(true);
  });
});

describe("useVirtualPreview music URL refresh", () => {
  let playSpy: ReturnType<typeof jest.spyOn>;
  let pauseSpy: ReturnType<typeof jest.spyOn>;

  beforeEach(() => {
    jest.spyOn(window.HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    playSpy = jest
      .spyOn(window.HTMLMediaElement.prototype, "play")
      .mockImplementation(() => Promise.resolve());
    pauseSpy = jest
      .spyOn(window.HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => {});
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("invokes onMusicError and pauses only the music when the audio element errors", () => {
    const onMusicError = jest.fn();
    render(<Harness onPlayingChange={jest.fn()} musicTrackActive onMusicError={onMusicError} />);

    fireEvent.error(screen.getByTestId("music"));

    expect(onMusicError).toHaveBeenCalledTimes(1);
    const audioPauseCalls = pauseSpy.mock.instances.filter(
      (el: unknown) => (el as HTMLMediaElement).tagName === "AUDIO",
    );
    const videoPauseCalls = pauseSpy.mock.instances.filter(
      (el: unknown) => (el as HTMLMediaElement).tagName === "VIDEO",
    );
    expect(audioPauseCalls.length).toBeGreaterThan(0);
    expect(videoPauseCalls).toHaveLength(0);
  });

  it("resumes music at the mapped offset when a fresh URL arrives while playing", async () => {
    const { rerender } = render(
      <Harness
        onPlayingChange={jest.fn()}
        musicTrackActive
        musicAudioUrl="https://cdn.example.test/music.m4a?sig=expired"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "play" }));

    const playsBefore = playSpy.mock.instances.filter(
      (el: unknown) => (el as HTMLMediaElement).tagName === "AUDIO",
    ).length;
    expect(playsBefore).toBeGreaterThan(0);

    rerender(
      <Harness
        onPlayingChange={jest.fn()}
        musicTrackActive
        musicAudioUrl="https://cdn.example.test/music.m4a?sig=fresh"
      />,
    );

    await waitFor(() => {
      const audioPlays = playSpy.mock.instances.filter(
        (el: unknown) => (el as HTMLMediaElement).tagName === "AUDIO",
      );
      expect(audioPlays.length).toBeGreaterThan(playsBefore);
    });
    const music = screen.getByTestId("music") as HTMLAudioElement;
    expect(Math.abs(music.currentTime - 55.71)).toBeLessThan(0.1);
  });
});

describe("useVirtualPreview transport", () => {
  let playSpy: ReturnType<typeof jest.spyOn>;
  let pauseSpy: ReturnType<typeof jest.spyOn>;

  const audioPlays = () =>
    playSpy.mock.instances.filter(
      (el: unknown) => (el as HTMLMediaElement).tagName === "AUDIO",
    ).length;
  const audioPauses = () =>
    pauseSpy.mock.instances.filter(
      (el: unknown) => (el as HTMLMediaElement).tagName === "AUDIO",
    ).length;
  const playsOn = (el: HTMLMediaElement) =>
    playSpy.mock.instances.filter((inst: unknown) => inst === el).length;

  beforeEach(() => {
    jest.spyOn(window.HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    playSpy = jest
      .spyOn(window.HTMLMediaElement.prototype, "play")
      .mockImplementation(() => Promise.resolve());
    pauseSpy = jest
      .spyOn(window.HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => {});
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("seeks the music to the mapped offset once its metadata loads", () => {
    render(<Harness onPlayingChange={jest.fn()} musicTrackActive />);
    fireEvent.click(screen.getByRole("button", { name: "play" }));

    const music = screen.getByTestId("music") as HTMLAudioElement;
    music.currentTime = 0;
    const playsBefore = audioPlays();
    fireEvent.loadedMetadata(music);

    expect(Math.abs(music.currentTime - 55.71)).toBeLessThan(0.1);
    expect(audioPlays()).toBeGreaterThan(playsBefore);
  });

  it("never pauses the music when the active deck stalls (music is the master clock)", () => {
    jest.useFakeTimers();
    try {
      render(<Harness onPlayingChange={jest.fn()} musicTrackActive />);
      fireEvent.click(screen.getByRole("button", { name: "play" }));

      const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
      const pausesBefore = audioPauses();
      const playsBefore = audioPlays();
      fireEvent.waiting(deckA);
      jest.advanceTimersByTime(2000); // stall persists — music must not care
      fireEvent.playing(deckA);

      expect(audioPauses()).toBe(pausesBefore);
      expect(audioPlays()).toBe(playsBefore);
    } finally {
      jest.useRealTimers();
    }
  });

  it("jumps the video forward when it falls behind the running music", () => {
    render(<Harness onPlayingChange={jest.fn()} musicTrackActive />);
    fireEvent.click(screen.getByRole("button", { name: "play" }));

    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    const music = screen.getByTestId("music") as HTMLAudioElement;
    Object.defineProperty(music, "paused", { value: false, configurable: true });

    music.currentTime = 55.71 + 1.0; // music ran 1s ahead while the deck stalled
    deckA.currentTime = 1.4; // deck frozen at its in-point (virtual time 0)
    fireEvent.timeUpdate(deckA);

    // Video yields to the music: seeked forward to inS + audio-derived offset.
    expect(Math.abs(deckA.currentTime - 2.4)).toBeLessThan(0.05);
    // The running music was NOT touched.
    expect(Math.abs(music.currentTime - 56.71)).toBeLessThan(0.01);
  });

  it("forward-catches the music when it fell behind on its own", () => {
    render(<Harness onPlayingChange={jest.fn()} musicTrackActive />);
    fireEvent.click(screen.getByRole("button", { name: "play" }));

    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    const music = screen.getByTestId("music") as HTMLAudioElement;
    Object.defineProperty(music, "paused", { value: false, configurable: true });

    music.currentTime = 55.71; // music stalled at the cut start
    deckA.currentTime = 1.4 + 1.5; // video advanced to virtual time 1.5
    fireEvent.timeUpdate(deckA);

    expect(Math.abs(music.currentTime - (55.71 + 1.5))).toBeLessThan(0.05);
  });

  it("never rewinds the music at a boundary swap", () => {
    render(<Harness onPlayingChange={jest.fn()} musicTrackActive slots={TWO_SLOTS} />);
    fireEvent.click(screen.getByRole("button", { name: "play" }));

    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    const music = screen.getByTestId("music") as HTMLAudioElement;
    Object.defineProperty(music, "paused", { value: false, configurable: true });

    // Music slightly AHEAD of the boundary target (55.71 + 2 = 57.71).
    music.currentTime = 58.5;
    deckA.currentTime = 3.4; // native end of entry 0
    fireEvent.ended(deckA);

    // Soft sync at the swap must not rewind the running music.
    expect(music.currentTime).toBe(58.5);
  });

  it("keeps the music playing when the timeline changes mid-play", () => {
    const { rerender } = render(
      <Harness onPlayingChange={jest.fn()} musicTrackActive slots={ONE_SLOT} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "play" }));

    const pausesBefore = audioPauses();
    const playsBefore = audioPlays();
    rerender(<Harness onPlayingChange={jest.fn()} musicTrackActive slots={TWO_SLOTS} />);

    expect(audioPauses()).toBe(pausesBefore);
    expect(audioPlays()).toBeGreaterThan(playsBefore);
  });

  it("plays the incoming deck exactly once on a boundary swap (no frame-0 restart)", () => {
    render(<Harness onPlayingChange={jest.fn()} musicTrackActive slots={TWO_SLOTS} />);
    fireEvent.click(screen.getByRole("button", { name: "play" }));

    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    const deckB = screen.getByTestId("deck-b") as HTMLVideoElement;

    // The preload bound deck B to SLOT_2's source without playing it.
    expect(playsOn(deckB)).toBe(0);

    deckA.currentTime = 3.4; // inS 1.4 + durationS 2 => native end of entry 0
    fireEvent.ended(deckA);

    expect(playsOn(deckB)).toBe(1);
    expect(Math.abs(deckB.currentTime - 0.5)).toBeLessThan(0.05);
  });

  it("advances both decks through a transition without rewinding output time", () => {
    const onTimeUpdate = jest.fn();
    render(
      <Harness
        onPlayingChange={jest.fn()}
        musicTrackActive
        onTimeUpdate={onTimeUpdate}
        slots={TRANSITION_SLOTS}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "play" }));

    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    const deckB = screen.getByTestId("deck-b") as HTMLVideoElement;

    // The overlap begins at output t=1.7. Both source clocks should advance
    // from their own in-points while the canvas blends the decks.
    deckA.currentTime = 1.4 + 1.75;
    fireEvent.timeUpdate(deckA);
    expect(playsOn(deckB)).toBeGreaterThan(0);
    expect(deckB.currentTime).toBeCloseTo(0.5 + 0.05, 2);

    deckA.currentTime = 1.4 + 2;
    fireEvent.ended(deckA);

    expect(deckB.currentTime).toBeCloseTo(0.5 + 0.3, 2);
    expect(onTimeUpdate).toHaveBeenLastCalledWith(2);
  });
});

describe("useVirtualPreview carousel-window clock (bug fix: frozen transport)", () => {
  // Both decks are paused for the whole carousel window by design (the
  // mounted CarouselBlockPreview owns rendering it, not a video source) —
  // so the ONLY way `currentTime` used to move while the playhead was inside
  // it was a deck's `timeupdate` event, which a paused deck never fires.
  // Playing into the block therefore froze the transport at the block's
  // start forever. The fix drives `currentTime` from a rAF/wall-clock delta
  // instead while inside the window, then hands off to the next deck via
  // the same boundary path (`finishEntry` -> `swapToNext`) once it ends.
  const CAROUSEL: VirtualCarouselSplice = { position: "intro", durationS: 4 };

  let playSpy: ReturnType<typeof jest.spyOn>;
  let pauseSpy: ReturnType<typeof jest.spyOn>;
  const playsOn = (el: HTMLMediaElement) =>
    playSpy.mock.instances.filter((inst: unknown) => inst === el).length;

  beforeEach(() => {
    jest.spyOn(window.HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    playSpy = jest
      .spyOn(window.HTMLMediaElement.prototype, "play")
      .mockImplementation(() => Promise.resolve());
    pauseSpy = jest
      .spyOn(window.HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => {});
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it("advances currentTime while playing inside the block instead of freezing at its start", () => {
    const onTimeUpdate = jest.fn();
    render(
      <Harness onPlayingChange={jest.fn()} onTimeUpdate={onTimeUpdate} slots={TWO_SLOTS} carousel={CAROUSEL} />,
    );

    // "intro" splices the block at the very start: [0, 4), both clips shift
    // later. Pressing play at t=0 lands directly inside it.
    fireEvent.click(screen.getByRole("button", { name: "play" }));
    expect(onTimeUpdate).toHaveBeenLastCalledWith(0);

    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    const deckB = screen.getByTestId("deck-b") as HTMLVideoElement;

    act(() => {
      jest.advanceTimersByTime(1000);
    });

    // The clock ticked forward on its own — no deck ever fired `timeupdate`.
    const lastReported = onTimeUpdate.mock.calls.at(-1)?.[0] as number;
    expect(lastReported).toBeGreaterThan(0.5);
    expect(lastReported).toBeLessThan(4);
    // Neither deck was ever asked to play while still inside the block — the
    // preview component owns the visual, not a video source.
    expect(playsOn(deckA)).toBe(0);
    expect(playsOn(deckB)).toBe(0);
  });

  it("publishes display-rate frame samples but throttles shell commits in a frame-driven block", () => {
    const onTimeUpdate = jest.fn();
    const onFrameTimeUpdate = jest.fn();
    render(
      <Harness
        onPlayingChange={jest.fn()}
        onTimeUpdate={onTimeUpdate}
        onFrameTimeUpdate={onFrameTimeUpdate}
        frameDriven
        slots={TWO_SLOTS}
        carousel={CAROUSEL}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "play" }));
    onTimeUpdate.mockClear();
    onFrameTimeUpdate.mockClear();

    act(() => {
      jest.advanceTimersByTime(100);
    });

    expect(onFrameTimeUpdate.mock.calls.length).toBeGreaterThan(2);
    expect(onTimeUpdate).not.toHaveBeenCalled();
    const lastFrame = onFrameTimeUpdate.mock.calls.at(-1)?.[0] as number;
    expect(lastFrame).toBeGreaterThan(0.05);
    expect(lastFrame).toBeLessThan(0.2);

    act(() => {
      jest.advanceTimersByTime(150);
    });
    expect(onTimeUpdate).toHaveBeenCalledTimes(1);
  });

  it("hands off to the next deck at the correct boundary once the block ends, and stops advancing on its own after that", () => {
    const onTimeUpdate = jest.fn();
    render(
      <Harness onPlayingChange={jest.fn()} onTimeUpdate={onTimeUpdate} slots={TWO_SLOTS} carousel={CAROUSEL} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "play" }));

    const deckB = screen.getByTestId("deck-b") as HTMLVideoElement;

    act(() => {
      // Past the 4s window.
      jest.advanceTimersByTime(4200);
    });

    // finishEntry -> swapToNext handed off exactly like a clip-to-clip cut:
    // the next entry (shifted clip 0, inS 1.4) is seeked and played on the
    // OTHER deck (deck was "a"; the block itself never touched a deck).
    expect(playsOn(deckB)).toBeGreaterThan(0);
    expect(deckB.currentTime).toBeCloseTo(1.4, 1);
    expect(onTimeUpdate).toHaveBeenCalledWith(4); // exact block-end boundary

    const callsAtHandoff = onTimeUpdate.mock.calls.length;
    act(() => {
      // The rAF clock must not still be running post-handoff (it would keep
      // calling onTimeUpdate against an entry that's no longer "carousel").
      jest.advanceTimersByTime(500);
    });
    expect(onTimeUpdate.mock.calls.length).toBe(callsAtHandoff);
  });

  it("stops the clock on pause and does not resume advancing on its own", () => {
    const onTimeUpdate = jest.fn();
    const onPlayingChange = jest.fn();
    render(
      <Harness onPlayingChange={onPlayingChange} onTimeUpdate={onTimeUpdate} slots={TWO_SLOTS} carousel={CAROUSEL} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "play" }));

    act(() => {
      jest.advanceTimersByTime(500);
    });
    const midPlayCalls = onTimeUpdate.mock.calls.length;
    expect(midPlayCalls).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "pause" }));
    expect(onPlayingChange).toHaveBeenLastCalledWith(false);

    const callsAtPause = onTimeUpdate.mock.calls.length;
    act(() => {
      jest.advanceTimersByTime(1000);
    });
    expect(onTimeUpdate.mock.calls.length).toBe(callsAtPause);
  });

  it("uses transport state to toggle pause inside a non-video window", () => {
    const onPlayingChange = jest.fn();
    const onTimeUpdate = jest.fn();
    render(
      <Harness
        onPlayingChange={onPlayingChange}
        onTimeUpdate={onTimeUpdate}
        slots={TWO_SLOTS}
        carousel={CAROUSEL}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "play" }));
    act(() => jest.advanceTimersByTime(250));
    fireEvent.click(screen.getByRole("button", { name: "toggle" }));

    expect(onPlayingChange).toHaveBeenLastCalledWith(false);
    const callsAtPause = onTimeUpdate.mock.calls.length;
    act(() => jest.advanceTimersByTime(500));
    expect(onTimeUpdate).toHaveBeenCalledTimes(callsAtPause);
  });

  // Bug #2 (visual E2E): "with a staged carousel block, scrubbing OUTSIDE its
  // window shows a light-gray placeholder instead of normal clips." The
  // correct contract (this suite's own docblock intent) is that outside the
  // block window playback is byte-for-byte the SAME as without one — decks
  // load/play the real clips, nothing about the spliced-in block leaks into
  // ordinary clip-to-clip handoff. "outro" places the block AFTER both
  // clips, so playing from t=0 through their shared boundary stays entirely
  // outside it for this whole test.
  it("plays clips normally outside the block window even though one is staged later in the timeline", () => {
    const onTimeUpdate = jest.fn();
    render(
      <Harness
        onPlayingChange={jest.fn()}
        onTimeUpdate={onTimeUpdate}
        slots={TWO_SLOTS}
        carousel={{ position: "outro", durationS: 4 }}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "play" }));

    const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
    const deckB = screen.getByTestId("deck-b") as HTMLVideoElement;

    // Clip 0 plays normally from its in-point — no gray/blank gap.
    expect(deckA.src).toBe(DEFAULT_CLIPS[0].signed_url);
    expect(playsOn(deckA)).toBeGreaterThan(0);
    expect(deckA.currentTime).toBeCloseTo(1.4, 1);

    // The clip-to-clip boundary at virtual t=2 hands off to clip 1 exactly
    // as it would with no carousel staged at all.
    deckA.currentTime = 1.4 + 2; // native end of entry 0
    fireEvent.ended(deckA);

    expect(deckB.src).toBe(DEFAULT_CLIPS[1].signed_url);
    expect(playsOn(deckB)).toBeGreaterThan(0);
    expect(deckB.currentTime).toBeCloseTo(0.5, 1);
    expect(onTimeUpdate).toHaveBeenCalledWith(2);
  });
});
