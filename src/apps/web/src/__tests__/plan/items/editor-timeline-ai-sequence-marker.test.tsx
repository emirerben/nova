/**
 * Timeline text-lane row marker for AI-authored editorial sequence bars
 * (Lane PR-B). Mirrors the direct-render style already used for the lock /
 * behind-subject markers in this component — role === "generative_sequence"
 * alone is NOT sufficient (the editor's own "split and place" composition
 * tool reuses that role for user-typed text with no source_params), so the
 * marker must only show for bars carrying the backend's "sequence_scene"
 * provenance marker.
 */
import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";

// jsdom lacks ResizeObserver (EditorTimelineBody's viewport measure loop).
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

import EditorTimelineBody, {
  type EditorMotionBar,
  type EditorTimelineBodyProps,
} from "@/app/plan/items/[id]/_editor/EditorTimelineBody";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import { buildVirtualTimeline } from "@/app/plan/items/[id]/_editor/virtual-timeline";

function baseProps(textBars: TextElementBar[]): EditorTimelineBodyProps {
  return {
    durationS: 10,
    timelineProjection: buildVirtualTimeline([], []),
    currentTimeS: 0,
    zoom: 1,
    selection: null,
    onSelect: jest.fn(),
    onClear: jest.fn(),
    textBars,
    visualBlocks: [],
    slots: [],
    grid: [],
    clipsLoading: false,
    filmstripClips: [],
    sfx: [],
    hasMusic: false,
    videoMuted: false,
    onToggleVideoMute: jest.fn(),
    soundMuted: false,
    onToggleSoundMute: jest.fn(),
    overlays: [],
    onScrub: jest.fn(),
    onScrubStart: jest.fn(),
  };
}

const AI_SEQUENCE_BAR: TextElementBar = {
  id: "sequence-1",
  text: "edits and I didn't really like CapCut",
  start_s: 0,
  end_s: 2,
  role: "generative_sequence",
  source_params: { source: "sequence_scene", key: "0:1" },
};

const USER_TYPED_SEQUENCE_BAR: TextElementBar = {
  id: "user-sequence-1",
  text: "my own composed beat",
  start_s: 0,
  end_s: 2,
  role: "generative_sequence",
};

const PLAIN_TEXT_BAR: TextElementBar = {
  id: "title-1",
  text: "Big title",
  start_s: 0,
  end_s: 2,
  role: "generative_intro",
};

const MOTION_BLOCK: EditorMotionBar = {
  id: "motion-1",
  label: "Wild Type",
  start_s: 1,
  end_s: 3.5,
  sourceScene: {
    id: "motion-1",
    preset_id: "kinetic_word",
    preset_version: 2,
    start_frame: 30,
    end_frame_exclusive: 105,
    palette: { primary: "#0c0c0e", accent: "#c7ff3d" },
    intensity: 0.72,
    params: { text: "Wild Type" },
    motion: {
      version: 2,
      speed: 1,
      easing: "ease-in-out-cubic",
      hold_frames: 30,
    },
  },
};

describe("EditorTimelineBody — AI sequence row marker", () => {
  it("shows the AI sequence marker for a backend-projected sequence_scene bar", () => {
    render(<EditorTimelineBody {...baseProps([AI_SEQUENCE_BAR])} />);
    expect(screen.getByLabelText("AI sequence")).toBeInTheDocument();
  });

  it("hides the marker for a user-typed generative_sequence bar (split & place)", () => {
    render(<EditorTimelineBody {...baseProps([USER_TYPED_SEQUENCE_BAR])} />);
    expect(screen.queryByLabelText("AI sequence")).not.toBeInTheDocument();
  });

  it("hides the marker for a plain text bar", () => {
    render(<EditorTimelineBody {...baseProps([PLAIN_TEXT_BAR])} />);
    expect(screen.queryByLabelText("AI sequence")).not.toBeInTheDocument();
  });

  it("forwards one immutable text-drag origin through every trim preview", () => {
    const onPreviewTextTiming = jest.fn();
    render(
      <EditorTimelineBody
        {...baseProps([PLAIN_TEXT_BAR])}
        onPreviewTextTiming={onPreviewTextTiming}
      />,
    );
    const bar = screen.getByRole("button", { name: /Text row 1, Big title/ });
    Object.defineProperties(bar, {
      setPointerCapture: { value: jest.fn(), configurable: true },
      hasPointerCapture: { value: jest.fn(() => true), configurable: true },
      releasePointerCapture: { value: jest.fn(), configurable: true },
    });
    jest.spyOn(bar, "getBoundingClientRect").mockReturnValue({
      left: 0, right: 120, top: 0, bottom: 24, width: 120, height: 24,
      x: 0, y: 0, toJSON: () => ({}),
    });
    const scroller = screen.getByTestId("editor-timeline-lanes-scroll");
    jest.spyOn(scroller, "getBoundingClientRect").mockReturnValue({
      left: 0, right: 600, top: 0, bottom: 240, width: 600, height: 240,
      x: 0, y: 0, toJSON: () => ({}),
    });

    fireEvent.pointerDown(bar, { pointerId: 7, clientX: 119 });
    fireEvent.pointerMove(bar, { pointerId: 7, clientX: 80 });
    fireEvent.pointerMove(bar, { pointerId: 7, clientX: 110 });
    fireEvent.pointerUp(bar, { pointerId: 7, clientX: 110 });

    expect(onPreviewTextTiming).toHaveBeenCalledTimes(2);
    for (const call of onPreviewTextTiming.mock.calls) {
      expect(["left", "right", "body"]).toContain(call[2]);
      expect(call[3]).toStrictEqual(PLAIN_TEXT_BAR);
    }
  });
});

describe("EditorTimelineBody — Creator Blocks lane", () => {
  it("renders a dedicated selectable lane with exact timing", () => {
    const onSelect = jest.fn();
    render(<EditorTimelineBody
      {...baseProps([])}
      showMotionBlocks
      motionBlocks={[MOTION_BLOCK]}
      onSelect={onSelect}
    />);

    expect(screen.getByTestId("editor-motion-lane")).toBeInTheDocument();
    const bar = screen.getByRole("button", { name: /Wild Type/ });
    expect(bar).toHaveAttribute("data-editor-bar-kind", "motion");
    fireEvent.click(bar);
    expect(onSelect).toHaveBeenCalledWith("motion", "motion-1");
  });

  it("drags and trims Creator Blocks with 1/30-second snapping and one history record", () => {
    (global as unknown as { PointerEvent: typeof MouseEvent }).PointerEvent = MouseEvent;
    const onPreviewMotionTiming = jest.fn();
    const onRecordTimelineEdit = jest.fn();
    const onSelect = jest.fn();
    const capture = new Set<number>();
    Object.defineProperties(HTMLElement.prototype, {
      setPointerCapture: { configurable: true, value(id: number) { capture.add(id); } },
      hasPointerCapture: { configurable: true, value(id: number) { return capture.has(id); } },
      releasePointerCapture: { configurable: true, value(id: number) { capture.delete(id); } },
    });
    render(
      <EditorTimelineBody
        {...baseProps([])}
        showMotionBlocks
        motionBlocks={[MOTION_BLOCK]}
        onSelect={onSelect}
        onPreviewMotionTiming={onPreviewMotionTiming}
        onRecordTimelineEdit={onRecordTimelineEdit}
      />,
    );
    const bar = screen.getByRole("button", { name: /Wild Type/ });
    jest.spyOn(bar, "getBoundingClientRect").mockReturnValue({
      left: 0, right: 120, top: 0, bottom: 24, width: 120, height: 24,
      x: 0, y: 0, toJSON: () => ({}),
    });
    const scroller = screen.getByTestId("editor-timeline-lanes-scroll");
    jest.spyOn(scroller, "getBoundingClientRect").mockReturnValue({
      left: 0, right: 600, top: 0, bottom: 240, width: 600, height: 240,
      x: 0, y: 0, toJSON: () => ({}),
    });

    fireEvent.pointerDown(bar, { pointerId: 1, clientX: 60 });
    fireEvent.pointerMove(bar, { pointerId: 1, clientX: 90 });
    fireEvent.pointerUp(bar, { pointerId: 1, clientX: 90 });

    expect(onRecordTimelineEdit).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("motion", "motion-1");
    expect(onPreviewMotionTiming).toHaveBeenCalledWith(
      "motion-1",
      expect.objectContaining({ start_s: expect.any(Number), end_s: expect.any(Number) }),
      MOTION_BLOCK,
    );
    const patch = onPreviewMotionTiming.mock.calls.at(-1)?.[1];
    expect(Number.isFinite(patch.start_s)).toBe(true);
    expect(Number.isFinite(patch.end_s)).toBe(true);
    expect(patch.start_s).not.toBe(1);
    expect(patch.end_s - patch.start_s).toBeCloseTo(2.5, 8);
    expect(Math.round(patch.start_s * 30)).toBeCloseTo(patch.start_s * 30, 8);
    expect(Math.round(patch.end_s * 30)).toBeCloseTo(patch.end_s * 30, 8);

    onPreviewMotionTiming.mockClear();
    fireEvent.pointerDown(bar, { pointerId: 2, clientX: 1 });
    fireEvent.pointerMove(bar, { pointerId: 2, clientX: 20 });
    fireEvent.pointerUp(bar, { pointerId: 2, clientX: 20 });
    expect(onPreviewMotionTiming).toHaveBeenCalledWith(
      "motion-1",
      expect.objectContaining({ end_s: 3.5 }),
      MOTION_BLOCK,
    );
    const leftTrim = onPreviewMotionTiming.mock.calls.at(-1)?.[1];
    expect(leftTrim.start_s).toBeGreaterThan(1);
    expect(leftTrim.end_s).toBe(3.5);

    onPreviewMotionTiming.mockClear();
    fireEvent.pointerDown(bar, { pointerId: 3, clientX: 119 });
    fireEvent.pointerMove(bar, { pointerId: 3, clientX: 100 });
    fireEvent.pointerUp(bar, { pointerId: 3, clientX: 100 });
    const rightTrim = onPreviewMotionTiming.mock.calls.at(-1)?.[1];
    expect(rightTrim.start_s).toBe(1);
    expect(rightTrim.end_s).toBeLessThan(3.5);
  });

  it("restores the immutable motion origin when a drag is cancelled", () => {
    (global as unknown as { PointerEvent: typeof MouseEvent }).PointerEvent = MouseEvent;
    const onPreviewMotionTiming = jest.fn();
    const onRecordTimelineEdit = jest.fn();
    const capture = new Set<number>();
    Object.defineProperties(HTMLElement.prototype, {
      setPointerCapture: { configurable: true, value(id: number) { capture.add(id); } },
      hasPointerCapture: { configurable: true, value(id: number) { return capture.has(id); } },
      releasePointerCapture: { configurable: true, value(id: number) { capture.delete(id); } },
    });
    render(<EditorTimelineBody
      {...baseProps([])}
      showMotionBlocks
      motionBlocks={[MOTION_BLOCK]}
      onPreviewMotionTiming={onPreviewMotionTiming}
      onRecordTimelineEdit={onRecordTimelineEdit}
    />);
    const bar = screen.getByRole("button", { name: /Wild Type/ });
    jest.spyOn(bar, "getBoundingClientRect").mockReturnValue({
      left: 0, right: 120, top: 0, bottom: 24, width: 120, height: 24,
      x: 0, y: 0, toJSON: () => ({}),
    });
    const scroller = screen.getByTestId("editor-timeline-lanes-scroll");
    jest.spyOn(scroller, "getBoundingClientRect").mockReturnValue({
      left: 0, right: 600, top: 0, bottom: 240, width: 600, height: 240,
      x: 0, y: 0, toJSON: () => ({}),
    });

    fireEvent.pointerDown(bar, { pointerId: 4, clientX: 60 });
    fireEvent.pointerMove(bar, { pointerId: 4, clientX: 90 });
    fireEvent.pointerCancel(bar, { pointerId: 4, clientX: 90 });

    expect(onRecordTimelineEdit).toHaveBeenCalledTimes(1);
    expect(onPreviewMotionTiming.mock.calls.at(-1)).toEqual([
      "motion-1",
      { start_s: 1, end_s: 3.5 },
      MOTION_BLOCK,
    ]);
  });

  it("keeps persisted read-only Creator Blocks selectable without drag mutation", () => {
    const onPreviewMotionTiming = jest.fn();
    const onRecordTimelineEdit = jest.fn();
    const onSelect = jest.fn();
    render(<EditorTimelineBody
      {...baseProps([])}
      showMotionBlocks
      motionBlocks={[{
        id: "motion-evolving",
        label: "Evolving Type",
        start_s: 0,
        end_s: 5.3,
        sourceScene: {
          ...MOTION_BLOCK.sourceScene,
          id: "motion-evolving",
          preset_id: "evolving_type",
          start_frame: 0,
          end_frame_exclusive: 159,
        } as EditorMotionBar["sourceScene"],
        readOnly: true,
      }]}
      onPreviewMotionTiming={onPreviewMotionTiming}
      onRecordTimelineEdit={onRecordTimelineEdit}
      onSelect={onSelect}
    />);
    const bar = screen.getByRole("button", { name: /Evolving Type/ });

    fireEvent.pointerDown(bar, { pointerId: 5, clientX: 60 });
    fireEvent.pointerMove(bar, { pointerId: 5, clientX: 90 });
    fireEvent.pointerUp(bar, { pointerId: 5, clientX: 90 });
    fireEvent.click(bar);

    expect(onPreviewMotionTiming).not.toHaveBeenCalled();
    expect(onRecordTimelineEdit).not.toHaveBeenCalled();
    expect(onSelect).toHaveBeenCalledWith("motion", "motion-evolving");
  });
});
