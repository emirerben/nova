import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

describe("Select", () => {
  it("renders a combobox trigger and opens to reveal options", async () => {
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

    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    expect(await screen.findByRole("option", { name: "Narrated" })).toBeInTheDocument();
  });
});
