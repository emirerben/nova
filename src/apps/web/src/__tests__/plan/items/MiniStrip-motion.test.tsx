import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import { MiniStrip } from "@/app/plan/items/[id]/_editor/MiniStrip";

describe("Pocket MiniStrip Creator Block regions", () => {
  it("renders exact regions and selects the block without selecting its clip", () => {
    const onSelectClip = jest.fn();
    const onSelectMark = jest.fn();
    render(
      <MiniStrip
        segments={[{ id: "clip-1", startS: 0, endS: 6, hasMarks: true }]}
        marks={[{ id: "motion-1", startS: 1, endS: 3.5, label: "Wild Type" }]}
        durationS={6}
        currentTimeS={1}
        selectedMarkId="motion-1"
        onScrub={jest.fn()}
        onSelectClip={onSelectClip}
        onSelectMark={onSelectMark}
      />,
    );

    const region = screen.getByRole("button", { name: /Wild Type/ });
    expect(region).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(region);
    expect(onSelectMark).toHaveBeenCalledWith("motion-1", 1);
    expect(onSelectClip).not.toHaveBeenCalled();
  });
});
