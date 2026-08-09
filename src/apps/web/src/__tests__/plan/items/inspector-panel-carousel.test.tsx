import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";

import InspectorPanel from "@/app/plan/items/[id]/_editor/InspectorPanel";
import type { CarouselPanelControl } from "@/app/plan/items/[id]/_editor/CarouselPanel";

const noop = jest.fn();

function renderCarouselInspector({
  capable = true,
  reason = null,
}: {
  capable?: boolean;
  reason?: string | null;
} = {}) {
  const onChange = jest.fn();
  const onRemove = jest.fn();
  const control: CarouselPanelControl = {
    capable,
    reason,
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

  it("fails closed when a persisted Carousel is selected but the edit is incapable", () => {
    const { onChange, onRemove } = renderCarouselInspector({
      capable: false,
      reason: "This edit style doesn't support Carousel.",
    });

    expect(screen.getByRole("status")).toHaveTextContent(
      "This edit style doesn't support Carousel.",
    );
    expect(screen.queryByRole("radiogroup", { name: "Carousel effect" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove carousel" })).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
    expect(onRemove).not.toHaveBeenCalled();
  });

  it("uses honest fallback copy when an incapable edit has no explicit reason", () => {
    renderCarouselInspector({ capable: false });

    expect(screen.getByRole("status")).toHaveTextContent(
      "Carousel isn't available for this edit.",
    );
  });
});
