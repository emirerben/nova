import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";

import InspectorPanel from "@/app/plan/items/[id]/_editor/InspectorPanel";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

const noop = jest.fn();

function makeCaptionBar(overrides: Partial<TextElementBar> = {}): TextElementBar {
  return {
    id: "caption-0",
    role: "narrated_caption",
    text: "we flew to Turkey",
    start_s: 0,
    end_s: 1.2,
    size_px: 64,
    ...overrides,
  };
}

function renderCaptionInspector(
  bar: TextElementBar,
  overrides: Partial<React.ComponentProps<typeof InspectorPanel>> = {},
) {
  const onPatch = jest.fn();
  const onMergeCaptionCue = jest.fn();
  render(
    <InspectorPanel
      selection={{ kind: "text", id: bar.id }}
      bar={bar}
      clipTiming={null}
      sfx={null}
      overlay={null}
      tab="basic"
      sampleWord={null}
      appliedPresetId={null}
      contentRef={React.createRef<HTMLTextAreaElement>()}
      onEditText={noop}
      onPatch={onPatch}
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
      onMergeCaptionCue={onMergeCaptionCue}
      onClose={noop}
      onPickPreset={noop}
      {...overrides}
    />,
  );
  return { onPatch, onMergeCaptionCue };
}

describe("InspectorPanel caption role badge + Emphasize toggle (4b)", () => {
  it("shows the role badge for a server-assigned role", () => {
    renderCaptionInspector(makeCaptionBar({ smart_role: "hook" }));
    expect(screen.getByText("Hook")).toBeInTheDocument();
  });

  it("shows no role badge for a plain/role-less cue", () => {
    renderCaptionInspector(makeCaptionBar());
    expect(screen.queryByText("Hook")).not.toBeInTheDocument();
  });

  it("turning Emphasize on sets smart_emphasis + a role-derived smart_style", () => {
    const { onPatch } = renderCaptionInspector(makeCaptionBar({ smart_role: "payoff" }));

    fireEvent.click(screen.getByRole("button", { name: "Emphasize" }));

    expect(onPatch).toHaveBeenCalledWith({ smart_emphasis: true, smart_style: "payoff" });
  });

  it("turning Emphasize off clears both fields", () => {
    const { onPatch } = renderCaptionInspector(
      makeCaptionBar({ smart_role: "hook", smart_style: "hook", smart_emphasis: true }),
    );

    fireEvent.click(screen.getByRole("button", { name: "★ Emphasized" }));

    expect(onPatch).toHaveBeenCalledWith({ smart_emphasis: false, smart_style: null });
  });

  it("falls back to the hook style for a role-less cue's Emphasize toggle", () => {
    const { onPatch } = renderCaptionInspector(makeCaptionBar());

    fireEvent.click(screen.getByRole("button", { name: "Emphasize" }));

    expect(onPatch).toHaveBeenCalledWith({ smart_emphasis: true, smart_style: "hook" });
  });

  it("shows a bigger-size preview hint for a role that scales up", () => {
    renderCaptionInspector(makeCaptionBar({ smart_style: "hook" }));
    expect(screen.getByText(/Burns bigger for this role/)).toBeInTheDocument();
  });

  it("shows no size preview hint for a plain cue (no smart_style)", () => {
    renderCaptionInspector(makeCaptionBar());
    expect(screen.queryByText(/Burns bigger for this role/)).not.toBeInTheDocument();
  });
});

describe("InspectorPanel caption merge-with-neighbor (4b)", () => {
  it("disables both merge buttons when no neighbor is available", () => {
    renderCaptionInspector(makeCaptionBar(), {
      canMergeCaptionPrev: false,
      canMergeCaptionNext: false,
    });

    expect(screen.queryByTitle("Merge with the previous caption")).not.toBeInTheDocument();
  });

  it("merges with the previous caption on click", () => {
    const { onMergeCaptionCue } = renderCaptionInspector(makeCaptionBar(), {
      canMergeCaptionPrev: true,
      canMergeCaptionNext: false,
    });

    const prevButton = screen.getByTitle("Merge with the previous caption");
    expect(prevButton).toBeEnabled();
    fireEvent.click(prevButton);
    expect(onMergeCaptionCue).toHaveBeenCalledWith("prev");

    expect(screen.getByTitle("Merge with the next caption")).toBeDisabled();
  });

  it("merges with the next caption on click", () => {
    const { onMergeCaptionCue } = renderCaptionInspector(makeCaptionBar(), {
      canMergeCaptionPrev: false,
      canMergeCaptionNext: true,
    });

    fireEvent.click(screen.getByTitle("Merge with the next caption"));
    expect(onMergeCaptionCue).toHaveBeenCalledWith("next");
  });
});
