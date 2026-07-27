/**
 * The canvas caption preview must honour the SAME precedence the inspector
 * shows: per-cue override first, then the variant-wide value.
 *
 * Guards the dead-control bug: the "This caption" Font / Color / Size fields
 * write `cue_font_family` / `cue_text_color` / `cue_size_px`, but the preview
 * used to read only `font_family` / `color` / `size_px`. The controls changed
 * state the user could never see, so they read as broken.
 */

import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";

import EditorCanvas from "@/app/plan/items/[id]/_editor/EditorCanvas";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import type { PlanItemVariant } from "@/lib/plan-api";

/**
 * jsdom has no layout, so the real observer would report h=0 and the preview's
 * stage-scaled `fontSizePx` would collapse to 0 for every case — making size
 * untestable. Report a fixed stage box instead so scaled sizes are comparable.
 */
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

const variant = {
  variant_id: "subtitled",
  resolved_archetype: "subtitled",
  base_video_url: "https://example.com/base.mp4",
  render_status: "ready",
  text_mode: "none",
  captions_enabled: true,
  caption_size_px: 78,
  caption_text_color: "#FFFFFF",
  voiceover_caption_font: "Playfair Display",
} as unknown as PlanItemVariant;

function captionBar(overrides: Partial<TextElementBar> = {}): TextElementBar {
  return {
    id: "caption-0",
    role: "narrated_caption",
    text: "Pınar Beyaz,",
    start_s: 0,
    end_s: 4,
    ...overrides,
  };
}

function renderCanvas(bar: TextElementBar) {
  render(
    <EditorCanvas
      variant={variant}
      elements={[]}
      bars={[bar]}
      selectedTextId={null}
      currentTime={1}
      masonryDurationS={8}
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
  const preview = document.querySelector('[data-caption-preview="true"]');
  return preview?.firstElementChild as HTMLElement | undefined;
}

describe("canvas caption preview — per-cue overrides", () => {
  it("renders the caption at all", () => {
    renderCanvas(captionBar());
    expect(screen.getByText("Pınar Beyaz,")).toBeInTheDocument();
  });

  it("uses the per-cue colour override, not the variant colour", () => {
    const el = renderCanvas(captionBar({ color: "#FFFFFF", cue_text_color: "#602E2E" }));
    expect(el?.style.color).toBe("rgb(96, 46, 46)");
  });

  it("falls back to the variant colour when no override is set", () => {
    const el = renderCanvas(captionBar({ color: "#00FF00" }));
    expect(el?.style.color).toBe("rgb(0, 255, 0)");
  });

  it("uses the per-cue size override, not the variant size", () => {
    const withOverride = renderCanvas(captionBar({ size_px: 78, cue_size_px: 150 }));
    const overriddenPx = parseFloat(withOverride?.style.fontSize ?? "0");
    document.body.innerHTML = "";
    const withoutOverride = renderCanvas(captionBar({ size_px: 78 }));
    const basePx = parseFloat(withoutOverride?.style.fontSize ?? "0");
    // Guard the guard: a 0 here would make the comparison vacuously meaningless.
    expect(basePx).toBeGreaterThan(0);
    // Same stage factor both times, so the override must render strictly larger.
    expect(overriddenPx).toBeGreaterThan(basePx);
    expect(overriddenPx / basePx).toBeCloseTo(150 / 78, 2);
  });

  it("uses the per-cue font override, not the variant font", () => {
    const el = renderCanvas(
      captionBar({ font_family: "Playfair Display", cue_font_family: "Montserrat Bold" }),
    );
    document.body.innerHTML = "";
    const base = renderCanvas(captionBar({ font_family: "Playfair Display" }));
    expect(el?.style.fontFamily).not.toBe(base?.style.fontFamily);
  });
});
