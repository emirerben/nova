import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "@jest/globals";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

describe("Popover", () => {
  it("opens on click and renders content at z-[130]", async () => {
    const user = userEvent.setup({ delay: null });
    render(
      <Popover>
        <PopoverTrigger aria-label="Info">i</PopoverTrigger>
        <PopoverContent className="extra-class">Helper copy</PopoverContent>
      </Popover>
    );

    await user.click(screen.getByRole("button", { name: "Info" }));
    const content = await screen.findByText("Helper copy");
    expect(content.className).toContain("z-[130]");
    expect(content.className).toContain("extra-class");
  });
});
