import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import KriaEditStory from "@/components/KriaEditStory";
import {
  AUTO_RENDER_START_MS,
  AUTO_SOUND_START_MS,
  AUTO_STORY_DURATION_MS,
  getAutoKriaHeadlineLineCount,
  getAutoKriaStoryStep,
  getAutoStoryAudioVolume,
  getKriaHeadlineLineCount,
  getKriaStoryStep,
} from "@/components/KriaEditStorySteps";

beforeEach(() => {
  Object.defineProperty(HTMLMediaElement.prototype, "play", {
    configurable: true,
    value: jest.fn().mockResolvedValue(undefined),
  });
  Object.defineProperty(HTMLMediaElement.prototype, "pause", {
    configurable: true,
    value: jest.fn(),
  });
});

afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
});

function mockReducedMotion(matches: boolean) {
  let currentMatches = matches;
  let changeListener: (() => void) | undefined;
  const addEventListener = jest.fn((_type: string, listener: () => void) => {
    changeListener = listener;
  });
  const removeEventListener = jest.fn();
  const mediaQuery = {
    get matches() { return currentMatches; },
    media: "(prefers-reduced-motion: reduce)",
    addEventListener,
    removeEventListener,
  };
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: jest.fn().mockReturnValue(mediaQuery),
  });
  return {
    addEventListener,
    removeEventListener,
    setMatches(nextMatches: boolean) {
      currentMatches = nextMatches;
      act(() => changeListener?.());
    },
  };
}

function mockAnimationFrames() {
  let nextId = 1;
  const callbacks = new Map<number, FrameRequestCallback>();
  jest.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
    const id = nextId;
    nextId += 1;
    callbacks.set(id, callback);
    return id;
  });
  jest.spyOn(window, "cancelAnimationFrame").mockImplementation((id) => {
    callbacks.delete(id);
  });

  return {
    runAll(now: number) {
      const pending = Array.from(callbacks.entries());
      pending.forEach(([id]) => callbacks.delete(id));
      act(() => pending.forEach(([, callback]) => callback(now)));
    },
    runLatest(now: number) {
      const entry = Array.from(callbacks.entries()).at(-1);
      if (!entry) throw new Error("No animation frame was scheduled");
      callbacks.delete(entry[0]);
      act(() => entry[1](now));
    },
  };
}

function createDeferred() {
  let resolve!: () => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<void>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

describe("getKriaStoryStep", () => {
  it.each([
    [0, 0],
    [0.02, 1],
    [0.12, 2],
    [0.24, 3],
    [0.36, 4],
    [0.52, 5],
    [0.68, 6],
    [0.82, 7],
    [1, 7],
  ])("maps progress %s to storyboard beat %s", (progress, expected) => {
    expect(getKriaStoryStep(progress)).toBe(expected);
  });
});

describe("getAutoKriaStoryStep", () => {
  it.each([
    [0, 0],
    [299, 0],
    [300, 1],
    [1_100, 2],
    [1_900, 3],
    [AUTO_RENDER_START_MS, 4],
    [4_500, 5],
    [5_500, 6],
    [7_199, 6],
    [AUTO_SOUND_START_MS, 7],
    [AUTO_STORY_DURATION_MS, 7],
  ])("maps elapsed time %sms to automatic beat %s", (elapsed, expected) => {
    expect(getAutoKriaStoryStep(elapsed)).toBe(expected);
  });
});

describe("headline sequencing", () => {
  it.each([
    [0.86, 0],
    [0.87, 1],
    [0.93, 2],
    [0.99, 3],
  ])("reveals %s scroll progress as %s lines", (progress, expected) => {
    expect(getKriaHeadlineLineCount(progress)).toBe(expected);
  });

  it.each([
    [7_799, 0],
    [7_800, 1],
    [8_800, 2],
    [9_800, 3],
  ])("reveals %sms automatic time as %s lines", (elapsed, expected) => {
    expect(getAutoKriaHeadlineLineCount(elapsed)).toBe(expected);
  });
});

describe("getAutoStoryAudioVolume", () => {
  it("keeps music silent until the sound-effects beat, then fades it in", () => {
    expect(getAutoStoryAudioVolume(0)).toBe(0);
    expect(getAutoStoryAudioVolume(AUTO_SOUND_START_MS - 1)).toBe(0);
    expect(getAutoStoryAudioVolume(AUTO_SOUND_START_MS)).toBe(0);
    expect(getAutoStoryAudioVolume(AUTO_SOUND_START_MS + 150)).toBeCloseTo(0.4);
    expect(getAutoStoryAudioVolume(AUTO_SOUND_START_MS + 300)).toBeCloseTo(0.8);
    expect(getAutoStoryAudioVolume(AUTO_STORY_DURATION_MS)).toBeCloseTo(0.8);
  });
});

describe("KriaEditStory reduced motion", () => {
  it("exposes the complete editing story without requiring playback", () => {
    mockReducedMotion(false);

    render(<KriaEditStory />);

    expect(screen.getByText("How Kria builds your edit:")).toBeInTheDocument();
    expect(screen.getAllByRole("heading")).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveAttribute("aria-label");
    expect(
      screen.getByText(/captions and visual effects arrive together/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/only then does the finished edit begin playing music/i),
    ).toBeInTheDocument();
  });

  it("renders the completed composition without requiring scroll", async () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: jest.fn().mockReturnValue({
        matches: true,
        media: "(prefers-reduced-motion: reduce)",
        addEventListener: jest.fn(),
        removeEventListener: jest.fn(),
      }),
    });

    render(<KriaEditStory />);

    await waitFor(() => {
      expect(screen.getByText("Add sound effects, 82–100%")).toBeInTheDocument();
    });
    expect(
      screen.getByLabelText("How Kria turns raw videos into a finished edit"),
    ).toHaveAttribute("data-reduced-motion", "true");
  });

  it("uses the raw-upload media set without media-role labels", () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: jest.fn().mockReturnValue({
        matches: false,
        media: "(prefers-reduced-motion: reduce)",
        addEventListener: jest.fn(),
        removeEventListener: jest.fn(),
      }),
    });

    const { container } = render(<KriaEditStory />);
    const sources = Array.from(container.querySelectorAll("video")).map((video) =>
      video.getAttribute("src"),
    );

    expect(sources).toContain("/landing/raw-story/lisbon.mp4");
    expect(sources).toContain("/landing/raw-story/travel-render.mp4");
    expect(sources).not.toContain("/landing/raw-story/beach-gathering.mp4");
    expect(screen.getByRole("heading", { level: 1 })).toHaveAttribute(
      "data-image-blend",
      "difference",
    );
    expect(screen.getByRole("heading", { level: 1 })).toHaveAttribute(
      "data-screen-alignment",
      "center",
    );
    expect(container.querySelector('[class*="activeShot"]')).not.toBeInTheDocument();
    expect(container.querySelectorAll('[data-active="true"]')).toHaveLength(0);
    expect(screen.queryByText(/video ·|video 0|image overlay/i)).not.toBeInTheDocument();
    expect(screen.queryByText("Every raw moment")).not.toBeInTheDocument();
    expect(screen.getByText("Add visual effects")).toBeInTheDocument();
    expect(screen.queryByText("Remove filler sounds")).not.toBeInTheDocument();
    expect(container.querySelectorAll('[data-feature-group="captions-effects"]')).toHaveLength(2);
  });

  it("ends at the primary CTA without a progress footer", () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: jest.fn().mockReturnValue({
        matches: false,
        media: "(prefers-reduced-motion: reduce)",
        addEventListener: jest.fn(),
        removeEventListener: jest.fn(),
      }),
    });

    render(<KriaEditStory mode="auto" />);

    expect(screen.getByRole("link", { name: /create my first edit/i })).toHaveAttribute(
      "href",
      "/plan",
    );
    expect(screen.queryByText("Auto")).not.toBeInTheDocument();
    expect(screen.queryByText("0%")).not.toBeInTheDocument();
  });

  it("starts the automatic comparison from one sound-enabled action", async () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: jest.fn().mockReturnValue({
        matches: false,
        media: "(prefers-reduced-motion: reduce)",
        addEventListener: jest.fn(),
        removeEventListener: jest.fn(),
      }),
    });

    render(<KriaEditStory mode="auto" />);
    const audio = screen.getByTestId("auto-story-audio") as HTMLAudioElement;
    const play = jest.fn().mockResolvedValue(undefined);
    const pause = jest.fn();
    audio.play = play;
    audio.pause = pause;

    expect(audio).toHaveAttribute("src", "/landing/raw-story/travel-reference-audio.m4a");

    fireEvent.click(screen.getByRole("button", { name: /play with sound/i }));

    expect(play).toHaveBeenCalledTimes(1);
    expect(audio.volume).toBe(0);
    expect(
      screen.getByLabelText("How Kria turns raw videos into a finished edit"),
    ).toHaveAttribute("data-mode", "auto");
    expect(await screen.findByRole("button", { name: /pause automatic demo/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("link", { name: "Automatic" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    fireEvent.click(screen.getByRole("button", { name: /pause automatic demo/i }));
    expect(pause).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /with sound/i })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("resumes from the paused timestamp and resets when replaying", async () => {
    mockReducedMotion(false);
    const frames = mockAnimationFrames();

    const { container } = render(<KriaEditStory mode="auto" />);
    const audio = screen.getByTestId("auto-story-audio") as HTMLAudioElement;
    const renderedVideo = container.querySelector<HTMLVideoElement>(
      'video[src="/landing/raw-story/travel-render.mp4"]',
    );
    const overlayVideo = container.querySelector<HTMLVideoElement>(
      'video[src="/landing/raw-story/corfu.mp4"]',
    );
    expect(renderedVideo).not.toBeNull();
    expect(overlayVideo).not.toBeNull();
    const renderedPlay = jest.fn().mockResolvedValue(undefined);
    const renderedPause = jest.fn();
    renderedVideo!.play = renderedPlay;
    renderedVideo!.pause = renderedPause;
    const play = jest.fn().mockResolvedValue(undefined);
    const pause = jest.fn();
    audio.play = play;
    audio.pause = pause;
    Object.defineProperty(audio, "paused", {
      configurable: true,
      get: () => false,
    });

    fireEvent.click(screen.getByRole("button", { name: /play with sound/i }));
    audio.currentTime = 2;
    frames.runLatest(2_000);
    expect(screen.getByText("Create more, 24–36%")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /pause automatic demo/i }));
    expect(screen.getByRole("button", { name: /resume with sound/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /resume with sound/i }));
    expect(audio.currentTime).toBe(2);
    expect(play).toHaveBeenCalledTimes(2);

    audio.currentTime = AUTO_STORY_DURATION_MS / 1_000;
    frames.runLatest(AUTO_STORY_DURATION_MS);
    expect(screen.getByRole("button", { name: /replay with sound/i })).toBeInTheDocument();
    expect(renderedPlay).toHaveBeenCalled();
    expect(renderedPause).toHaveBeenCalled();
    expect(renderedVideo!.currentTime).toBeCloseTo(
      (AUTO_STORY_DURATION_MS - AUTO_RENDER_START_MS) / 1_000,
    );
    overlayVideo!.currentTime = 1.25;

    fireEvent.click(screen.getByRole("button", { name: /replay with sound/i }));
    expect(audio.currentTime).toBe(0);
    expect(overlayVideo!.currentTime).toBe(0);
    expect(play).toHaveBeenCalledTimes(3);
  });

  it("stops reduced-motion playback after the authored render duration", async () => {
    jest.useFakeTimers();
    mockReducedMotion(true);

    render(<KriaEditStory mode="auto" />);
    const audio = screen.getByTestId("auto-story-audio") as HTMLAudioElement;
    const pause = jest.fn();
    audio.play = jest.fn().mockResolvedValue(undefined);
    audio.pause = pause;

    await waitFor(() => {
      expect(
        screen.getByLabelText("How Kria turns raw videos into a finished edit"),
      ).toHaveAttribute("data-reduced-motion", "true");
    });
    fireEvent.click(screen.getByRole("button", { name: /replay with sound/i }));

    act(() => {
      jest.advanceTimersByTime(AUTO_STORY_DURATION_MS - AUTO_RENDER_START_MS);
    });

    expect(pause).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /replay with sound/i })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("clears the reduced-motion stop timer when unmounted", async () => {
    jest.useFakeTimers();
    mockReducedMotion(true);

    const { unmount } = render(<KriaEditStory mode="auto" />);
    const audio = screen.getByTestId("auto-story-audio") as HTMLAudioElement;
    const pause = jest.fn();
    audio.play = jest.fn().mockResolvedValue(undefined);
    audio.pause = pause;

    await waitFor(() => {
      expect(
        screen.getByLabelText("How Kria turns raw videos into a finished edit"),
      ).toHaveAttribute("data-reduced-motion", "true");
    });
    fireEvent.click(screen.getByRole("button", { name: /replay with sound/i }));
    unmount();
    const callsAfterUnmount = pause.mock.calls.length;

    act(() => {
      jest.advanceTimersByTime(AUTO_STORY_DURATION_MS);
    });

    expect(pause).toHaveBeenCalledTimes(callsAfterUnmount);
  });

  it("cancels the reduced-motion stop timer when paused early", async () => {
    jest.useFakeTimers();
    mockReducedMotion(true);

    render(<KriaEditStory mode="auto" />);
    const audio = screen.getByTestId("auto-story-audio") as HTMLAudioElement;
    const pause = jest.fn();
    audio.play = jest.fn().mockResolvedValue(undefined);
    audio.pause = pause;

    await waitFor(() => {
      expect(
        screen.getByLabelText("How Kria turns raw videos into a finished edit"),
      ).toHaveAttribute("data-reduced-motion", "true");
    });
    fireEvent.click(screen.getByRole("button", { name: /replay with sound/i }));
    fireEvent.click(screen.getByRole("button", { name: /pause automatic demo/i }));
    const callsAfterPause = pause.mock.calls.length;

    act(() => {
      jest.advanceTimersByTime(AUTO_STORY_DURATION_MS);
    });

    expect(pause).toHaveBeenCalledTimes(callsAfterPause);
    expect(screen.getByRole("button", { name: /replay with sound/i })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("remains controllable when the browser rejects media playback", async () => {
    mockReducedMotion(false);

    render(<KriaEditStory mode="auto" />);
    const audio = screen.getByTestId("auto-story-audio") as HTMLAudioElement;
    audio.play = jest
      .fn()
      .mockRejectedValueOnce(new Error("playback blocked"))
      .mockResolvedValue(undefined);
    audio.pause = jest.fn();

    fireEvent.click(screen.getByRole("button", { name: /play with sound/i }));

    expect(
      await screen.findByRole("button", { name: /playing without sound/i }),
    ).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: /playing without sound/i }));
    expect(screen.getByRole("button", { name: /with sound/i })).toHaveAttribute(
      "aria-pressed",
      "false",
    );

    // Playback rejection can take long enough for the visual clock to advance,
    // so this control may correctly read either "Play" or "Resume" with sound.
    fireEvent.click(screen.getByRole("button", { name: /with sound/i }));
    expect(audio.play).toHaveBeenCalledTimes(2);
    expect(
      await screen.findByRole("button", { name: /pause automatic demo/i }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("resumes from the visual timeline after blocked audio instead of rewinding", async () => {
    mockReducedMotion(false);
    const frames = mockAnimationFrames();
    jest.spyOn(performance, "now").mockReturnValue(0);

    render(<KriaEditStory mode="auto" />);
    const audio = screen.getByTestId("auto-story-audio") as HTMLAudioElement;
    let audioPaused = true;
    Object.defineProperty(audio, "paused", { configurable: true, get: () => audioPaused });
    audio.play = jest
      .fn()
      .mockRejectedValueOnce(new Error("playback blocked"))
      .mockImplementation(() => {
        audioPaused = false;
        return Promise.resolve();
      });
    audio.pause = jest.fn(() => { audioPaused = true; });

    fireEvent.click(screen.getByRole("button", { name: /play with sound/i }));
    frames.runLatest(4_000);
    expect(screen.getByText("Captions + visual effects, 36–52%")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: /playing without sound/i }));

    fireEvent.click(screen.getByRole("button", { name: /resume with sound/i }));
    expect(audio.currentTime).toBeCloseTo(4);
    frames.runLatest(4_100);
    expect(screen.getByText("Captions + visual effects, 36–52%")).toBeInTheDocument();
  });

  it("stops active playback when reduced motion is enabled at runtime", async () => {
    const mediaQuery = mockReducedMotion(false);

    const { container, unmount } = render(<KriaEditStory mode="auto" />);
    const audio = screen.getByTestId("auto-story-audio") as HTMLAudioElement;
    audio.play = jest.fn().mockResolvedValue(undefined);
    audio.pause = jest.fn();
    const renderedVideo = container.querySelector<HTMLVideoElement>(
      'video[src="/landing/raw-story/travel-render.mp4"]',
    );
    expect(renderedVideo).not.toBeNull();
    renderedVideo!.pause = jest.fn();

    fireEvent.click(screen.getByRole("button", { name: /play with sound/i }));
    mediaQuery.setMatches(true);

    await waitFor(() => {
      expect(
        screen.getByLabelText("How Kria turns raw videos into a finished edit"),
      ).toHaveAttribute("data-reduced-motion", "true");
    });
    expect(screen.getByText("Add sound effects, 82–100%")).toBeInTheDocument();
    expect(audio.pause).toHaveBeenCalled();
    expect(renderedVideo!.pause).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /replay with sound/i })).toHaveAttribute(
      "aria-pressed",
      "false",
    );

    unmount();
    expect(mediaQuery.removeEventListener).toHaveBeenCalledWith(
      "change",
      expect.any(Function),
    );
  });

  it("ignores a stale audio rejection after a newer playback starts", async () => {
    mockReducedMotion(false);
    const firstPlayback = createDeferred();

    render(<KriaEditStory mode="auto" />);
    const audio = screen.getByTestId("auto-story-audio") as HTMLAudioElement;
    audio.play = jest
      .fn()
      .mockReturnValueOnce(firstPlayback.promise)
      .mockResolvedValue(undefined);
    audio.pause = jest.fn();

    fireEvent.click(screen.getByRole("button", { name: /play with sound/i }));
    fireEvent.click(screen.getByRole("button", { name: /pause automatic demo/i }));
    fireEvent.click(screen.getByRole("button", { name: /play with sound/i }));
    await act(async () => { firstPlayback.reject(new Error("stale rejection")); });

    expect(screen.getByRole("button", { name: /pause automatic demo/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /playing without sound/i })).not.toBeInTheDocument();
  });

  it("attempts rendered-video playback only once after a rejection", () => {
    mockReducedMotion(false);
    const frames = mockAnimationFrames();

    const { container } = render(<KriaEditStory mode="auto" />);
    const audio = screen.getByTestId("auto-story-audio") as HTMLAudioElement;
    Object.defineProperty(audio, "paused", { configurable: true, get: () => false });
    audio.play = jest.fn().mockResolvedValue(undefined);
    const renderedVideo = container.querySelector<HTMLVideoElement>(
      'video[src="/landing/raw-story/travel-render.mp4"]',
    );
    expect(renderedVideo).not.toBeNull();
    const renderedPlay = jest.fn().mockRejectedValue(new Error("video unavailable"));
    renderedVideo!.play = renderedPlay;
    Object.defineProperty(renderedVideo!, "paused", {
      configurable: true,
      get: () => true,
    });

    fireEvent.click(screen.getByRole("button", { name: /play with sound/i }));
    audio.currentTime = AUTO_RENDER_START_MS / 1_000;
    frames.runLatest(AUTO_RENDER_START_MS);
    audio.currentTime += 0.1;
    frames.runLatest(AUTO_RENDER_START_MS + 100);

    expect(renderedPlay).toHaveBeenCalledTimes(1);
  });

  it("retries rendered-video playback once when media becomes playable", async () => {
    mockReducedMotion(false);
    const frames = mockAnimationFrames();

    const { container } = render(<KriaEditStory mode="auto" />);
    const audio = screen.getByTestId("auto-story-audio") as HTMLAudioElement;
    Object.defineProperty(audio, "paused", { configurable: true, get: () => false });
    audio.play = jest.fn().mockResolvedValue(undefined);
    const renderedVideo = container.querySelector<HTMLVideoElement>(
      'video[src="/landing/raw-story/travel-render.mp4"]',
    );
    expect(renderedVideo).not.toBeNull();
    const renderedPlay = jest
      .fn()
      .mockRejectedValueOnce(new Error("video unavailable"))
      .mockResolvedValue(undefined);
    renderedVideo!.play = renderedPlay;
    Object.defineProperty(renderedVideo!, "paused", {
      configurable: true,
      get: () => true,
    });

    fireEvent.click(screen.getByRole("button", { name: /play with sound/i }));
    audio.currentTime = AUTO_RENDER_START_MS / 1_000;
    frames.runLatest(AUTO_RENDER_START_MS);
    await waitFor(() => expect(renderedPlay).toHaveBeenCalledTimes(1));

    fireEvent.canPlay(renderedVideo!);
    expect(renderedPlay).toHaveBeenCalledTimes(2);
    fireEvent.canPlay(renderedVideo!);
    expect(renderedPlay).toHaveBeenCalledTimes(2);
  });

  it("does not arm a retry from a stale rendered-video rejection", async () => {
    mockReducedMotion(false);
    const frames = mockAnimationFrames();
    const firstPlayback = createDeferred();

    const { container } = render(<KriaEditStory mode="auto" />);
    const audio = screen.getByTestId("auto-story-audio") as HTMLAudioElement;
    Object.defineProperty(audio, "paused", { configurable: true, get: () => false });
    audio.play = jest.fn().mockResolvedValue(undefined);
    const renderedVideo = container.querySelector<HTMLVideoElement>(
      'video[src="/landing/raw-story/travel-render.mp4"]',
    );
    expect(renderedVideo).not.toBeNull();
    const renderedPlay = jest
      .fn()
      .mockReturnValueOnce(firstPlayback.promise)
      .mockResolvedValue(undefined);
    renderedVideo!.play = renderedPlay;
    Object.defineProperty(renderedVideo!, "paused", { configurable: true, get: () => true });

    fireEvent.click(screen.getByRole("button", { name: /play with sound/i }));
    audio.currentTime = AUTO_RENDER_START_MS / 1_000;
    frames.runLatest(AUTO_RENDER_START_MS);
    fireEvent.click(screen.getByRole("button", { name: /pause automatic demo/i }));
    fireEvent.click(screen.getByRole("button", { name: /resume with sound/i }));
    frames.runLatest(AUTO_RENDER_START_MS + 100);
    expect(renderedPlay).toHaveBeenCalledTimes(2);

    await act(async () => { firstPlayback.reject(new Error("stale rejection")); });
    fireEvent.canPlay(renderedVideo!);
    expect(renderedPlay).toHaveBeenCalledTimes(2);
  });

  it("does not chase rendered-video drift while the video is buffering", () => {
    mockReducedMotion(false);
    const frames = mockAnimationFrames();

    const { container } = render(<KriaEditStory mode="auto" />);
    const audio = screen.getByTestId("auto-story-audio") as HTMLAudioElement;
    Object.defineProperty(audio, "paused", { configurable: true, get: () => false });
    audio.play = jest.fn().mockResolvedValue(undefined);
    const renderedVideo = container.querySelector<HTMLVideoElement>(
      'video[src="/landing/raw-story/travel-render.mp4"]',
    );
    expect(renderedVideo).not.toBeNull();
    renderedVideo!.play = jest.fn().mockResolvedValue(undefined);
    Object.defineProperty(renderedVideo!, "paused", { configurable: true, get: () => false });
    Object.defineProperty(renderedVideo!, "seeking", { configurable: true, get: () => false });
    let readyState: number = HTMLMediaElement.HAVE_METADATA;
    Object.defineProperty(renderedVideo!, "readyState", {
      configurable: true,
      get: () => readyState,
    });

    fireEvent.click(screen.getByRole("button", { name: /play with sound/i }));
    audio.currentTime = AUTO_RENDER_START_MS / 1_000;
    frames.runLatest(AUTO_RENDER_START_MS);
    audio.currentTime += 1;
    frames.runLatest(AUTO_RENDER_START_MS + 1_000);
    expect(renderedVideo!.currentTime).toBe(0);

    readyState = HTMLMediaElement.HAVE_FUTURE_DATA;
    frames.runLatest(AUTO_RENDER_START_MS + 1_100);
    expect(renderedVideo!.currentTime).toBeCloseTo(1);
  });

  it("updates the scroll beat and recalculates travel geometry on resize", () => {
    mockReducedMotion(false);
    const frames = mockAnimationFrames();

    const { container, unmount } = render(<KriaEditStory />);
    const section = screen.getByLabelText("How Kria turns raw videos into a finished edit");
    Object.defineProperty(section, "offsetHeight", { configurable: true, value: 1_000 });
    Object.defineProperty(section, "offsetTop", { configurable: true, value: 0 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 100 });
    Object.defineProperty(window, "scrollY", { configurable: true, value: 324 });

    const captions = screen.getByText("Captions");
    const stickyStage = captions.parentElement!;
    const captionsTarget = container.querySelector<HTMLElement>('[class*="captionsTarget"]');
    expect(captionsTarget).not.toBeNull();
    Object.defineProperties(captions, {
      offsetParent: { configurable: true, value: stickyStage },
      offsetLeft: { configurable: true, value: 0 },
      offsetTop: { configurable: true, value: 0 },
      offsetWidth: { configurable: true, value: 100 },
      offsetHeight: { configurable: true, value: 50 },
    });
    stickyStage.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 1_000, height: 900, right: 1_000, bottom: 900, x: 0, y: 0, toJSON() {} }) as DOMRect;
    captions.getBoundingClientRect = () => {
      const consumed = captions.getAttribute("data-consumed") === "true";
      return ({
        left: consumed ? 160 : 0,
        top: consumed ? 80 : 0,
        width: consumed ? 24 : 100,
        height: consumed ? 12 : 50,
        right: consumed ? 184 : 100,
        bottom: consumed ? 92 : 50,
        x: consumed ? 160 : 0,
        y: consumed ? 80 : 0,
        toJSON() {},
      }) as DOMRect;
    };
    captionsTarget!.getBoundingClientRect = () =>
      ({ left: 200, top: 100, width: 20, height: 10, right: 220, bottom: 110, x: 200, y: 100, toJSON() {} }) as DOMRect;

    fireEvent.resize(window);
    frames.runAll(0);
    expect(captions.style.getPropertyValue("--travel-scale")).toBe("0.24");

    fireEvent.scroll(window);
    frames.runLatest(16);
    expect(screen.getByText("Captions + visual effects, 36–52%")).toBeInTheDocument();

    fireEvent.resize(window);
    frames.runAll(32);
    expect(captions.style.getPropertyValue("--travel-x")).toBe("160px");
    expect(captions.style.getPropertyValue("--travel-y")).toBe("80px");

    const cancel = window.cancelAnimationFrame as jest.MockedFunction<
      typeof window.cancelAnimationFrame
    >;
    unmount();
    expect(cancel).toHaveBeenCalled();
  });

  it("keeps the phone video-only after the overlay inputs land", () => {
    mockReducedMotion(false);
    const frames = mockAnimationFrames();

    const { container } = render(<KriaEditStory />);
    const section = screen.getByLabelText("How Kria turns raw videos into a finished edit");
    Object.defineProperty(section, "offsetHeight", { configurable: true, value: 1_000 });
    Object.defineProperty(section, "offsetTop", { configurable: true, value: 0 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 100 });
    const overlays = container.querySelectorAll<HTMLVideoElement>(
      'video[src="/landing/raw-story/corfu.mp4"]',
    );
    expect(overlays).toHaveLength(1);
    const [overlay] = Array.from(overlays);
    const play = jest.fn().mockResolvedValue(undefined);
    const pause = jest.fn();
    overlay.play = play;
    overlay.pause = pause;

    Object.defineProperty(window, "scrollY", { configurable: true, value: 400 });
    fireEvent.scroll(window);
    frames.runAll(16);
    expect(play).toHaveBeenCalledTimes(1);
    overlay.currentTime = 1.25;

    Object.defineProperty(window, "scrollY", { configurable: true, value: 550 });
    fireEvent.scroll(window);
    frames.runAll(32);
    expect(pause).toHaveBeenCalled();
    expect(container.querySelector("[data-overlay-result]")).not.toBeInTheDocument();
    expect(
      container.querySelector('video[src="/landing/raw-story/travel-render.mp4"]')?.className,
    ).toContain("activeShot");

    Object.defineProperty(window, "scrollY", { configurable: true, value: 400 });
    fireEvent.scroll(window);
    frames.runAll(48);
    expect(play).toHaveBeenCalledTimes(2);
    expect(overlay.currentTime).toBe(1.25);
  });
});
