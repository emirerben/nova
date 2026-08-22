import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Toggle } from "@/components/ui/toggle";

describe("Toggle", () => {
  it("renders a toggle button and flips pressed state on click", async () => {
    const user = userEvent.setup();
    render(<Toggle aria-label="Bold" className="extra-class" />);
    const el = screen.getByRole("button", { name: "Bold" });
    expect(el.className).toContain("extra-class");
    expect(el).toHaveAttribute("aria-pressed", "false");
    await user.click(el);
    expect(el).toHaveAttribute("aria-pressed", "true");
  });
});
