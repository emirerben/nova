import "@testing-library/jest-dom";
import React from "react";
import { render } from "@testing-library/react";

import EditorCanvas, {
  editorCanvasStageStyle,
} from "@/app/plan/items/[id]/_editor/EditorCanvas";
import { createEditorCanvasVirtualPreview } from "@/app/dev-qa/editor-canvas-geometry/virtual-preview-fixture";
import type { VirtualPreviewController } from "@/app/plan/items/[id]/_editor/useVirtualPreview";
import type { LookAdjustments, LookPreset } from "@/lib/generative-api";
import type { PlanItemVariant } from "@/lib/plan-api";

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

const variant = {
  variant_id: "song_text",
  output_url: "https://example.com/portrait-output.mp4",
  render_status: "ready",
  text_mode: "agent_text",
} as unknown as PlanItemVariant;

function editorCanvas(
  canvas: { w: number; h: number },
  virtualPreview: VirtualPreviewController | null = null,
  lookPreset: LookPreset = "none",
  lookAdjustments: LookAdjustments | null = null,
  stageHeightCss?: string,
  zoomPct = 100,
) {
  return (
    <EditorCanvas
      variant={variant}
      elements={[]}
      bars={[]}
      selectedTextId={null}
      currentTime={0}
      lookPreset={lookPreset}
      lookAdjustments={lookAdjustments}
      masonryDurationS={8}
      zoomPct={zoomPct}
      tool="select"
      videoRef={React.createRef<HTMLVideoElement>()}
      onSelectText={jest.fn()}
      onClearSelection={jest.fn()}
      onPatchBar={jest.fn()}
      onFocusContent={jest.fn()}
      onTimeUpdate={jest.fn()}
      onDuration={jest.fn()}
      canvas={canvas}
      virtualPreview={virtualPreview}
      stageHeightCss={stageHeightCss}
    />
  );
}

function virtualPreview(): VirtualPreviewController {
  return createEditorCanvasVirtualPreview(jest.fn());
}

describe("EditorCanvas orientation video fit", () => {
  it("uses width-driven aspect-ratio sizing for portrait and landscape stages", () => {
    expect(editorCanvasStageStyle({ w: 1080, h: 1920 }, "100dvh - 152px", 100)).toEqual({
      width: "calc(max(1px, (100dvh - 152px)) * 1 * 0.5625)",
      aspectRatio: "1080 / 1920",
      maxWidth: "100%",
    });
    expect(editorCanvasStageStyle({ w: 1920, h: 1080 }, "100dvh - 152px", 100)).toEqual({
      width: "calc(max(1px, (100dvh - 152px)) * 1 * 1.7777777777777777)",
      aspectRatio: "1920 / 1080",
      maxWidth: "100%",
    });

    const view = render(
      editorCanvas({ w: 1080, h: 1920 }, null, "none", null, "100dvh - 152px"),
    );
    const stage = view.getByTestId("editor-canvas-stage");
    expect(stage.style.height).toBe("");
    expect(stage.style.aspectRatio).toBe("1080 / 1920");
  });

  it("supports fallback height, zoom, and a positive short-viewport floor", () => {
    expect(editorCanvasStageStyle({ w: 1080, h: 1920 }, undefined, 100).width).toBe(
      "calc(max(1px, (100vh - 56px - 260px - 48px)) * 1 * 0.5625)",
    );
    expect(editorCanvasStageStyle({ w: 1080, h: 1920 }, "100dvh - 398px", 150).width).toBe(
      "calc(max(1px, (100dvh - 398px)) * 1.5 * 0.5625)",
    );
    expect(editorCanvasStageStyle({ w: 1080, h: 1920 }, "100dvh - 398px", 200).width).toBe(
      "calc(max(1px, (100dvh - 398px)) * 2 * 0.5625)",
    );
  });

  it("uses the same stage geometry for clean and virtual preview", () => {
    const clean = render(editorCanvas({ w: 1080, h: 1920 }, null, "none", null, "100dvh - 350px", 200));
    const cleanStage = clean.getByTestId("editor-canvas-stage");
    const cleanAspectRatio = cleanStage.style.aspectRatio;

    clean.rerender(
      editorCanvas({ w: 1080, h: 1920 }, virtualPreview(), "none", null, "100dvh - 350px", 200),
    );
    expect(clean.getByTestId("editor-canvas-stage").style.aspectRatio).toBe(cleanAspectRatio);
  });

  it("preserves portrait contain and switches the rendered video to landscape cover", () => {
    const view = render(editorCanvas({ w: 1080, h: 1920 }));
    const video = view.container.querySelector("video");

    expect(video).toHaveClass("object-contain");
    expect(video).not.toHaveClass("object-cover");

    view.rerender(editorCanvas({ w: 1920, h: 1080 }));

    expect(view.container.querySelector("video")).toHaveClass("object-cover");
    expect(view.container.querySelector("video")).not.toHaveClass("object-contain");
  });

  it("applies landscape cover to both virtual-preview decks", () => {
    const view = render(editorCanvas({ w: 1920, h: 1080 }, virtualPreview()));
    const decks = view.container.querySelectorAll("video[data-virtual-preview-deck]");

    expect(decks).toHaveLength(2);
    decks.forEach((deck) => {
      expect(deck).toHaveClass("object-cover");
      expect(deck).not.toHaveClass("object-contain");
    });
  });

  it("composes customizable video, tint, and grain preview layers", () => {
    const controls: LookAdjustments = {
      intensity: 0.8,
      warmth: 0.1,
      contrast: -0.1,
      grain: 0.3,
      vignette: 0.4,
    };
    const view = render(editorCanvas({ w: 1080, h: 1920 }, null, "olive_film", controls));
    const video = view.container.querySelector("video");

    expect(video?.style.filter).toContain("sepia(");
    expect(
      view.container.querySelector('[data-look-preview-layer="tint"]'),
    ).toHaveAttribute("data-look-preview-preset", "olive_film");
    expect(
      view.container.querySelector('[data-look-preview-layer="grain"]'),
    ).toHaveAttribute("data-look-preview-preset", "olive_film");

    view.rerender(editorCanvas({ w: 1080, h: 1920 }));
    expect(view.container.querySelector("video")?.style.filter).toBe("");
    expect(view.container.querySelector("[data-look-preview-layer]")).toBeNull();
  });
});
