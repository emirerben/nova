import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import StylesDrawer from "@/app/plan/items/[id]/_editor/StylesDrawer";

describe("StylesDrawer video looks", () => {
  it("renders capability-advertised looks, selection, and source preview", () => {
    const onSelectLook = jest.fn();
    const { container } = render(
      <StylesDrawer
        sampleText={null}
        appliedStyleSetId={null}
        availableLookPresets={["none", "golden_hour", "faded_analog"]}
        selectedLookPreset="golden_hour"
        lookPreviewUrl="https://storage.example/source.mp4?token=abc"
        onSelectLook={onSelectLook}
      />,
    );

    expect(screen.getByRole("radio", { name: "Golden Hour" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("radio", { name: "Faded Analog" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    expect(container.querySelector("video")).toHaveAttribute(
      "src",
      "https://storage.example/source.mp4?token=abc#t=0.05",
    );

    fireEvent.click(screen.getByRole("radio", { name: "Faded Analog" }));
    expect(onSelectLook).toHaveBeenCalledWith("faded_analog");
    expect(screen.queryByText("Text style")).not.toBeInTheDocument();
  });

  it("shows an honest mixed state when slots do not share one look", () => {
    render(
      <StylesDrawer
        sampleText={null}
        appliedStyleSetId={null}
        availableLookPresets={["none", "golden_hour", "faded_analog"]}
        selectedLookPreset={null}
        lookPresetMixed
        onSelectLook={jest.fn()}
      />,
    );

    expect(screen.getByText("Mixed")).toBeInTheDocument();
    for (const radio of screen.getAllByRole("radio")) {
      expect(radio).toHaveAttribute("aria-checked", "false");
    }
  });

  it("does not call a uniform legacy per-clip look mixed", () => {
    render(
      <StylesDrawer
        sampleText={null}
        appliedStyleSetId={null}
        availableLookPresets={["none", "golden_hour", "faded_analog"]}
        selectedLookPreset={null}
        lookPresetMixed={false}
        onSelectLook={jest.fn()}
      />,
    );

    expect(screen.queryByText("Mixed")).not.toBeInTheDocument();
    for (const radio of screen.getAllByRole("radio")) {
      expect(radio).toHaveAttribute("aria-checked", "false");
    }
  });
});
