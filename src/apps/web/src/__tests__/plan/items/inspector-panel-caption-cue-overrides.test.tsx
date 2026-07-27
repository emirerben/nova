/**
 * Lane PR-A — caption inspector split ("This caption" per-cue overrides vs
 * "All captions" variant-level globals). Mirrors the pattern in
 * inspector-panel-caption-emphasis.test.tsx.
 */
import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";

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
    color: "#FFFFFF",
    font_family: "Playfair Display",
    ...overrides,
  };
}

function renderCaptionInspector(
  bar: TextElementBar,
  overrides: Partial<React.ComponentProps<typeof InspectorPanel>> = {},
) {
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
      {...overrides}
    />,
  );
  return { onPatch };
}

describe("InspectorPanel caption section split (Lane PR-A)", () => {
  it("renders both 'This caption' and 'All captions' section labels", () => {
    renderCaptionInspector(makeCaptionBar());
    expect(screen.getByText("This caption")).toBeInTheDocument();
    expect(screen.getByText("All captions")).toBeInTheDocument();
  });

  it("does not render the section split for a non-caption text bar", () => {
    renderCaptionInspector({
      id: "text-0",
      role: "generative_intro",
      text: "hello",
      start_s: 0,
      end_s: 1,
    });
    expect(screen.queryByText("This caption")).not.toBeInTheDocument();
    expect(screen.queryByText("All captions")).not.toBeInTheDocument();
  });

  it("shows no 'Match all captions' clear link when no per-cue override is set", () => {
    renderCaptionInspector(makeCaptionBar());
    expect(screen.queryByText("Match all captions")).not.toBeInTheDocument();
  });

  it("shows the 'Match all captions' clear link once any per-cue override is set", () => {
    renderCaptionInspector(makeCaptionBar({ cue_size_px: 90 }));
    expect(screen.getByText("Match all captions")).toBeInTheDocument();
  });

  it("clicking 'Match all captions' clears all three per-cue override fields", () => {
    const { onPatch } = renderCaptionInspector(
      makeCaptionBar({ cue_font_family: "Montserrat Bold", cue_text_color: "#00FF00", cue_size_px: 90 }),
    );
    fireEvent.click(screen.getByText("Match all captions"));
    expect(onPatch).toHaveBeenCalledWith({
      cue_font_family: null,
      cue_text_color: null,
      cue_size_px: null,
    });
  });

  it("This caption's font picker defaults to the effective (global) font when no override is set", () => {
    renderCaptionInspector(makeCaptionBar({ font_family: "Playfair Display" }));
    expect(
      screen.getByRole("button", { name: "This caption's font: Playfair Display" }),
    ).toBeInTheDocument();
  });

  it("This caption's font picker shows the override, not the global, once one is set", () => {
    renderCaptionInspector(
      makeCaptionBar({ font_family: "Playfair Display", cue_font_family: "Inter-Bold" }),
    );
    expect(
      screen.getByRole("button", { name: "This caption's font: Inter-Bold" }),
    ).toBeInTheDocument();
    // The "All captions" picker still shows the unaffected global value.
    expect(screen.getByRole("button", { name: "Font: Playfair Display" })).toBeInTheDocument();
  });

  it("picking a font in 'This caption' patches cue_font_family, not font_family", () => {
    const { onPatch } = renderCaptionInspector(makeCaptionBar());
    fireEvent.click(screen.getByRole("button", { name: /^This caption's font:/ }));
    const listbox = screen.getByRole("listbox", { name: "Fonts" });
    const firstOption = within(listbox).getAllByRole("option")[0];
    fireEvent.click(firstOption);
    expect(onPatch).toHaveBeenCalledWith(
      expect.objectContaining({ cue_font_family: expect.any(String) }),
    );
    expect(onPatch).not.toHaveBeenCalledWith(expect.objectContaining({ font_family: expect.anything() }));
  });

  it("changing This caption's color patches cue_text_color, not color", () => {
    const { onPatch } = renderCaptionInspector(makeCaptionBar());
    const hexInput = screen.getByLabelText("This caption's fill color hex");
    fireEvent.change(hexInput, { target: { value: "#123ABC" } });
    fireEvent.blur(hexInput);
    expect(onPatch).toHaveBeenCalledWith({ cue_text_color: "#123ABC" });
  });

  it("changing This caption's size patches cue_size_px, not size_px", () => {
    const { onPatch } = renderCaptionInspector(makeCaptionBar({ size_px: 64 }));
    const slider = screen.getByLabelText("This caption's font size");
    fireEvent.change(slider, { target: { value: "100" } });
    expect(onPatch).toHaveBeenCalledWith({ cue_size_px: 100 });
  });

  it("This caption's size slider shows the effective (global) size when no override is set", () => {
    renderCaptionInspector(makeCaptionBar({ size_px: 80 }));
    const slider = screen.getByLabelText("This caption's font size") as HTMLInputElement;
    expect(slider.value).toBe("80");
  });

  it("This caption's size slider shows the override once one is set", () => {
    renderCaptionInspector(makeCaptionBar({ size_px: 80, cue_size_px: 120 }));
    const slider = screen.getByLabelText("This caption's font size") as HTMLInputElement;
    expect(slider.value).toBe("120");
  });

  it("'All captions' Fill color still patches the global color field", () => {
    const { onPatch } = renderCaptionInspector(makeCaptionBar());
    // "Fill color hex" is shared with the pre-existing "Highlight" row (a
    // known, pre-existing label collision, out of scope here) — the Fill row
    // renders first.
    const [hexInput] = screen.getAllByLabelText("Fill color hex");
    fireEvent.change(hexInput, { target: { value: "#ABCDEF" } });
    fireEvent.blur(hexInput);
    expect(onPatch).toHaveBeenCalledWith({ color: "#ABCDEF" });
  });
});
