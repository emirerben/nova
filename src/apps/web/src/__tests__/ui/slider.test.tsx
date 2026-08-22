import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Slider } from "@/components/ui/slider";

describe("Slider", () => {
  it("renders a slider role with the lime range + zinc track", async () => {
    const user = userEvent.setup();
    render(<Slider aria-label="Intensity" defaultValue={[50]} max={100} step={1} />);
    const thumb = screen.getByRole("slider", { name: "Intensity" });
    expect(thumb).toHaveAttribute("aria-valuenow", "50");

    thumb.focus();
    await user.keyboard("{ArrowRight}");
    expect(thumb).toHaveAttribute("aria-valuenow", "51");
  });
});
