import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render } from "@testing-library/react";

import EditorCanvas, {
  editorCanvasStageStyle,
} from "@/app/plan/items/[id]/_editor/EditorCanvas";
import { createEditorCanvasVirtualPreview } from "@/app/dev-qa/editor-canvas-geometry/virtual-preview-fixture";
import type { VirtualTimelineEntry } from "@/app/plan/items/[id]/_editor/virtual-timeline";
import type { VirtualPreviewController } from "@/app/plan/items/[id]/_editor/useVirtualPreview";
import type { LookAdjustments, LookPreset } from "@/lib/generative-api";
import type { CameraEffect, PlanItemVariant } from "@/lib/plan-api";

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;
Object.defineProperty(window.HTMLMediaElement.prototype, "play", {
  configurable: true,
  value: jest.fn().mockResolvedValue(undefined),
});
Object.defineProperty(window.HTMLMediaElement.prototype, "pause", {
  configurable: true,
  value: jest.fn(),
});

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
  currentTime = 0,
  cameraEffects: CameraEffect[] = [],
  playing = false,
) {
  return (
    <EditorCanvas
      variant={variant}
      elements={[]}
      bars={[]}
      selectedTextId={null}
      currentTime={currentTime}
      cameraEffects={cameraEffects}
      playing={playing}
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

type PreviewLayout = "fullscreen" | "supporting_card";

function virtualPreview(layout?: PreviewLayout): VirtualPreviewController {
  const preview = createEditorCanvasVirtualPreview(jest.fn());
  const entry = preview.timeline.entries[0];
  if (entry?.kind === "clip") {
    (entry as VirtualTimelineEntry & { layout?: PreviewLayout }).layout = layout;
    entry.sourceUrl = "https://example.com/landscape-source.mp4";
  }
  preview.timeline.hasMissingSource = false;
  return preview;
}

function mixedLayoutTransitionPreview(): VirtualPreviewController {
  const preview = virtualPreview("fullscreen");
  const outgoing = preview.timeline.entries[0] as VirtualTimelineEntry;
  outgoing.durationS = 2;
  outgoing.transitionAfter = "crossfade";
  outgoing.transitionDurationS = 0.2;
  preview.timeline.entries.push({
    ...outgoing,
    slotIndex: 1,
    slotKey: "supporting-card-slot",
    startS: 1.8,
    durationS: 2,
    sourceUrl: "https://example.com/supporting-card-source.mp4",
    transitionAfter: "cut",
    transitionDurationS: null,
    overlapBeforeS: 0.2,
    layout: "supporting_card",
  } as VirtualTimelineEntry & { layout: PreviewLayout });
  preview.timeline.totalDurationS = 3.8;
  return preview;
}

function supportingCardImagePreview(): VirtualPreviewController {
  const preview = virtualPreview("supporting_card");
  const entry = preview.timeline.entries[0];
  if (entry?.kind === "clip") {
    entry.mediaKind = "image";
    entry.sourceUrl = "https://example.com/landscape-source.jpg";
  }
  return preview;
}

function mixedLayoutImageTransitionPreview(): VirtualPreviewController {
  const preview = mixedLayoutTransitionPreview();
  for (const [index, entry] of preview.timeline.entries.entries()) {
    if (entry.kind !== "clip") continue;
    entry.mediaKind = "image";
    entry.sourceUrl = `https://example.com/image-${index}.jpg`;
  }
  return preview;
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

  it("crops fullscreen guided source media in a portrait virtual preview", () => {
    const view = render(editorCanvas({ w: 1080, h: 1920 }, virtualPreview("fullscreen")));
    const activeDeck = view.container.querySelector('video[data-virtual-preview-deck="a"]');

    expect(activeDeck).toHaveClass("object-cover");
    expect(activeDeck).not.toHaveClass("object-contain");
    expect(activeDeck).toHaveAttribute("data-virtual-preview-layout", "fullscreen");
    expect(view.getByTestId("editor-canvas-stage").style.aspectRatio).toBe("1080 / 1920");
  });

  it("renders supporting-card video with a contained foreground over a blurred backdrop", () => {
    const view = render(
      editorCanvas({ w: 1080, h: 1920 }, virtualPreview("supporting_card")),
    );
    const activeDeck = view.container.querySelector('video[data-virtual-preview-deck="a"]');

    expect(activeDeck).toHaveClass("object-contain");
    expect(activeDeck).toHaveAttribute("data-virtual-preview-layout", "supporting_card");
    expect(
      view.container.querySelector('video[data-virtual-preview-video-backdrop="a"]'),
    ).toBeInTheDocument();
  });

  it("applies camera pulses to both supporting-card layers", () => {
    const effects: CameraEffect[] = [{
      id: "pulse-1",
      token: "semantic_crop_pulse",
      start_s: 0,
      end_s: 2,
      intensity: 0.08,
      easing: "sine_pulse",
      source: "user",
    }];
    const view = render(
      editorCanvas(
        { w: 1080, h: 1920 },
        virtualPreview("supporting_card"),
        "none",
        null,
        undefined,
        100,
        1,
        effects,
      ),
    );
    const foreground = view.container.querySelector(
      'video[data-virtual-preview-deck="a"]',
    ) as HTMLVideoElement;
    const backdrop = view.container.querySelector(
      'video[data-virtual-preview-video-backdrop="a"]',
    ) as HTMLVideoElement;

    expect(foreground.style.transform).toBe("scale(1.08)");
    expect(backdrop.style.transform).toBe("scale(1.08) scale(1.12)");
  });

  it("keeps the supporting-video backdrop synchronized and cleans it up", () => {
    const play = window.HTMLMediaElement.prototype.play as jest.Mock;
    const pause = window.HTMLMediaElement.prototype.pause as jest.Mock;
    const view = render(
      editorCanvas(
        { w: 1080, h: 1920 },
        virtualPreview("supporting_card"),
        "none",
        null,
        undefined,
        100,
        0,
        [],
        true,
      ),
    );
    const foreground = view.container.querySelector(
      'video[data-virtual-preview-deck="a"]',
    ) as HTMLVideoElement;
    const backdrop = view.container.querySelector(
      'video[data-virtual-preview-video-backdrop="a"]',
    ) as HTMLVideoElement;
    Object.defineProperties(foreground, {
      paused: { configurable: true, value: false },
      readyState: { configurable: true, value: 1 },
    });
    Object.defineProperties(backdrop, {
      duration: { configurable: true, value: 5 },
      readyState: { configurable: true, value: 1 },
    });
    foreground.currentTime = 1.25;
    foreground.playbackRate = 1.5;
    foreground.style.transform = "scale(1.04)";
    play.mockClear();
    pause.mockClear();

    fireEvent.play(foreground);

    expect(backdrop.currentTime).toBeCloseTo(1.25);
    expect(backdrop.playbackRate).toBe(1.5);
    expect(backdrop.style.transform).toBe("scale(1.04) scale(1.12)");
    expect(play).toHaveBeenCalledTimes(1);

    view.unmount();
    expect(pause).toHaveBeenCalled();
  });

  it("renders supporting-card images with the renderer's card and blurred-canvas layout", () => {
    const view = render(editorCanvas({ w: 1080, h: 1920 }, supportingCardImagePreview()));
    const foreground = view.container.querySelector('img[data-virtual-preview-image="true"]');

    expect(foreground).toHaveClass("object-contain");
    expect(
      view.container.querySelector('img[data-virtual-preview-image-backdrop="a"]'),
    ).toBeInTheDocument();
    expect(
      view.container.querySelector('[data-virtual-preview-image-deck="a"]'),
    ).toHaveAttribute("data-virtual-preview-layout", "supporting_card");
  });

  it("resolves fullscreen and supporting-card fit independently during a crossfade", () => {
    const view = render(
      editorCanvas({ w: 1080, h: 1920 }, mixedLayoutTransitionPreview(), "none", null, undefined, 100, 1.9),
    );
    const outgoing = view.container.querySelector('video[data-virtual-preview-deck="a"]');
    const incoming = view.container.querySelector('video[data-virtual-preview-deck="b"]');

    expect(outgoing).toHaveClass("object-cover");
    expect(outgoing).toHaveAttribute("data-virtual-preview-layout", "fullscreen");
    expect(incoming).toHaveClass("object-contain");
    expect(incoming).toHaveAttribute("data-virtual-preview-layout", "supporting_card");
    expect(
      view.container.querySelector('video[data-virtual-preview-video-backdrop="b"]'),
    ).toBeInTheDocument();
  });

  it("resolves fullscreen and supporting-card image decks during a crossfade", () => {
    const view = render(
      editorCanvas(
        { w: 1080, h: 1920 },
        mixedLayoutImageTransitionPreview(),
        "none",
        null,
        undefined,
        100,
        1.9,
      ),
    );
    const outgoing = view.container.querySelector(
      '[data-virtual-preview-image-deck="a"]',
    );
    const incoming = view.container.querySelector(
      '[data-virtual-preview-image-deck="b"]',
    );

    expect(outgoing).toHaveAttribute("data-virtual-preview-layout", "fullscreen");
    expect(outgoing?.querySelector('[data-virtual-preview-image="true"]')).toHaveClass(
      "object-cover",
    );
    expect(incoming).toHaveAttribute("data-virtual-preview-layout", "supporting_card");
    expect(incoming?.querySelector('[data-virtual-preview-image="true"]')).toHaveClass(
      "object-contain",
    );
    expect(
      incoming?.querySelector('[data-virtual-preview-image-backdrop="b"]'),
    ).toBeInTheDocument();
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
