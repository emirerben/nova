/**
 * IntroTextPreview — the 0-latency DOM overlay.
 *
 * Uses a FIRING ResizeObserver mock: the component renders a measuring shell
 * until it learns its width, and the contentEditable node only mounts after
 * that. (Regression B2: with an inert RO mock the node never mounts in jsdom,
 * so the text-initialization path was untested — and broken: an effect keyed
 * on [text] had already no-oped before the node existed, leaving the editor
 * EMPTY over the persisted hook text; typing then replaced the whole text.)
 */

import { act, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { IntroTextPreview } from "@/components/variant-editor/IntroTextPreview";
import type { IntroOverlayParams } from "@/lib/overlay-layout";

// ResizeObserver mock that immediately reports a 270px-wide box.
class FiringResizeObserver {
  private cb: ResizeObserverCallback;
  constructor(cb: ResizeObserverCallback) {
    this.cb = cb;
  }
  observe() {
    act(() => {
      this.cb(
        [{ contentRect: { width: 270 } } as unknown as ResizeObserverEntry],
        this as unknown as ResizeObserver,
      );
    });
  }
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof FiringResizeObserver }).ResizeObserver =
  FiringResizeObserver;

const params: IntroOverlayParams = {
  text: "hello world",
  effect: "karaoke-line",
  textColor: "#FFFFFF",
  highlightColor: "#FFD24A",
  fontFamily: "Playfair Display",
  textSizePx: 60,
  position: "center",
  positionXFrac: null,
  positionYFrac: null,
  textAnchor: "center",
  strokeWidth: 0,
};

describe("IntroTextPreview", () => {
  it("initializes the editable node with the persisted text once mounted", () => {
    render(<IntroTextPreview params={params} editable onTextChange={jest.fn()} />);
    const box = screen.getByRole("textbox", { name: /intro text/i });
    expect(box.textContent).toBe("hello world");
  });

  it("applies the settled karaoke color and registry font", () => {
    render(<IntroTextPreview params={params} />);
    const node = document.querySelector('[data-placeholder]') as HTMLElement;
    expect(node).not.toBeNull();
    expect(node.style.color).toBe("rgb(255, 210, 74)"); // #FFD24A settled hold
    expect(node.style.fontFamily).toContain("Playfair Display");
  });

  it("scales the font to the container (270/1080 → ×0.25)", () => {
    render(<IntroTextPreview params={params} />);
    const node = document.querySelector('[data-placeholder]') as HTMLElement;
    expect(node.style.fontSize).toBe("15px"); // 60px × 0.25
  });

  it("renders nothing for empty text when not editable", () => {
    render(<IntroTextPreview params={{ ...params, text: "" }} />);
    expect(document.querySelector('[data-placeholder]')).toBeNull();
  });

  it("keeps an empty editable node mounted (placeholder hit target)", () => {
    render(<IntroTextPreview params={{ ...params, text: "" }} editable />);
    const box = screen.getByRole("textbox", { name: /intro text/i });
    expect(box).toBeInTheDocument();
    expect(box.textContent).toBe("");
  });

  it("syncs external text changes into the node when not focused", () => {
    const { rerender } = render(<IntroTextPreview params={params} editable />);
    rerender(<IntroTextPreview params={{ ...params, text: "second draft" }} editable />);
    const box = screen.getByRole("textbox", { name: /intro text/i });
    expect(box.textContent).toBe("second draft");
  });

  // Cluster decline → linear fallback. The editorial cluster engine returns null
  // when the hook is outside the 3-6 word range (or empty), and the server
  // renders the LINEAR intro in exactly that case. The preview must follow, or
  // it goes blank while the burn comes back with visible linear text.
  it("falls back to the linear preview when a cluster hook exceeds 6 words", () => {
    render(
      <IntroTextPreview
        layout="cluster"
        params={{ ...params, text: "one two three four five six seven" }}
        editable
        onTextChange={jest.fn()}
      />,
    );
    const box = screen.getByRole("textbox", { name: /intro text/i });
    expect(box.textContent).toBe("one two three four five six seven");
  });

  it("falls back to the linear preview for an empty cluster hook (no blank overlay)", () => {
    render(<IntroTextPreview layout="cluster" params={{ ...params, text: "" }} editable />);
    const box = screen.getByRole("textbox", { name: /intro text/i });
    expect(box).toBeInTheDocument();
    expect(box.textContent).toBe("");
  });
});
