import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Checkbox } from "@/components/ui/checkbox";

describe("Checkbox", () => {
  it("renders a checkbox role and toggles on click", async () => {
    const user = userEvent.setup();
    render(<Checkbox aria-label="Agree" className="extra-class" />);
    const box = screen.getByRole("checkbox", { name: "Agree" });
    expect(box.className).toContain("extra-class");
    expect(box).toHaveAttribute("aria-checked", "false");
    await user.click(box);
    expect(box).toHaveAttribute("aria-checked", "true");
  });
});
