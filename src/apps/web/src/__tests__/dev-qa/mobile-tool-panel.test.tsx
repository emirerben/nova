import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  MobileToolPanel,
  type MobileToolPanelState,
} from "@/app/dev-qa/mobile-editor/MobileToolPanel";

const state: MobileToolPanelState = {
  text: {
    font: "Inter",
    color: "#FFFFFF",
    size: 64,
    alignment: "center",
    boxPosition: "center",
    effect: "none",
    motion: null,
    themeTransition: "none",
    themeTargetGlyph: "",
    preset: "clean-caption",
    highlightColor: "#A3E635",
    strokeWidth: 0,
    textCase: "none",
    letterSpacing: 0,
    lineSpacing: 1.15,
    maxWidthFrac: 0.9,
    shadowEnabled: true,
    shadowStyle: "standard",
    behindSubject: false,
  },
  captions: {
    text: "Caption",
    enabled: true,
    font: "Inter",
    color: "#FFFFFF",
    size: 52,
    stroke: 4,
    shadow: true,
    language: "English",
  },
  musicTrack: "City Lights",
  musicGain: 70,
  visuals: [],
  overlay: {
    name: "Badge",
    durationS: 2,
    position: "Center",
    sourceInS: 0.4,
    sourceOutS: 3.2,
    scale: 0.42,
    displayMode: "Overlay",
    zOrder: 2,
  },
  look: "Clean",
  clipLook: "Clean",
  transition: "Cut",
  kriaStatus: "Proposal ready",
};

describe("MobileToolPanel overlays", () => {
  it("exposes the complete placement controls with 44px tab and close targets", async () => {
    const user = userEvent.setup();
    render(
      <MobileToolPanel
        tool="overlays"
        state={state}
        onAction={jest.fn()}
        onClose={jest.fn()}
        onDisabledTap={jest.fn()}
      />,
    );

    const close = screen.getByRole("button", { name: "Close Overlays controls" });
    expect(close).toHaveClass("size-11");
    const placeTab = screen.getByRole("tab", { name: /Place/i });
    expect(placeTab).toHaveClass("min-h-11");
    await user.click(placeTab);

    expect(screen.getByRole("slider", { name: "Overlay source In" })).toBeVisible();
    expect(screen.getByRole("slider", { name: "Overlay source Out" })).toBeVisible();
    expect(screen.getByRole("slider", { name: "Overlay scale" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Fullscreen" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Send backward" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Bring forward" })).toBeVisible();
  });

  it("passes an uploaded overlay object URL and media kind to the fixture", () => {
    const onAction = jest.fn();
    const createObjectURL = jest.fn(() => "blob:overlay-preview");
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: createObjectURL,
    });
    render(
      <MobileToolPanel
        tool="overlays"
        state={{ ...state, overlay: null }}
        onAction={onAction}
        onClose={jest.fn()}
        onDisabledTap={jest.fn()}
      />,
    );

    const input = screen.getByLabelText("Upload overlay");
    const file = new File(["image"], "badge.png", { type: "image/png" });
    fireEvent.change(input, { target: { files: [file] } });

    expect(createObjectURL).toHaveBeenCalledWith(file);
    expect(onAction).toHaveBeenCalledWith("overlays.upload", {
      name: "badge.png",
      previewUrl: "blob:overlay-preview",
      mediaKind: "image",
    });
  });
});
