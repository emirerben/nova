import { render, screen } from "@testing-library/react";
import userEvent, { PointerEventsCheckLevel } from "@testing-library/user-event";
import { describe, expect, it } from "@jest/globals";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

describe("Tooltip", () => {
  it("opens on hover/focus and renders content at z-[130]", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    render(
      <TooltipProvider delayDuration={0}>
        <Tooltip>
          <TooltipTrigger aria-label="Reconnect">Reconnect</TooltipTrigger>
          <TooltipContent className="extra-class">Private beta</TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );

    await user.hover(screen.getByRole("button", { name: "Reconnect" }));
    const content = await screen.findByText("Private beta");
    expect(content.className).toContain("z-[130]");
    expect(content.className).toContain("extra-class");
  });
});
