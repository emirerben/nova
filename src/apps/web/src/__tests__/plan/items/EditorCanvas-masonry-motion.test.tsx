import "@testing-library/jest-dom";
import React from "react";
import { act, fireEvent, render } from "@testing-library/react";

import EditorCanvas from "@/app/plan/items/[id]/_editor/EditorCanvas";
import { createEditorPlaybackClock } from "@/app/plan/items/[id]/_editor/editor-playback-clock";
import type { PlanItemVariant, TextElement } from "@/lib/plan-api";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

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
(global as unknown as { PointerEvent: typeof MouseEvent }).PointerEvent = MouseEvent;

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

function canvas(
  currentTime: number,
  currentVariant = variant,
  currentBars = [bar],
  playbackClock: ReturnType<typeof createEditorPlaybackClock> | null = null,
) {
  return (
    <EditorCanvas
      variant={currentVariant}
      elements={[element]}
      bars={currentBars}
      selectedTextId={null}
      currentTime={currentTime}
      playbackClock={playbackClock}
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
  it("publishes decoded rendered frames and rejects callbacks invalidated by a seek", () => {
    const callbacks = new Map<number, VideoFrameRequestCallback>();
    let nextId = 1;
    const request = jest.fn((callback: VideoFrameRequestCallback) => {
        const id = nextId++;
        callbacks.set(id, callback);
        return id;
      });
    const cancel = jest.fn();
    Object.defineProperties(HTMLVideoElement.prototype, {
      requestVideoFrameCallback: { configurable: true, value: request },
      cancelVideoFrameCallback: { configurable: true, value: cancel },
    });
    const clock = createEditorPlaybackClock(0);
    const videoRef = React.createRef<HTMLVideoElement>();
    const view = render(
      <EditorCanvas
        variant={variant}
        elements={[element]}
        bars={[bar]}
        selectedTextId={null}
        currentTime={0}
        playbackClock={clock}
        playing
        masonryDurationS={8}
        zoomPct={100}
        tool="select"
        videoRef={videoRef}
        onSelectText={jest.fn()}
        onClearSelection={jest.fn()}
        onPatchBar={jest.fn()}
        onFocusContent={jest.fn()}
        onTimeUpdate={jest.fn()}
        onDuration={jest.fn()}
      />,
    );
    const video = videoRef.current!;
    const firstCallback = callbacks.get(1)!;

    act(() => firstCallback(0, { mediaTime: 1.25 } as VideoFrameCallbackMetadata));
    expect(clock.getSnapshot()).toBe(1.25);

    video.currentTime = 2;
    fireEvent.seeked(video);
    expect(clock.getSnapshot()).toBe(2);
    act(() => firstCallback(0, { mediaTime: 7 } as VideoFrameCallbackMetadata));
    expect(clock.getSnapshot()).toBe(2);

    const oldSourceCallback = callbacks.get(nextId - 1)!;
    view.rerender(
      <EditorCanvas
        variant={{
          ...variant,
          output_url: "https://example.com/output-v2.mp4",
          render_finished_at: "2026-08-13T16:00:00Z",
        }}
        elements={[element]}
        bars={[bar]}
        selectedTextId={null}
        currentTime={2}
        playbackClock={clock}
        playing
        masonryDurationS={8}
        zoomPct={100}
        tool="select"
        videoRef={videoRef}
        onSelectText={jest.fn()}
        onClearSelection={jest.fn()}
        onPatchBar={jest.fn()}
        onFocusContent={jest.fn()}
        onTimeUpdate={jest.fn()}
        onDuration={jest.fn()}
      />,
    );
    const afterSourceSwap = clock.getSnapshot();
    act(() =>
      oldSourceCallback(0, { mediaTime: 6 } as VideoFrameCallbackMetadata),
    );
    expect(clock.getSnapshot()).toBe(afterSourceSwap);

    view.unmount();
    expect(cancel).toHaveBeenCalled();
    expect(request).toHaveBeenCalled();
    delete (HTMLVideoElement.prototype as Partial<HTMLVideoElement>)
      .requestVideoFrameCallback;
    delete (HTMLVideoElement.prototype as Partial<HTMLVideoElement>)
      .cancelVideoFrameCallback;
  });

  it("uses timeupdate as the compatibility clock when rendered rVFC is unavailable", () => {
    const originalRequest = HTMLVideoElement.prototype.requestVideoFrameCallback;
    Object.defineProperty(HTMLVideoElement.prototype, "requestVideoFrameCallback", {
      configurable: true,
      value: undefined,
    });
    const clock = createEditorPlaybackClock(0);
    const videoRef = React.createRef<HTMLVideoElement>();
    try {
      const view = render(
        <EditorCanvas
          variant={variant}
          elements={[element]}
          bars={[bar]}
          selectedTextId={null}
          currentTime={0}
          playbackClock={clock}
          playing
          masonryDurationS={8}
          zoomPct={100}
          tool="select"
          videoRef={videoRef}
          onSelectText={jest.fn()}
          onClearSelection={jest.fn()}
          onPatchBar={jest.fn()}
          onFocusContent={jest.fn()}
          onTimeUpdate={jest.fn()}
          onDuration={jest.fn()}
        />,
      );
      videoRef.current!.currentTime = 1.75;
      fireEvent.timeUpdate(videoRef.current!);
      expect(clock.getSnapshot()).toBe(1.75);
      view.unmount();
    } finally {
      Object.defineProperty(HTMLVideoElement.prototype, "requestVideoFrameCallback", {
        configurable: true,
        value: originalRequest,
      });
    }
  });

  it("authoritatively resamples rendered video after canplay, rate, and visibility recovery", () => {
    const callbacks = new Map<number, VideoFrameRequestCallback>();
    let nextId = 1;
    const request = jest.fn((callback: VideoFrameRequestCallback) => {
      const id = nextId++;
      callbacks.set(id, callback);
      return id;
    });
    const cancel = jest.fn();
    Object.defineProperties(HTMLVideoElement.prototype, {
      requestVideoFrameCallback: { configurable: true, value: request },
      cancelVideoFrameCallback: { configurable: true, value: cancel },
    });
    const visibilityDescriptor = Object.getOwnPropertyDescriptor(document, "visibilityState");
    const clock = createEditorPlaybackClock(0);
    const videoRef = React.createRef<HTMLVideoElement>();
    try {
      const view = render(
        <EditorCanvas
          variant={variant}
          elements={[element]}
          bars={[bar]}
          selectedTextId={null}
          currentTime={0}
          playbackClock={clock}
          playing
          masonryDurationS={8}
          zoomPct={100}
          tool="select"
          videoRef={videoRef}
          onSelectText={jest.fn()}
          onClearSelection={jest.fn()}
          onPatchBar={jest.fn()}
          onFocusContent={jest.fn()}
          onTimeUpdate={jest.fn()}
          onDuration={jest.fn()}
        />,
      );
      const video = videoRef.current!;

      video.currentTime = 1.25;
      act(() => fireEvent.canPlay(video));
      expect(clock.getSnapshot()).toBe(1.25);

      video.currentTime = 2.5;
      act(() => fireEvent.rateChange(video));
      expect(clock.getSnapshot()).toBe(2.5);

      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "visible",
      });
      video.currentTime = 3.75;
      act(() => document.dispatchEvent(new Event("visibilitychange")));
      expect(clock.getSnapshot()).toBe(3.75);
      expect(request).toHaveBeenCalledTimes(4);
      expect(cancel).toHaveBeenCalledTimes(3);
      view.unmount();
    } finally {
      if (visibilityDescriptor) {
        Object.defineProperty(document, "visibilityState", visibilityDescriptor);
      } else {
        Reflect.deleteProperty(document, "visibilityState");
      }
      delete (HTMLVideoElement.prototype as Partial<HTMLVideoElement>)
        .requestVideoFrameCallback;
      delete (HTMLVideoElement.prototype as Partial<HTMLVideoElement>)
        .cancelVideoFrameCallback;
    }
  });

  it("preserves the legacy reduced-motion behavior while flag-off", () => {
    const previousMatchMedia = window.matchMedia;
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: jest.fn().mockReturnValue({
        matches: true,
        media: "(prefers-reduced-motion: reduce)",
        onchange: null,
        addEventListener: jest.fn(),
        removeEventListener: jest.fn(),
        addListener: jest.fn(),
        removeListener: jest.fn(),
        dispatchEvent: jest.fn(),
      }),
    });

    try {
      const handwritingBar = { ...bar, effect: "handwriting" } as TextElementBar;
      const view = render(canvas(0.5, variant, [handwritingBar]));
      const paths = Array.from(
        view.container.querySelectorAll<SVGPathElement>(
          "[data-handwriting-strokes] g:last-of-type path",
        ),
      );

      expect(paths.length).toBeGreaterThan(0);
      expect(paths.every((path) => path.getAttribute("stroke-dashoffset") === "0")).toBe(true);
    } finally {
      Object.defineProperty(window, "matchMedia", {
        configurable: true,
        value: previousMatchMedia,
      });
    }
  });

  it("keeps authored handwriting timing under reduced motion when frame-driven", () => {
    const previousMatchMedia = window.matchMedia;
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: jest.fn().mockReturnValue({
        matches: true,
        media: "(prefers-reduced-motion: reduce)",
        onchange: null,
        addEventListener: jest.fn(),
        removeEventListener: jest.fn(),
        addListener: jest.fn(),
        removeListener: jest.fn(),
        dispatchEvent: jest.fn(),
      }),
    });

    try {
      const handwritingBar = { ...bar, effect: "handwriting" } as TextElementBar;
      const view = render(
        canvas(0.5, variant, [handwritingBar], createEditorPlaybackClock(0.5)),
      );
      const paths = Array.from(
        view.container.querySelectorAll<SVGPathElement>(
          "[data-handwriting-strokes] g:last-of-type path",
        ),
      );
      expect(paths.length).toBeGreaterThan(0);
      expect(paths.some((path) => path.getAttribute("stroke-dashoffset") !== "0")).toBe(true);
    } finally {
      Object.defineProperty(window, "matchMedia", {
        configurable: true,
        value: previousMatchMedia,
      });
    }
  });

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

  it("persists a board-local layer origin when dragging into a late pocket", () => {
    const onPatchBar = jest.fn();
    const view = render(
      <EditorCanvas
        variant={variant}
        elements={[element]}
        bars={[bar]}
        selectedTextId="title"
        currentTime={6}
        masonryDurationS={8}
        zoomPct={100}
        tool="select"
        videoRef={React.createRef<HTMLVideoElement>()}
        onSelectText={jest.fn()}
        onClearSelection={jest.fn()}
        onPatchBar={onPatchBar}
        onFocusContent={jest.fn()}
        onTimeUpdate={jest.fn()}
        onDuration={jest.fn()}
      />,
    );
    const overlay = view.container.querySelector<HTMLElement>("[data-text-id='title']");
    expect(overlay).not.toBeNull();

    fireEvent.pointerDown(overlay as HTMLElement, {
      button: 0,
      clientX: 0,
      clientY: 0,
      pointerId: 1,
    });
    fireEvent.pointerMove(overlay as HTMLElement, {
      clientX: 400,
      clientY: 0,
      pointerId: 1,
    });
    fireEvent.pointerUp(overlay as HTMLElement, { pointerId: 1 });

    expect(onPatchBar).toHaveBeenCalledWith(
      "title",
      expect.objectContaining({
        x_frac: expect.any(Number),
        source_params: {
          masonry_motion: expect.objectContaining({ layer_origin_px: expect.any(Number) }),
        },
      }),
    );
    const patch = onPatchBar.mock.calls[0]?.[1] as TextElementBar;
    const layerOriginPx = Number(
      (patch.source_params?.masonry_motion as Record<string, unknown>)?.layer_origin_px,
    );
    const expectedBoardX = Number(bar.x_frac) * 1080 + (400 / 540) * 1080;
    expect(layerOriginPx).toBeGreaterThan(0);
    expect(layerOriginPx + Number(patch.x_frac) * 1080).toBeCloseTo(expectedBoardX, 6);
  });

  it("commits width resizing as one custom-placement patch", () => {
    const onPatchBar = jest.fn();
    const view = render(
      <EditorCanvas
        variant={variant}
        elements={[element]}
        bars={[{ ...bar, position: "top", y_frac: undefined }]}
        selectedTextId="title"
        currentTime={1}
        masonryDurationS={8}
        zoomPct={100}
        tool="select"
        videoRef={React.createRef<HTMLVideoElement>()}
        onSelectText={jest.fn()}
        onClearSelection={jest.fn()}
        onPatchBar={onPatchBar}
        onFocusContent={jest.fn()}
        onTimeUpdate={jest.fn()}
        onDuration={jest.fn()}
      />,
    );
    const overlay = view.container.querySelector<HTMLElement>("[data-text-id='title']");
    const handle = view.getByRole("button", { name: "Adjust text width (right)" });

    fireEvent.pointerDown(handle, { clientX: 0, pointerId: 1 });
    fireEvent.pointerMove(overlay as HTMLElement, { clientX: 100, pointerId: 1 });
    fireEvent.pointerUp(overlay as HTMLElement, { pointerId: 1 });

    expect(onPatchBar).toHaveBeenCalledTimes(1);
    expect(onPatchBar).toHaveBeenCalledWith(
      "title",
      expect.objectContaining({
        max_width_frac: expect.any(Number),
        position: "custom",
        y_frac: 0.15,
      }),
    );
  });
});

describe("EditorCanvas subtitled preview", () => {
  it("keeps the caption-free base editable while showing the active working cue", () => {
    const subtitled = {
      ...variant,
      resolved_archetype: "subtitled",
      base_video_url: "https://example.com/caption-free-base.mp4",
      captions_enabled: true,
      caption_cues: [
        { text: "Stale persisted caption", start_s: 1, end_s: 2.5 },
      ],
    } as unknown as PlanItemVariant;
    const captionBar: TextElementBar = {
      id: "caption-0",
      text: "Edited working caption",
      start_s: 1,
      end_s: 2.5,
      role: "narrated_caption",
      font_family: "Playfair Display",
      size_px: 192,
      color: "#112233",
      stroke_width: 8,
      shadow_enabled: false,
    };

    const view = render(canvas(1.5, subtitled, [bar, captionBar]));
    const caption = view.getByText("Edited working caption");

    expect(caption.closest("[data-caption-preview]")).toHaveAttribute(
      "data-caption-preview",
      "true",
    );
    expect(caption).toHaveStyle({
      color: "#112233",
      fontSize: "96px",
    });
    expect(caption.style.fontFamily).toContain("Playfair Display");
    expect(caption.style.textShadow).toContain("4px 0 0 #000");
    expect(view.queryByText("Stale persisted caption")).not.toBeInTheDocument();
    expect(view.container.querySelector("video")).toHaveAttribute(
      "src",
      "https://example.com/caption-free-base.mp4",
    );

    view.rerender(canvas(3, subtitled, [bar, captionBar]));
    expect(view.queryByText("Edited working caption")).not.toBeInTheDocument();
  });

  it("does not show cues when captions are disabled", () => {
    const subtitled = {
      ...variant,
      resolved_archetype: "subtitled",
      base_video_url: "https://example.com/caption-free-base.mp4",
      captions_enabled: false,
      caption_cues: [{ text: "Hidden caption", start_s: 1, end_s: 2.5 }],
    } as unknown as PlanItemVariant;

    const view = render(canvas(1.5, subtitled, [
      bar,
      { id: "caption-0", text: "Hidden caption", start_s: 1, end_s: 2.5, role: "narrated_caption" },
    ]));

    expect(view.queryByText("Hidden caption")).not.toBeInTheDocument();
  });

  it("does not double-preview captions over an output-only burned video", () => {
    const subtitled = {
      ...variant,
      resolved_archetype: "subtitled",
      base_video_url: null,
      output_url: "https://example.com/already-captioned.mp4",
      captions_enabled: true,
      caption_cues: [{ text: "Already burned", start_s: 1, end_s: 2.5 }],
    } as unknown as PlanItemVariant;

    const view = render(canvas(1.5, subtitled, [
      bar,
      { id: "caption-0", text: "Already burned", start_s: 1, end_s: 2.5, role: "narrated_caption" },
    ]));

    expect(view.queryByText("Already burned")).not.toBeInTheDocument();
    expect(view.container.querySelector("video")).toHaveAttribute(
      "src",
      "https://example.com/already-captioned.mp4",
    );
  });
});
