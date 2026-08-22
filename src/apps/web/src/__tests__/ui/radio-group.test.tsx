import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";

describe("RadioGroup", () => {
  it("renders radio roles and selects on click", async () => {
    const user = userEvent.setup();
    render(
      <RadioGroup defaultValue="a" aria-label="Style" className="extra-class">
        <RadioGroupItem value="a" aria-label="Classic" />
        <RadioGroupItem value="b" aria-label="Bold" />
      </RadioGroup>
    );
    const group = screen.getByRole("radiogroup", { name: "Style" });
    expect(group.className).toContain("extra-class");

    const bold = screen.getByRole("radio", { name: "Bold" });
    expect(bold).toHaveAttribute("aria-checked", "false");
    await user.click(bold);
    expect(bold).toHaveAttribute("aria-checked", "true");
  });
});
