/**
 * The canvas must play the CLEAN (text-free) base whenever the variant has one.
 *
 * Load-bearing because the canvas draws every text element as a DOM layer on top
 * of whatever it plays. Prefer `output_url` and that layer lands on pixels that
 * already contain the same words — the intro renders twice, one copy draggable
 * and one not. That is exactly what talking_head shipped as: it never cached a
 * pre-burn composite, so `base_video_url` was always absent and `src` fell
 * through to the burned output (fixed in generative_build.py; pinned by
 * tests/tasks/test_generative_dispatch.py).
 *
 * The fallback itself stays deliberate — a variant with no base (e.g. a
 * `text_mode: "none"` montage the user is adding fresh text to) still needs
 * something to play.
 */

import "@testing-library/jest-dom";
import React from "react";
import { render } from "@testing-library/react";

import EditorCanvas from "@/app/plan/items/[id]/_editor/EditorCanvas";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import type { PlanItemVariant, TextElement } from "@/lib/plan-api";

const STAGE = { width: 405, height: 720 };
class ResizeObserverMock {
  constructor(private cb: ResizeObserverCallback) {}
  observe() {
    this.cb(
      [{ contentRect: STAGE } as unknown as ResizeObserverEntry],
      this as unknown as ResizeObserver,
    );
  }
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

const OUTPUT_URL = "https://example.com/variant_1_talking_head.mp4";
const BASE_URL = "https://example.com/base_1_talking_head.mp4";

const element: TextElement = {
  id: "intro-1",
  text: "the unexpected ending you might be missing",
  start_s: 0,
  end_s: 12,
  role: "generative_intro",
  position: "middle",
  size_px: 69,
};

const bar = {
  id: "intro-1",
  role: "generative_intro",
  text: element.text,
  start_s: 0,
  end_s: 12,
} as TextElementBar;

function renderCanvas(variantOverrides: Partial<PlanItemVariant>) {
  const variant = {
    variant_id: "talking_head",
    resolved_archetype: "talking_head",
    render_status: "ready",
    text_mode: "agent_text",
    output_url: OUTPUT_URL,
    ...variantOverrides,
  } as unknown as PlanItemVariant;

  const { container } = render(
    <EditorCanvas
      variant={variant}
      elements={[element]}
      bars={[bar]}
      selectedTextId={null}
      currentTime={1}
      masonryDurationS={12}
      zoomPct={100}
      tool="select"
      videoRef={React.createRef<HTMLVideoElement>()}
      onSelectText={jest.fn()}
      onClearSelection={jest.fn()}
      onPatchBar={jest.fn()}
      onFocusContent={jest.fn()}
      onTimeUpdate={jest.fn()}
      onDuration={jest.fn()}
      canvas={{ w: 1080, h: 1920 }}
    />,
  );
  return container;
}

describe("canvas playback source", () => {
  it("plays the clean base, not the burned output, when both are present", () => {
    const container = renderCanvas({ base_video_url: BASE_URL });
    expect(container.querySelector("video")?.getAttribute("src")).toBe(BASE_URL);
    // The DOM text layer is the only copy of this text on screen.
    expect(container.querySelector('[data-text-id="intro-1"]')).toBeInTheDocument();
  });

  it("falls back to the output only when there is no base", () => {
    const container = renderCanvas({});
    expect(container.querySelector("video")?.getAttribute("src")).toBe(OUTPUT_URL);
  });
});
