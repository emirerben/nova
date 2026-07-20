import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";

import InspectorPanel from "@/app/plan/items/[id]/_editor/InspectorPanel";

const noop = jest.fn();

describe("InspectorPanel clip timing", () => {
  it("uses 0.1s as the smallest positive duration input", () => {
    const onPatchClipTiming = jest.fn();
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
  });
});
