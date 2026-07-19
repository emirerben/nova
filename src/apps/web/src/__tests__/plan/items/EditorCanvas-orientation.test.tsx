import "@testing-library/jest-dom";
import React from "react";
import { render } from "@testing-library/react";

import EditorCanvas from "@/app/plan/items/[id]/_editor/EditorCanvas";
import type { VirtualPreviewController } from "@/app/plan/items/[id]/_editor/useVirtualPreview";
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
) {
  return (
    <EditorCanvas
      variant={variant}
      elements={[]}
      bars={[]}
      selectedTextId={null}
      currentTime={0}
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
      canvas={canvas}
      virtualPreview={virtualPreview}
    />
  );
}

function virtualPreview(): VirtualPreviewController {
  const videoARef = React.createRef<HTMLVideoElement>();
  const videoBRef = React.createRef<HTMLVideoElement>();
  const noop = jest.fn();
  const videoProps = (deck: "a" | "b", ref: React.RefObject<HTMLVideoElement>) => ({
    ref,
    muted: true,
    playsInline: true as const,
    preload: "auto" as const,
    "data-virtual-preview-deck": deck,
    "data-active": deck === "a",
    onLoadedMetadata: noop,
    onCanPlay: noop,
    onPlaying: noop,
    onWaiting: noop,
    onSeeking: noop,
    onSeeked: noop,
    onTimeUpdate: noop,
    onEnded: noop,
    onPlay: noop,
    onPause: noop,
    onError: noop,
  });

  return {
    timeline: { entries: [], totalDurationS: 0, hasMissingSource: false },
    activeDeck: "a",
    buffering: false,
    videoAProps: videoProps("a", videoARef),
    videoBProps: videoProps("b", videoBRef),
    musicAudioProps: null,
    play: noop,
    pause: noop,
    toggle: noop,
    seekTo: noop,
  } as VirtualPreviewController;
}

describe("EditorCanvas orientation video fit", () => {
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
});
