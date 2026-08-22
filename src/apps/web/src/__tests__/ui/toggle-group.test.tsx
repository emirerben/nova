import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

describe("ToggleGroup", () => {
  it("renders a group of toggle buttons and selects on click", async () => {
    const user = userEvent.setup();
    render(
      <ToggleGroup type="single" defaultValue="left" aria-label="Align">
        <ToggleGroupItem value="left" aria-label="Left" />
        <ToggleGroupItem value="right" aria-label="Right" />
      </ToggleGroup>
    );
    const right = screen.getByRole("radio", { name: "Right" });
    expect(right).toHaveAttribute("aria-checked", "false");
    await user.click(right);
    expect(right).toHaveAttribute("aria-checked", "true");
  });
});
