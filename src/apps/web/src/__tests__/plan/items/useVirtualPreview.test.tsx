import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, jest } from "@jest/globals";

import type { DraftSlot } from "@/app/generative/timeline-math";
import { useVirtualPreview } from "@/app/plan/items/[id]/_editor/useVirtualPreview";

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

// Stable callbacks — inline jest.fn() props change identity every render,
// which re-fires the hook's timeline effect and skews play/pause counts
// (production passes stable useCallback/setState handlers).
const NOOP_TIME_UPDATE = () => {};
const NOOP_DURATION = () => {};
const NOOP_SOURCE_ERROR = () => {};

function Harness({
  onPlayingChange,
  soundMuted = false,
  videoMuted = false,
  musicTrackActive = false,
  musicAudioUrl = "https://cdn.example.test/music.m4a",
  onMusicError,
  slots = ONE_SLOT,
}: {
  onPlayingChange: (playing: boolean) => void;
  soundMuted?: boolean;
  videoMuted?: boolean;
  musicTrackActive?: boolean;
  musicAudioUrl?: string | null;
  onMusicError?: () => void;
  slots?: DraftSlot[];
}) {
  const preview = useVirtualPreview({
    enabled: true,
    slots,
    clips: DEFAULT_CLIPS,
    grid: EMPTY_GRID,
    currentTime: 0,
    muted: videoMuted,
    musicAudioUrl,
    musicStartS: 55.71,
    soundMuted,
    musicTrackActive,
    onTimeUpdate: NOOP_TIME_UPDATE,
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
    </>
  );
}

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

  it("ignores the transient boundary `waiting` blip (no music hold)", () => {
    jest.useFakeTimers();
    try {
      render(<Harness onPlayingChange={jest.fn()} musicTrackActive />);
      fireEvent.click(screen.getByRole("button", { name: "play" }));

      const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
      const pausesBefore = audioPauses();
      const playsBefore = audioPlays();
      fireEvent.waiting(deckA);
      fireEvent.playing(deckA); // deck recovers before the debounce fires
      jest.advanceTimersByTime(1000);

      expect(audioPauses()).toBe(pausesBefore);
      // The running music must not be re-touched on recovery either.
      expect(audioPlays()).toBe(playsBefore);
    } finally {
      jest.useRealTimers();
    }
  });

  it("holds the music after a sustained stall and resumes with the deck", () => {
    jest.useFakeTimers();
    try {
      render(<Harness onPlayingChange={jest.fn()} musicTrackActive />);
      fireEvent.click(screen.getByRole("button", { name: "play" }));

      const deckA = screen.getByTestId("deck-a") as HTMLVideoElement;
      const pausesBefore = audioPauses();
      fireEvent.waiting(deckA);
      jest.advanceTimersByTime(1000); // stall persists past the debounce
      expect(audioPauses()).toBeGreaterThan(pausesBefore);

      const playsBefore = audioPlays();
      fireEvent.playing(deckA);
      expect(audioPlays()).toBeGreaterThan(playsBefore);
    } finally {
      jest.useRealTimers();
    }
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
});
