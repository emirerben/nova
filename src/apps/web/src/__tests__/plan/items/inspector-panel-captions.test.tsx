import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";

import InspectorPanel from "@/app/plan/items/[id]/_editor/InspectorPanel";

const noop = jest.fn();

function renderEmptyInspector(overrides = {}) {
  render(
    <InspectorPanel
      selection={null}
      bar={null}
      clipTiming={null}
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
      {...overrides}
    />,
  );
}

describe("InspectorPanel empty state", () => {
  it("shows the generic empty state", () => {
    renderEmptyInspector();

    expect(screen.getByText("Select anything to edit it")).toBeInTheDocument();
    expect(screen.queryByTestId("inspector-captions-cta")).not.toBeInTheDocument();
  });
});
