/**
 * EditorCanvas — carousel-block scale wrapper (Lane C mount bug #1).
 *
 * `CarouselBlockPreviewImpl` renders at its native 1080x1920 canvas space and
 * documents that the PARENT mount must apply the stage's fit-to-viewport
 * scale (see that component's docblock, "Coordinate contract for the
 * parent"). The original EditorCanvas mount forgot to apply it, so the
 * 1080x1920 box rendered at native size inside the much smaller on-screen
 * stage — only its top-left sliver was visible (measured live: a 1080x1920
 * root with `transform: none` inside a ~198x354 on-screen stage).
 *
 * This locks in the fix: the mount is wrapped in a `data-testid=
 * "carousel-block-scale-wrapper"` box sized to the canvas's native
 * dimensions and scaled by the SAME `stageSize.h / canvas.h` ratio the rest
 * of this file already uses to convert canvas-native px to on-screen CSS px
 * (e.g. the caption font-size calc) — not a newly invented ratio.
 */
import "@testing-library/jest-dom";
import React from "react";
import { render } from "@testing-library/react";

import EditorCanvas from "@/app/plan/items/[id]/_editor/EditorCanvas";
import type { VirtualPreviewController } from "@/app/plan/items/[id]/_editor/useVirtualPreview";
import type { VirtualTimeline } from "@/app/plan/items/[id]/_editor/virtual-timeline";
import type { CarouselMoment, TimelineClip } from "@/lib/generative-api";
import type { PlanItemVariant } from "@/lib/plan-api";

// jsdom lacks ResizeObserver — report a fixed on-screen stage size so the
// scale ratio is deterministic: stageSize.h / canvas.h = 960 / 1920 = 0.5.
class ResizeObserverMock {
  constructor(private readonly callback: ResizeObserverCallback) {}
  observe() {
    this.callback(
      [{ contentRect: { width: 540, height: 960 } } as ResizeObserverEntry],
      this as unknown as ResizeObserver,
    );
  }
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

const variant = {
  variant_id: "original_text",
  output_url: "https://example.com/output.mp4",
  render_status: "ready",
  text_mode: "original_text",
} as unknown as PlanItemVariant;

function noopVideoProps(deck: "a" | "b") {
  return {
    ref: undefined,
    muted: true,
    playsInline: true as const,
    preload: "auto" as const,
    "data-virtual-preview-deck": deck,
    "data-active": deck === "a",
    onLoadedMetadata: jest.fn(),
    onCanPlay: jest.fn(),
    onPlaying: jest.fn(),
    onWaiting: jest.fn(),
    onSeeking: jest.fn(),
    onSeeked: jest.fn(),
    onTimeUpdate: jest.fn(),
    onEnded: jest.fn(),
    onPlay: jest.fn(),
    onPause: jest.fn(),
    onError: jest.fn(),
  };
}

function makeVirtualPreview(timeline: VirtualTimeline): VirtualPreviewController {
  return {
    timeline,
    activeDeck: "a",
    buffering: false,
    transitionPreview: null,
    videoAProps: noopVideoProps("a"),
    videoBProps: noopVideoProps("b"),
    musicAudioProps: null,
    play: jest.fn(),
    pause: jest.fn(),
    toggle: jest.fn(),
    seekTo: jest.fn(),
  } as unknown as VirtualPreviewController;
}

const CAROUSEL_MOMENT: CarouselMoment = {
  effect: "cover_flow",
  mode: "focus",
  focus_clip_index: null,
  position: "middle",
  duration_s: 10,
} as unknown as CarouselMoment;

const CLIPS: Pick<TimelineClip, "clip_index" | "signed_url">[] = [
  { clip_index: 0, signed_url: "https://cdn.example.test/clip0.mp4" },
  { clip_index: 1, signed_url: "https://cdn.example.test/clip1.mp4" },
];

// A single spliced carousel entry spanning [5, 15).
const TIMELINE_WITH_CAROUSEL: VirtualTimeline = {
  entries: [
    {
      kind: "clip",
      slotIndex: 0,
      slotKey: "s0",
      clipIndex: 0,
      startS: 0,
      durationS: 5,
      inS: 0,
      sourceUrl: CLIPS[0].signed_url ?? null,
      transitionAfter: "cut",
      transitionDurationS: null,
      overlapBeforeS: 0,
    },
    { kind: "carousel", startS: 5, durationS: 10 },
  ],
  totalDurationS: 15,
  hasMissingSource: false,
};

function renderCanvas(currentTime: number) {
  return render(
    <EditorCanvas
      variant={variant}
      elements={[]}
      bars={[]}
      selectedTextId={null}
      currentTime={currentTime}
      masonryDurationS={15}
      zoomPct={100}
      tool="select"
      videoRef={React.createRef<HTMLVideoElement>()}
      onSelectText={jest.fn()}
      onClearSelection={jest.fn()}
      onPatchBar={jest.fn()}
      onFocusContent={jest.fn()}
      onTimeUpdate={jest.fn()}
      onDuration={jest.fn()}
      playing={false}
      virtualPreview={makeVirtualPreview(TIMELINE_WITH_CAROUSEL)}
      carouselMoment={CAROUSEL_MOMENT}
      carouselClips={CLIPS}
    />,
  );
}

describe("EditorCanvas carousel-block scale wrapper", () => {
  it("scales the native 1080x1920 carousel mount to the on-screen stage size when the playhead is inside the block", () => {
    const { container } = renderCanvas(8); // inside [5, 15)

    const wrapper = container.querySelector<HTMLDivElement>(
      '[data-testid="carousel-block-scale-wrapper"]',
    );
    expect(wrapper).toBeInTheDocument();
    // Native canvas dimensions, not the on-screen size — the scale transform
    // (not a resize) is what fits it into the stage.
    expect(wrapper).toHaveStyle({ width: "1080px", height: "1920px" });
    // stageSize.h (960, from the ResizeObserver mock) / canvas.h (1920) = 0.5.
    expect(wrapper).toHaveStyle({ transform: "scale(0.5)", transformOrigin: "0 0" });
  });

  it("does not mount the carousel preview (or its scale wrapper) when the playhead is outside the block", () => {
    const { container } = renderCanvas(2); // inside the clip entry [0, 5)

    expect(
      container.querySelector('[data-testid="carousel-block-scale-wrapper"]'),
    ).not.toBeInTheDocument();
  });
});
