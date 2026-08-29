import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

// Radix opens Select on pointerdown, not click — userEvent.click dispatches
// the full pointer sequence jsdom needs (see jest.setup.ts polyfills).
describe("Select", () => {
  it("renders a combobox trigger and opens to reveal options", async () => {
    const user = userEvent.setup({ delay: null });
    render(
      <Select defaultValue="a">
        <SelectTrigger aria-label="Kind" className="extra-class">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="a">Montage</SelectItem>
          <SelectItem value="b">Narrated</SelectItem>
        </SelectContent>
      </Select>
    );

    const trigger = screen.getByRole("combobox", { name: "Kind" });
    expect(trigger.className).toContain("extra-class");

    await user.click(trigger);
    expect(await screen.findByRole("option", { name: "Narrated" })).toBeInTheDocument();
  });
});
