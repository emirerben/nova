import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Switch } from "@/components/ui/switch";

describe("Switch", () => {
  it("renders a switch role and toggles on click", async () => {
    const user = userEvent.setup();
    render(<Switch aria-label="Auto-publish" className="extra-class" />);
    const el = screen.getByRole("switch", { name: "Auto-publish" });
    expect(el.className).toContain("extra-class");
    expect(el).toHaveAttribute("aria-checked", "false");
    await user.click(el);
    expect(el).toHaveAttribute("aria-checked", "true");
  });
});
