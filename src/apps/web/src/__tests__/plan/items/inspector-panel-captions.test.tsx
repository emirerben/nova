import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";

import InspectorPanel from "@/app/plan/items/[id]/_editor/InspectorPanel";

const noop = jest.fn();

/**
 * The empty inspector state (nothing selected) for a caption archetype. Without
 * `captionsTabHref` it shows the generic "Select anything to edit it"; with it,
 * a caption user who clicks the (uneditable) on-video caption gets a signpost to
 * the Captions tab instead of a dead-end empty panel (the reported bug).
 */
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

describe("InspectorPanel empty state — caption archetype CTA", () => {
  it("shows the Edit captions CTA (not the generic empty state) when captionsTabHref is set", () => {
    renderEmptyInspector({ captionsTabHref: "/plan/items/item-1" });

    expect(screen.getByTestId("inspector-captions-cta")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /edit captions/i });
    expect(link).toHaveAttribute("href", "/plan/items/item-1");
    expect(screen.queryByText("Select anything to edit it")).not.toBeInTheDocument();
  });

  it("shows the generic empty state when captionsTabHref is absent", () => {
    renderEmptyInspector();

    expect(screen.getByText("Select anything to edit it")).toBeInTheDocument();
    expect(screen.queryByTestId("inspector-captions-cta")).not.toBeInTheDocument();
  });

  it("shows the generic empty state when captionsTabHref is null", () => {
    renderEmptyInspector({ captionsTabHref: null });

    expect(screen.getByText("Select anything to edit it")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /edit captions/i })).not.toBeInTheDocument();
  });
});
