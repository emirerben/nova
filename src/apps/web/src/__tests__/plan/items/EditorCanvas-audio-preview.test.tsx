import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";

import EditorCanvas from "@/app/plan/items/[id]/_editor/EditorCanvas";
import type { PlanItemVariant } from "@/lib/plan-api";

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

(
  global as unknown as { ResizeObserver: typeof ResizeObserverMock }
).ResizeObserver = ResizeObserverMock;

const variant = {
  variant_id: "guided_story",
  output_url: "https://cdn.example.test/output.mp4",
  render_status: "ready",
  text_mode: "agent_text",
} as PlanItemVariant;

function canvas(
  currentTime: number,
  { playing = true, sourceUrl = "https://cdn.example.test/source-a.m4a" } = {},
) {
  return (
    <EditorCanvas
      variant={variant}
      elements={[]}
      bars={[]}
      selectedTextId={null}
      currentTime={currentTime}
      masonryDurationS={6}
      zoomPct={100}
      tool="select"
      videoRef={React.createRef<HTMLVideoElement>()}
      onSelectText={jest.fn()}
      onClearSelection={jest.fn()}
      onPatchBar={jest.fn()}
      onFocusContent={jest.fn()}
      onTimeUpdate={jest.fn()}
      onDuration={jest.fn()}
      playing={playing}
      sourceAudioMix="source_a"
      sourceAudioOptions={[
        {
          mix: "source_a",
          audio_path: "source-a.m4a",
          audio_url: sourceUrl,
          duration_s: 6,
        },
      ]}
    />
  );
}

describe("EditorCanvas alternate source audio", () => {
  beforeEach(() => {
    jest
      .spyOn(window.HTMLMediaElement.prototype, "load")
      .mockImplementation(() => {});
    jest
      .spyOn(window.HTMLMediaElement.prototype, "play")
      .mockImplementation(() => Promise.resolve());
    jest
      .spyOn(window.HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => {});
  });

  afterEach(() => jest.restoreAllMocks());

  it("does not reload the audio file on every committed playback tick", () => {
    const loadSpy = jest.spyOn(window.HTMLMediaElement.prototype, "load");
    const { rerender } = render(canvas(0));

    expect(
      screen.getByTestId("intercut-source-audio-preview"),
    ).toBeInTheDocument();
    expect(loadSpy).toHaveBeenCalledTimes(1);

    rerender(canvas(0.2));

    expect(loadSpy).toHaveBeenCalledTimes(1);
  });

  it("reloads only when the selected source identity changes", () => {
    const loadSpy = jest.spyOn(window.HTMLMediaElement.prototype, "load");
    const { rerender } = render(canvas(0));

    rerender(
      canvas(0, { sourceUrl: "https://cdn.example.test/source-b.m4a" }),
    );

    expect(loadSpy).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("intercut-source-audio-preview")).toHaveAttribute(
      "src",
      "https://cdn.example.test/source-b.m4a",
    );
  });

  it("mirrors transport pause without reloading the source", () => {
    const loadSpy = jest.spyOn(window.HTMLMediaElement.prototype, "load");
    const pauseSpy = jest.spyOn(window.HTMLMediaElement.prototype, "pause");
    const { rerender } = render(canvas(0));

    rerender(canvas(0.2, { playing: false }));

    expect(loadSpy).toHaveBeenCalledTimes(1);
    expect(pauseSpy).toHaveBeenCalled();
  });
});
