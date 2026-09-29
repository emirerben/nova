import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import KriaLifeLanding, {
  getLandingMorphState,
  getLandingScrollProgress,
  getLogoMaskMotionUnit,
  getProductDemoStep,
} from "@/components/KriaLifeLanding";

describe("KriaLifeLanding", () => {
  beforeAll(() => {
    Object.defineProperty(HTMLMediaElement.prototype, "play", {
      configurable: true,
      value: jest.fn().mockResolvedValue(undefined),
    });
    Object.defineProperty(HTMLMediaElement.prototype, "pause", {
      configurable: true,
      value: jest.fn(),
    });
  });

  beforeEach(() => {
    Object.defineProperty(window, "scrollY", { configurable: true, value: 0 });
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: jest.fn().mockReturnValue({
        matches: false,
        media: "(prefers-reduced-motion: reduce)",
        addEventListener: jest.fn(),
        removeEventListener: jest.fn(),
      }),
    });
    Object.defineProperty(window, "CSS", {
      configurable: true,
      value: { supports: jest.fn().mockReturnValue(false) },
    });
    (HTMLMediaElement.prototype.play as jest.Mock).mockReset().mockResolvedValue(undefined);
    (HTMLMediaElement.prototype.pause as jest.Mock).mockReset();
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("maps the reel to the standalone wordmark reveal stages", () => {
    const { container } = render(<KriaLifeLanding />);
    const video = container.querySelector("video")!;
    const hero = container.querySelector("section[data-stage]")!;

    for (const [time, stage] of [
      [0.79, "fullscreen"],
      [0.8, "wordmark"],
      [2.9, "complete"],
    ] as const) {
      Object.defineProperty(video, "currentTime", {
        configurable: true,
        value: time,
      });
      fireEvent.timeUpdate(video);
      expect(hero).toHaveAttribute("data-stage", stage);
    }
  });

  it("keeps the glass shell over the video-filled wordmark on scroll", () => {
    expect(getLandingScrollProgress(0, 0, 2300, 1000)).toBe(0);
    expect(getLandingScrollProgress(650, 0, 2300, 1000)).toBe(0.5);
    expect(getLandingScrollProgress(1300, 0, 2300, 1000)).toBe(1);
    expect(getLandingScrollProgress(1700, 0, 2300, 1000)).toBe(1);

    const start = getLandingMorphState(0);
    const middle = getLandingMorphState(0.5);
    const end = getLandingMorphState(1);

    expect(start.fullOpacity).toBe(1);
    expect(start.logoOpacity).toBe(0);
    expect(start.glassOpacity).toBe(1);
    expect(start.wordmarkOpacity).toBe(1);
    expect(middle.logoOpacity).toBeGreaterThan(0);
    expect(middle.glassOpacity).toBe(1);
    expect(middle.wordmarkOpacity).toBeLessThan(1);
    expect(middle.logoOpacity + middle.wordmarkOpacity).toBeCloseTo(1);
    expect(middle.fullScale).toBeLessThan(1);
    expect(end.fullOpacity).toBe(0);
    expect(end.logoOpacity).toBe(1);
    expect(end.glassOpacity).toBe(1);
    expect(end.wordmarkOpacity).toBe(0);
    expect(end.logoScale).toBe(1);
  });

  it("converts screen-pixel drift into the scaled SVG mask coordinate space", () => {
    const logoWidth = 585.34;
    const svgScale = logoWidth / 97.4;

    expect(getLogoMaskMotionUnit(logoWidth) * svgScale).toBeCloseTo(1);
  });

  it("maps the real product recording to the matching journey copy", () => {
    expect(getProductDemoStep(0)).toBe(0);
    expect(getProductDemoStep(2.2)).toBe(1);
    expect(getProductDemoStep(5.8)).toBe(2);
    expect(getProductDemoStep(9.8)).toBe(3);
  });

  it("renders only the generated reel and primary CTA for launch", () => {
    const { container } = render(<KriaLifeLanding />);

    expect(
      screen.getByRole("heading", {
        level: 1,
        name: /kria turns the moments in your camera roll/i,
      }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create a video" })).toHaveAttribute(
      "href",
      "/plan",
    );
    expect(screen.getAllByRole("link", { name: "Create a video" })).toHaveLength(1);
    expect(screen.queryByText(/film your life/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /see how it works/i })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", {
        level: 2,
        name: /from prompt to final cut/i,
      }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("contentinfo")).not.toBeInTheDocument();
    const videos = container.querySelectorAll("video");
    const video = videos[0];
    expect(videos).toHaveLength(2);
    expect(videos[1]).toHaveAttribute("src", "/landing/life/hero-loop-v5.mp4");
    expect(videos[1]).toHaveAttribute("loop");
    expect(videos[1]).toHaveProperty("muted", true);
    expect(video).toHaveAttribute("src", "/landing/life/hero-loop-v5.mp4");
    expect(video).toHaveAttribute("poster", "/landing/life/hero-poster-v5.jpg");
    expect(video).toHaveAttribute("loop");
    expect(video).toHaveProperty("muted", true);
    expect(
      container.querySelector('video[src="/landing/product-demo/kria-product-flow.mp4"]'),
    ).not.toBeInTheDocument();
    expect(
      container.querySelector(
        'use[href="/landing/life/kria-wordmark-white-outlined.svg#font_3_205"]',
      ),
    ).toBeInTheDocument();
    expect(container.querySelectorAll("svg use")).toHaveLength(8);
    expect(container.querySelector("foreignObject[mask]")).toBeInTheDocument();
    expect(container.querySelectorAll('mask [class*="logoMaskLetter"]')).toHaveLength(4);
    expect(container.querySelectorAll('[class*="glassLetter"]')).toHaveLength(4);
    for (const letter of ["K", "R", "I", "A"]) {
      expect(container.querySelectorAll(`[class*="letterFloat${letter}"]`)).toHaveLength(2);
    }
    expect(container.querySelector('[class*="glassWordmark"]')).toHaveAttribute(
      "aria-hidden",
      "true",
    );
    expect(container.querySelector("svg text")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Play hero reel" })).toBeInTheDocument();
    fireEvent.playing(video);
    expect(screen.getByRole("button", { name: "Pause hero reel" })).toBeInTheDocument();
    fireEvent.waiting(video);
    expect(screen.getByRole("button", { name: "Pause hero reel" })).toBeInTheDocument();

  });

  it("advances the reveal stage from the video timeline", () => {
    const { container } = render(<KriaLifeLanding />);
    const video = container.querySelector("video")!;
    Object.defineProperty(video, "currentTime", {
      configurable: true,
      value: 1.4,
    });

    fireEvent.timeUpdate(video);

    expect(
      container.querySelector('section[data-stage="wordmark"]'),
    ).toBeInTheDocument();
  });

  it("pins the completed lockup after completion or media failure", () => {
    const { container } = render(<KriaLifeLanding />);
    const video = container.querySelector("video")!;
    const hero = container.querySelector("section[data-stage]")!;

    Object.defineProperty(video, "currentTime", {
      configurable: true,
      value: 2.9,
    });
    fireEvent.timeUpdate(video);
    expect(hero).toHaveAttribute("data-stage", "complete");
    expect(hero).toHaveAttribute("data-payoff-visible", "true");

    Object.defineProperty(video, "currentTime", {
      configurable: true,
      value: 0,
    });
    fireEvent.timeUpdate(video);
    expect(hero).toHaveAttribute("data-stage", "complete");

    fireEvent.error(video);
    expect(hero).toHaveAttribute("data-static", "true");
  });

  it("shows the completed static landing state when reduced motion is requested", () => {
    const pause = HTMLMediaElement.prototype.pause as jest.Mock;
    pause.mockClear();
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: jest.fn().mockReturnValue({
        matches: true,
        media: "(prefers-reduced-motion: reduce)",
        addEventListener: jest.fn(),
        removeEventListener: jest.fn(),
      }),
    });

    const { container } = render(<KriaLifeLanding />);
    const hero = container.querySelector("section[data-stage]")!;

    expect(hero).toHaveAttribute("data-reduced-motion", "true");
    expect(hero).toHaveAttribute("data-stage", "complete");
    expect(hero).toHaveAttribute("data-playing", "false");
    expect(hero).toHaveAttribute("data-payoff-visible", "true");
    expect(pause).toHaveBeenCalled();
  });

  it("lets visitors pause and resume the synchronized hero reel", async () => {
    const { container } = render(<KriaLifeLanding />);
    const video = container.querySelector("video")!;
    let paused = false;
    Object.defineProperty(video, "paused", {
      configurable: true,
      get: () => paused,
    });

    fireEvent.playing(video);
    fireEvent.click(screen.getByRole("button", { name: "Pause hero reel" }));
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Play hero reel" })).toBeInTheDocument();
    expect(container.querySelector("section[data-stage]")).toHaveAttribute(
      "data-payoff-visible",
      "true",
    );

    paused = true;
    fireEvent.click(screen.getByRole("button", { name: "Play hero reel" }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Pause hero reel" })).toBeInTheDocument();
    });
  });

  it("starts the masked reel when the glass build begins", () => {
    const play = HTMLMediaElement.prototype.play as jest.Mock;
    const { container } = render(<KriaLifeLanding />);
    const video = container.querySelector("video")!;
    Object.defineProperty(video, "paused", {
      configurable: true,
      get: () => false,
    });
    Object.defineProperty(video, "currentTime", {
      configurable: true,
      value: 0.8,
    });

    fireEvent.timeUpdate(video);

    expect(play).toHaveBeenCalledTimes(2);
    expect(container.querySelector("section[data-stage]")).toHaveAttribute(
      "data-stage",
      "wordmark",
    );
  });

  it("starts the masked reel on scroll if the build has not started", () => {
    const play = HTMLMediaElement.prototype.play as jest.Mock;
    const { container } = render(<KriaLifeLanding />);
    const video = container.querySelector("video")!;
    const section = container.querySelector("section[data-stage]")!;
    Object.defineProperty(video, "paused", {
      configurable: true,
      get: () => false,
    });
    Object.defineProperty(section, "offsetTop", { configurable: true, value: 0 });
    Object.defineProperty(section, "offsetHeight", { configurable: true, value: 200 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 100 });
    Object.defineProperty(window, "scrollY", { configurable: true, value: 100 });
    jest.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callback(0);
      return 1;
    });

    fireEvent.scroll(window);

    expect(play).toHaveBeenCalledTimes(2);
  });

  it("uses a native scroll timeline without registering a scroll handler", () => {
    (window.CSS.supports as jest.Mock).mockReturnValue(true);
    const addEventListener = jest.spyOn(window, "addEventListener");

    const { container } = render(<KriaLifeLanding />);

    expect(container.querySelector("section[data-stage]")).toHaveAttribute(
      "data-scroll-driver",
      "native",
    );
    expect(addEventListener).not.toHaveBeenCalledWith(
      "scroll",
      expect.any(Function),
      expect.anything(),
    );
  });

  it("uses the completed static fallback when initial autoplay is rejected", async () => {
    const play = HTMLMediaElement.prototype.play as jest.Mock;
    play.mockRejectedValueOnce(new Error("autoplay blocked"));

    const { container } = render(<KriaLifeLanding />);

    await waitFor(() => {
      const hero = container.querySelector("section[data-stage]")!;
      expect(hero).toHaveAttribute("data-static", "true");
      expect(hero).toHaveAttribute("data-stage", "complete");
      expect(hero).toHaveAttribute("data-payoff-visible", "true");
    });
  });

  it("falls back to the completed static state when user-initiated playback is rejected", async () => {
    const play = HTMLMediaElement.prototype.play as jest.Mock;
    play
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error("blocked"));
    const { container } = render(<KriaLifeLanding />);
    const video = container.querySelector("video")!;
    Object.defineProperty(video, "paused", {
      configurable: true,
      get: () => true,
    });

    fireEvent.click(screen.getByRole("button", { name: "Play hero reel" }));

    await waitFor(() => {
      expect(container.querySelector("section[data-stage]")).toHaveAttribute("data-static", "true");
    });
  });

  it("uses the static completed fallback if the masked reel cannot load", () => {
    const { container } = render(<KriaLifeLanding />);
    const videos = container.querySelectorAll("video");

    fireEvent.error(videos[1]);

    const hero = container.querySelector("section[data-stage]")!;
    expect(hero).toHaveAttribute("data-static", "true");
    expect(hero).toHaveAttribute("data-stage", "complete");
    expect(hero).toHaveAttribute("data-payoff-visible", "true");
  });
});
