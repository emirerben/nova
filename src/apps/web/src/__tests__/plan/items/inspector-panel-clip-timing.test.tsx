import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";

import InspectorPanel from "@/app/plan/items/[id]/_editor/InspectorPanel";

const noop = jest.fn();

describe("InspectorPanel clip timing", () => {
  it("uses 0.1s as the smallest positive duration input", () => {
    const onPatchClipTiming = jest.fn();
    const onPatchClipLook = jest.fn();
    render(
      <InspectorPanel
        selection={{ kind: "clip", id: "slot-1" }}
        bar={null}
        clipTiming={{
          slot: {
            key: "slot-1",
            slotId: "slot-1",
            clipIndex: 0,
            inS: 0,
            durationBeats: null,
            durationS: 0.2,
            removed: false,
            momentDescription: null,
          },
          clipNumber: 1,
          durationS: 0.2,
          sourceDurationS: 1,
          sourceUrl: null,
        }}
        sfx={null}
        overlay={null}
        tab="basic"
        sampleWord={null}
        appliedPresetId={null}
        contentRef={React.createRef<HTMLTextAreaElement>()}
        onEditText={noop}
        onPatch={noop}
        onPatchTextTiming={noop}
        onPatchClipTiming={onPatchClipTiming}
        onPatchClipLook={onPatchClipLook}
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

    const duration = screen.getByRole("spinbutton", { name: "Dur seconds" });
    expect(duration).toHaveAttribute("min", "0.1");
    expect(duration).toHaveAttribute("step", "0.1");
    fireEvent.change(duration, { target: { value: "0.1" } });
    expect(onPatchClipTiming).toHaveBeenCalledWith({ durationS: 0.1 });

    expect(screen.getByRole("radio", { name: "Original" })).toBeChecked();
    fireEvent.click(screen.getByRole("radio", { name: "Olive Film" }));
    expect(onPatchClipLook).toHaveBeenCalledWith("olive_film");
    fireEvent.click(screen.getByRole("radio", { name: "Smoky Split-Tone" }));
    expect(onPatchClipLook).toHaveBeenCalledWith("smoky_split_tone");
    fireEvent.click(screen.getByRole("radio", { name: "Stadium Diffusion" }));
    expect(onPatchClipLook).toHaveBeenCalledWith("stadium_diffusion");
  });

  it("shows and emits all per-clip controls for a customizable look", () => {
    const onPatchClipLookAdjustments = jest.fn();
    const onRecordClipLookAdjustments = jest.fn();
    render(
      <InspectorPanel
        selection={{ kind: "clip", id: "slot-1" }}
        bar={null}
        clipTiming={{
          slot: {
            key: "slot-1",
            slotId: "slot-1",
            clipIndex: 0,
            inS: 0,
            durationBeats: null,
            durationS: 1,
            removed: false,
            momentDescription: null,
            lookPreset: "olive_film",
            lookAdjustments: {
              intensity: 0.8,
              warmth: 0.1,
              contrast: -0.2,
              grain: 0.3,
              vignette: 0.4,
            },
          },
          clipNumber: 1,
          durationS: 1,
          sourceDurationS: 3,
          sourceUrl: null,
        }}
        sfx={null}
        overlay={null}
        tab="basic"
        sampleWord={null}
        appliedPresetId={null}
        contentRef={React.createRef<HTMLTextAreaElement>()}
        onEditText={noop}
        onPatch={noop}
        onPatchTextTiming={noop}
        onPatchClipTiming={noop}
        onPatchClipLook={noop}
        onPatchClipLookAdjustments={onPatchClipLookAdjustments}
        onRecordClipLookAdjustments={onRecordClipLookAdjustments}
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

    expect(screen.getByRole("radio", { name: "Olive Film" })).toBeChecked();
    expect(screen.getByRole("slider", { name: "Look strength" })).toHaveValue("80");
    expect(screen.getByRole("slider", { name: "Look warmth" })).toHaveValue("10");
    expect(screen.getByRole("slider", { name: "Look contrast" })).toHaveValue("-20");
    expect(screen.getByRole("slider", { name: "Look grain" })).toHaveValue("30");
    expect(screen.getByRole("slider", { name: "Look vignette" })).toHaveValue("40");

    const warmth = screen.getByRole("slider", { name: "Look warmth" });
    expect(warmth).toHaveClass("h-11", "focus-visible:outline-lime-500");
    fireEvent.pointerDown(warmth);
    fireEvent.change(warmth, {
      target: { value: "35" },
    });
    fireEvent.change(warmth, { target: { value: "36" } });
    expect(onRecordClipLookAdjustments).toHaveBeenCalledTimes(1);
    expect(onPatchClipLookAdjustments).toHaveBeenCalledWith({ warmth: 0.35 });
    expect(onPatchClipLookAdjustments).toHaveBeenLastCalledWith({ warmth: 0.36 });

    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    expect(onRecordClipLookAdjustments).toHaveBeenCalledTimes(2);
    expect(onPatchClipLookAdjustments).toHaveBeenLastCalledWith({
      intensity: 1,
      warmth: 0,
      contrast: 0,
      grain: 0.18,
      vignette: 0.22,
    });
  });
});
