import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import TextLane from "@/app/plan/_components/TextLane";
import { textBoxScreenXFrac } from "@/app/plan/items/[id]/_editor/editor-smart-placement";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import type { PlanItemVariant } from "@/lib/plan-api";

function makeBar(overrides: Partial<TextElementBar> = {}): TextElementBar {
  return {
    id: "text-1",
    text: "First line\nSecond line",
    start_s: 0,
    end_s: 3,
    role: "generative_intro",
    alignment: "center",
    x_frac: 0.5,
    max_width_frac: 0.4,
    ...overrides,
  };
}

function renderLane(
  bar: TextElementBar,
  onTextElementsChange = jest.fn(),
  variant?: PlanItemVariant,
) {
  render(
    <TextLane
      textElements={[bar]}
      durationSeconds={10}
      currentTime={1}
      onTextElementsChange={onTextElementsChange}
      expandedBarId={bar.id}
      onBarSelect={jest.fn()}
      variant={variant}
    />,
  );
  return onTextElementsChange;
}

function latestBar(onChange: jest.Mock): TextElementBar {
  const calls = onChange.mock.calls;
  return calls[calls.length - 1][0][0] as TextElementBar;
}

describe("TextLane horizontal controls", () => {
  it.each([
    ["Staggered slice", "staggered-slice"],
    ["Giant title wipe", "giant-title-wipe"],
  ])("offers and applies the shared %s animation", async (label, effect) => {
    const onChange = renderLane(makeBar({ effect: "static" }));

    fireEvent.click(screen.getByRole("button", { name: label }));

    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1));
    expect(latestBar(onChange).effect).toBe(effect);
  });

  it("does not create history entries when the active choices are clicked", () => {
    const onChange = renderLane(makeBar());

    fireEvent.click(screen.getByRole("button", { name: "Align text center" }));
    fireEvent.click(screen.getByRole("button", { name: "Place box center" }));

    expect(onChange).not.toHaveBeenCalled();
  });

  it("changes text alignment without moving the box and undoes atomically", async () => {
    const onChange = renderLane(makeBar({ position: "top", y_frac: undefined }));

    fireEvent.click(screen.getByRole("button", { name: "Align text left" }));

    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1));
    expect(latestBar(onChange)).toMatchObject({
      alignment: "left",
      x_frac: 0.3,
      max_width_frac: 0.4,
      position: "custom",
      y_frac: 0.15,
    });

    fireEvent.click(screen.getByRole("button", { name: /Undo/i }));

    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(2));
    expect(latestBar(onChange)).toMatchObject({
      alignment: "center",
      x_frac: 0.5,
      max_width_frac: 0.4,
    });
  });

  it("places the box without changing its text alignment", async () => {
    const onChange = renderLane(makeBar({ alignment: "right", x_frac: 0.7 }));

    fireEvent.click(screen.getByRole("button", { name: "Place box left" }));
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1));
    expect(latestBar(onChange)).toMatchObject({
      alignment: "right",
      x_frac: 0.4,
      max_width_frac: 0.4,
      position: "custom",
    });

    fireEvent.click(screen.getByRole("button", { name: "Place box right" }));
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(2));
    expect(latestBar(onChange)).toMatchObject({
      alignment: "right",
      x_frac: 1,
      max_width_frac: 0.4,
      position: "custom",
    });
  });

  it("places masonry text against the visible frame at the playhead", async () => {
    const motion = {
      mode: "masonry_pan_x",
      duration_s: 8,
      pan_px: 932,
      board_width_px: 2012,
      frame_width_px: 1080,
      layer_origin_px: 900,
    };
    const onChange = renderLane(
      makeBar({
        alignment: "left",
        max_width_frac: 0.9,
        source_params: { masonry_motion: motion },
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: "Place box right" }));
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1));
    const changed = latestBar(onChange);
    const changedMotion = changed.source_params?.masonry_motion as Record<string, unknown>;

    expect(changed.x_frac).toBeCloseTo(0.1, 8);
    expect(textBoxScreenXFrac(changedMotion, 1, Number(changed.x_frac))).toBeCloseTo(
      0.1,
      8,
    );
  });

  it("uses variant motion for legacy masonry bars without stored metadata", async () => {
    const onChange = jest.fn();
    const variant = {
      variant_id: "masonry",
      montage_preset_rendered: "masonry",
    } as unknown as PlanItemVariant;
    renderLane(
      makeBar({ alignment: "left", max_width_frac: 0.9, source_params: undefined }),
      onChange,
      variant,
    );

    fireEvent.click(screen.getByRole("button", { name: "Place box right" }));
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1));
    const changed = latestBar(onChange);
    const changedMotion = changed.source_params?.masonry_motion as Record<string, unknown>;

    expect(changed.x_frac).toBeCloseTo(0.1, 8);
    expect(textBoxScreenXFrac(changedMotion, 1, Number(changed.x_frac))).toBeCloseTo(
      0.1,
      8,
    );
  });

  it("shows no box preset for a custom drag position until a preset is selected", async () => {
    const onChange = renderLane(makeBar({ x_frac: 0.37 }));

    expect(screen.getByRole("button", { name: "Place box left" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(screen.getByRole("button", { name: "Place box center" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(screen.getByRole("button", { name: "Place box right" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );

    fireEvent.click(screen.getByRole("button", { name: "Place box center" }));
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("button", { name: "Place box center" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("does not offer geometry controls for renderer-authored lyric rows", () => {
    renderLane(makeBar({ role: "lyric_line" }));

    expect(screen.queryByRole("group", { name: "Text alignment" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Box position" })).not.toBeInTheDocument();
  });
});
