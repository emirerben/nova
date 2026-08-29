import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

describe("Popover", () => {
  it("opens on click and renders content at z-[130]", async () => {
    render(
      <Popover>
        <PopoverTrigger aria-label="Info">i</PopoverTrigger>
        <PopoverContent className="extra-class">Helper copy</PopoverContent>
      </Popover>
    );

    fireEvent.click(screen.getByRole("button", { name: "Info" }));
    const content = await screen.findByText("Helper copy");
    expect(content.className).toContain("z-[130]");
    expect(content.className).toContain("extra-class");
  });
});
