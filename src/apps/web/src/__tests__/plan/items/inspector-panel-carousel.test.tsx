import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";

import InspectorPanel from "@/app/plan/items/[id]/_editor/InspectorPanel";
import type { CarouselPanelControl } from "@/app/plan/items/[id]/_editor/CarouselPanel";

const noop = jest.fn();

function renderCarouselInspector() {
  const onChange = jest.fn();
  const onRemove = jest.fn();
  const control: CarouselPanelControl = {
    capable: true,
    reason: null,
    current: {
      effect: "scale_sweep",
      mode: "focus",
      focus_clip_index: null,
      position: "middle",
      duration_s: 10,
      transition: "crossfade",
    },
    clips: [],
    onChange,
    onRemove,
  };

  render(
    <InspectorPanel
      selection={{ kind: "carousel", id: "carousel-block" }}
      bar={null}
      clipTiming={null}
      sfx={null}
      overlay={null}
      carousel={control}
      tab="basic"
      sampleWord={null}
      appliedPresetId={null}
      contentRef={React.createRef<HTMLTextAreaElement>()}
      onEditText={noop}
      onPatch={noop}
      onPatchTextTiming={noop}
      onPatchClipTiming={noop}
      onPreviewClipTiming={noop}
      onRecordClipTiming={noop}
      onPatchSfx={noop}
      onDeleteSfx={noop}
      onPatchOverlay={noop}
      onPreviewOverlay={noop}
      onRecordOverlay={noop}
      onDeleteOverlay={noop}
      onClose={noop}
      onPickPreset={noop}
    />,
  );

  return { onChange, onRemove };
}

describe("InspectorPanel Carousel", () => {
  it("owns Effect and playback controls in the right inspector", () => {
    const { onChange, onRemove } = renderCarouselInspector();

    expect(screen.getByTestId("carousel-inspector")).toHaveTextContent("Carousel");
    expect(screen.getByRole("radiogroup", { name: "Carousel effect" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: "Cover flow effect" }));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ effect: "cover_flow" }),
    );

    fireEvent.click(screen.getByRole("button", { name: "Remove carousel" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
  });
});
