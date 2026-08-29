import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import InspectorPanel from "@/app/plan/items/[id]/_editor/InspectorPanel";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

const noop = jest.fn();

function makeBar(overrides: Partial<TextElementBar> = {}): TextElementBar {
  return {
    id: "text-1",
    role: "generative_intro",
    text: "First line\nSecond line",
    start_s: 0,
    end_s: 3,
    position: "top",
    alignment: "center",
    x_frac: 0.5,
    max_width_frac: 0.4,
    ...overrides,
  };
}

function renderTextInspector(bar: TextElementBar) {
  const onPatch = jest.fn();
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
      onClose={noop}
      onPickPreset={noop}
    />,
  );
  return onPatch;
}

describe("InspectorPanel text horizontal controls", () => {
  it("offers standard, high-visibility, and off shadow effects", async () => {
    const onPatch = renderTextInspector(makeBar());
    const select = screen.getByRole("combobox", { name: "Shadow effect" });

    // Drive the real Radix Select through its keyboard contract. It's a fully
    // controlled component (unlike a native <select>'s DOM value), so each
    // pick is asserted against a bar that stays at its initial "standard"
    // shadow — picking "Standard" again from "standard" is a no-op in Radix
    // (the value doesn't change), so that transition is verified separately
    // below against a bar whose effective value differs.
    expect(select).toHaveTextContent("Standard");

    fireEvent.keyDown(select, { key: "ArrowDown" });
    fireEvent.click(await screen.findByRole("option", { name: "High visibility" }));
    expect(onPatch).toHaveBeenLastCalledWith({
      shadow_enabled: true,
      shadow_style: "high_visibility",
    });
    await waitFor(() => expect(screen.queryByRole("listbox")).not.toBeInTheDocument());

    fireEvent.keyDown(select, { key: "ArrowDown" });
    fireEvent.click(await screen.findByRole("option", { name: "Off" }));
    expect(onPatch).toHaveBeenLastCalledWith({
      shadow_enabled: false,
      shadow_style: "standard",
    });
  });

  it("restores standard from an off shadow effect", async () => {
    const onPatch = renderTextInspector(makeBar({ shadow_enabled: false }));
    const select = screen.getByRole("combobox", { name: "Shadow effect" });
    expect(select).toHaveTextContent("Off");

    fireEvent.keyDown(select, { key: "ArrowDown" });
    fireEvent.click(await screen.findByRole("option", { name: "Standard" }));
    expect(onPatch).toHaveBeenLastCalledWith({
      shadow_enabled: true,
      shadow_style: "standard",
    });
  });

  it("changes line alignment while preserving the box bounds and y placement", () => {
    const onPatch = renderTextInspector(makeBar());

    fireEvent.click(screen.getByRole("button", { name: "Align text left" }));

    expect(onPatch).toHaveBeenCalledWith({
      alignment: "left",
      x_frac: 0.3,
      position: "custom",
      y_frac: 0.15,
    });
  });

  it("moves the box while preserving the current text alignment", () => {
    const onPatch = renderTextInspector(
      makeBar({ position: "custom", alignment: "right", x_frac: 0.7, y_frac: 0.34 }),
    );

    fireEvent.click(screen.getByRole("button", { name: "Place box left" }));

    expect(onPatch).toHaveBeenCalledWith({
      x_frac: 0.4,
      position: "custom",
      y_frac: 0.34,
    });
  });

  it("shows no active preset for a manually dragged box", () => {
    renderTextInspector(makeBar({ position: "custom", x_frac: 0.37, y_frac: 0.4 }));

    for (const position of ["left", "center", "right"]) {
      expect(screen.getByRole("button", { name: `Place box ${position}` })).toHaveAttribute(
        "aria-pressed",
        "false",
      );
    }
  });

  it("hides unsupported geometry controls for lyric rows", () => {
    renderTextInspector(makeBar({ role: "lyric_line" }));

    expect(screen.queryByRole("group", { name: "Text alignment" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Box position" })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Shadow effect" })).toBeInTheDocument();
  });
});
