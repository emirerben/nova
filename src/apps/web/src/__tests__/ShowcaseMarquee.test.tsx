/**
 * Unit tests for ShowcaseMarquee.
 *
 * Covers:
 *  1. Desktop autoplay — all visible cards with `src` get play() called.
 *  2. Mobile single-play — only the most-visible card plays.
 *  3. Copy — "created with nova" rendered; clip title NOT visible as tile text.
 */
import "@testing-library/jest-dom";
import { render, act } from "@testing-library/react";
import ShowcaseMarquee, { ShowcaseClip } from "@/components/ShowcaseMarquee";

// ── IntersectionObserver mock ─────────────────────────────────────────────────

type IOCallback = (entries: Partial<IntersectionObserverEntry>[]) => void;

// Registry: observed element → IO callback.  Populated during observe().
const elementCallbacks = new Map<Element, IOCallback>();

beforeEach(() => {
  elementCallbacks.clear();

  global.IntersectionObserver = jest
    .fn()
    .mockImplementation((cb: IOCallback) => ({
      observe: jest.fn((el: Element) => {
        elementCallbacks.set(el, cb);
      }),
      unobserve: jest.fn(),
      disconnect: jest.fn(),
    })) as unknown as typeof IntersectionObserver;

  // Stub prototype play/pause so jsdom doesn't throw.
  // Instance-level mocks are installed after each render via installVideoMocks().
  Object.defineProperty(HTMLMediaElement.prototype, "play", {
    configurable: true,
    writable: true,
    value: jest.fn().mockResolvedValue(undefined),
  });
  Object.defineProperty(HTMLMediaElement.prototype, "pause", {
    configurable: true,
    writable: true,
    value: jest.fn(),
  });
});

afterEach(() => {
  jest.restoreAllMocks();
});

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Install per-instance play/pause mocks so assertions are per-element. */
function installVideoMocks(container: HTMLElement) {
  const videos = Array.from(container.querySelectorAll("video"));
  videos.forEach((v) => {
    Object.defineProperty(v, "play", {
      configurable: true,
      value: jest.fn().mockResolvedValue(undefined),
    });
    Object.defineProperty(v, "pause", {
      configurable: true,
      value: jest.fn(),
    });
  });
  return videos;
}

/** Fire an intersection event for a specific element. */
function fireIntersection(el: Element, intersecting: boolean) {
  const cb = elementCallbacks.get(el);
  if (!cb) return;
  cb([
    {
      target: el,
      isIntersecting: intersecting,
      intersectionRatio: intersecting ? 0.9 : 0,
    } as Partial<IntersectionObserverEntry>,
  ]);
}

/** Fire intersection events for ALL observed elements. */
function fireAll(intersecting: boolean) {
  Array.from(elementCallbacks.entries()).forEach(([el, cb]) => {
    cb([
      {
        target: el,
        isIntersecting: intersecting,
        intersectionRatio: intersecting ? 0.9 : 0,
      } as Partial<IntersectionObserverEntry>,
    ]);
  });
}

// ── Fixtures ──────────────────────────────────────────────────────────────────

const CLIPS: ShowcaseClip[] = [
  {
    title: "a week of mornings",
    from: "#111",
    to: "#000",
    src: "https://example.com/a.mp4",
  },
  {
    title: "med student eats",
    from: "#222",
    to: "#000",
    src: "https://example.com/b.mp4",
  },
  { title: "gallery show", from: "#333", to: "#000" }, // no src — gradient only
];

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("ShowcaseMarquee — desktop (≥768 px)", () => {
  beforeEach(() => {
    window.matchMedia = jest.fn().mockImplementation((query: string) => ({
      matches:
        query === "(min-width: 768px)" ||
        query !== "(prefers-reduced-motion: reduce)",
      media: query,
      addListener: jest.fn(),
      removeListener: jest.fn(),
    }));
  });

  it("plays all visible cards with src simultaneously", async () => {
    const { container } = render(<ShowcaseMarquee clips={CLIPS} />);
    // Two clips have `src` → two video elements.
    const videos = installVideoMocks(container);
    expect(videos).toHaveLength(2);

    // Simulate both cards entering the viewport.
    await act(async () => {
      fireAll(true);
    });

    // Both videos should have play() called.
    videos.forEach((v) => {
      expect(v.play).toHaveBeenCalled();
    });
    // Neither should be paused (they entered — no leave event fired).
    videos.forEach((v) => {
      expect(v.pause).not.toHaveBeenCalled();
    });
  });

  it("pauses only the card that leaves the viewport", async () => {
    const { container } = render(<ShowcaseMarquee clips={CLIPS} />);
    const videos = installVideoMocks(container);

    // Both enter.
    await act(async () => {
      fireAll(true);
    });

    // Reset counts so we only see calls from the leave event.
    videos.forEach((v) => {
      (v.play as ReturnType<typeof jest.fn>).mockClear();
      (v.pause as ReturnType<typeof jest.fn>).mockClear();
    });

    // First card leaves the viewport.
    const firstEl = Array.from(elementCallbacks.keys())[0];
    await act(async () => {
      fireIntersection(firstEl, false);
    });

    // First video gets paused.
    expect(videos[0].pause).toHaveBeenCalled();
    // Second video stays playing — no pause called on it.
    expect(videos[1].pause).not.toHaveBeenCalled();
    // Second video may get play() called again as the effect re-runs; that's fine.
  });
});

describe("ShowcaseMarquee — mobile (<768 px)", () => {
  beforeEach(() => {
    window.matchMedia = jest.fn().mockImplementation((query: string) => ({
      matches:
        query !== "(min-width: 768px)" &&
        query !== "(prefers-reduced-motion: reduce)",
      media: query,
      addListener: jest.fn(),
      removeListener: jest.fn(),
    }));
  });

  it("plays only the single most-visible card", async () => {
    const { container } = render(<ShowcaseMarquee clips={CLIPS} />);
    const videos = installVideoMocks(container);

    const entries = Array.from(elementCallbacks.entries());

    // First card becomes visible.
    await act(async () => {
      const [el] = entries[0];
      fireIntersection(el, true);
    });
    expect(videos[0].play).toHaveBeenCalled();
    // Second video is paused because playing set switched to only {0}.
    expect(videos[1].pause).toHaveBeenCalled();

    // Reset counts.
    videos.forEach((v) => {
      (v.play as ReturnType<typeof jest.fn>).mockClear();
      (v.pause as ReturnType<typeof jest.fn>).mockClear();
    });

    // Second card becomes visible (playing set switches to {1}).
    await act(async () => {
      const [el] = entries[1];
      fireIntersection(el, true);
    });
    expect(videos[1].play).toHaveBeenCalled();
    // First video is paused since it's no longer the active card.
    expect(videos[0].pause).toHaveBeenCalled();
  });
});

describe("ShowcaseMarquee — copy", () => {
  beforeEach(() => {
    window.matchMedia = jest.fn().mockReturnValue({
      matches: false,
      media: "",
      addListener: jest.fn(),
      removeListener: jest.fn(),
    });
  });

  it("renders 'created with nova' credit on each tile", () => {
    const { getAllByText } = render(<ShowcaseMarquee clips={CLIPS} />);
    const credits = getAllByText(/created with nova/i);
    // One per clip (three tiles).
    expect(credits.length).toBe(CLIPS.length);
  });

  it("does NOT render clip title as visible tile text", () => {
    const { queryByText } = render(<ShowcaseMarquee clips={CLIPS} />);
    // Titles feed aria-label + React key, but NOT rendered as text spans.
    CLIPS.forEach((c) => {
      expect(queryByText(c.title)).toBeNull();
    });
  });
});
