import "@testing-library/jest-dom";
import type { ComponentProps } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import ToolDrawer from "@/app/plan/items/[id]/_editor/ToolDrawer";

function renderTextDrawer(overrides: Partial<ComponentProps<typeof ToolDrawer>> = {}) {
  const props: ComponentProps<typeof ToolDrawer> = {
    tool: "text",
    sampleWord: "NOVA",
    appliedPresetId: null,
    onAddText: jest.fn(),
    onPickPreset: jest.fn(),
    onClose: jest.fn(),
    ...overrides,
  };
  return render(<ToolDrawer {...props} />);
}

describe("ToolDrawer smart text composition", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("runs smart placement across existing text blocks", () => {
    const onSmartPlaceAll = jest.fn();
    renderTextDrawer({ onSmartPlaceAll, smartPlaceAllAvailable: true });

    fireEvent.click(screen.getByRole("button", { name: "Smart place all" }));

    expect(onSmartPlaceAll).toHaveBeenCalledTimes(1);
  });

  it("splits a drafted title and clears it after the shell accepts it", () => {
    const onSplitSmartPlaceText = jest.fn(() => true);
    renderTextDrawer({ onSplitSmartPlaceText, splitSmartPlaceAvailable: true });

    const draft = screen.getByLabelText("Composition text");
    fireEvent.change(draft, { target: { value: "take the scenic route home" } });
    fireEvent.click(screen.getByRole("button", { name: "Split & place" }));

    expect(onSplitSmartPlaceText).toHaveBeenCalledWith("take the scenic route home");
    expect(draft).toHaveValue("");
  });

  it("keeps the draft when the shell rejects the split", () => {
    const onSplitSmartPlaceText = jest.fn(() => false);
    renderTextDrawer({ onSplitSmartPlaceText, splitSmartPlaceAvailable: true });

    const draft = screen.getByLabelText("Composition text");
    fireEvent.change(draft, { target: { value: "keep this" } });
    fireEvent.click(screen.getByRole("button", { name: "Split & place" }));

    expect(draft).toHaveValue("keep this");
  });
});
