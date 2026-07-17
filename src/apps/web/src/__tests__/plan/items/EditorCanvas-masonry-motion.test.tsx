import "@testing-library/jest-dom";
import React from "react";
import { render } from "@testing-library/react";

import EditorCanvas from "@/app/plan/items/[id]/_editor/EditorCanvas";
import type { PlanItemVariant, TextElement } from "@/lib/plan-api";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

const bar: TextElementBar = {
  id: "title",
  role: "generative_intro",
  text: "White pocket",
  start_s: 0,
  end_s: 8,
  x_frac: 0.8602,
  y_frac: 0.9359,
  max_width_frac: 0.2,
  position: "custom",
  size_px: 64,
};

const element = {
  ...bar,
  source_params: bar.source_params,
} as unknown as TextElement;

const variant = {
  variant_id: "song_text",
  output_url: "https://example.com/output.mp4",
  render_status: "ready",
  text_mode: "agent_text",
  montage_preset_rendered: "masonry",
} as unknown as PlanItemVariant;

function canvas(currentTime: number, currentVariant = variant) {
  return (
    <EditorCanvas
      variant={currentVariant}
      elements={[element]}
      bars={[bar]}
      selectedTextId={null}
      currentTime={currentTime}
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
    />
  );
}

describe("EditorCanvas masonry board motion", () => {
  it("matches final-render pan math at 0:00 and 0:02", () => {
    const view = render(canvas(0));
    const overlay = view.container.querySelector<HTMLElement>("[data-text-id='title']");
    expect(overlay).not.toBeNull();
    expect(parseFloat(overlay?.style.left ?? "NaN")).toBeCloseTo(86.02, 6);

    view.rerender(canvas(2));
    const moved = view.container.querySelector<HTMLElement>("[data-text-id='title']");
    expect(parseFloat(moved?.style.left ?? "NaN")).toBeCloseTo(
      (0.8602 - 233 / 1080) * 100,
      6,
    );
  });

  it("ignores stale masonry motion on non-masonry variants", () => {
    const nonMasonry = {
      ...variant,
      montage_preset_rendered: "standard",
    } as unknown as PlanItemVariant;
    const view = render(canvas(2, nonMasonry));
    const overlay = view.container.querySelector<HTMLElement>("[data-text-id='title']");

    expect(parseFloat(overlay?.style.left ?? "NaN")).toBeCloseTo(86.02, 6);
  });

  it("uses the current Polaroid board width and preview duration", () => {
    const polaroid = {
      ...variant,
      montage_preset_rendered: "polaroid_wall",
      text_placement_candidates: [
        {
          source: "polaroid_wall_whitespace",
          x_frac: 0.8,
          y_frac: 0.8,
          max_width_frac: 0.2,
          masonry_motion: { board_width_px: 2366 },
        },
      ],
    } as unknown as PlanItemVariant;
    const view = render(canvas(2, polaroid));
    const overlay = view.container.querySelector<HTMLElement>("[data-text-id='title']");

    expect(parseFloat(overlay?.style.left ?? "NaN")).toBeCloseTo(
      (0.8602 - 321.5 / 1080) * 100,
      6,
    );
  });
});
