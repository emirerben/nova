import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

describe("Tooltip", () => {
  it("opens on hover/focus and renders content at z-[130]", async () => {
    render(
      <TooltipProvider delayDuration={0}>
        <Tooltip>
          <TooltipTrigger aria-label="Reconnect">Reconnect</TooltipTrigger>
          <TooltipContent className="extra-class">Private beta</TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );

    fireEvent.focus(screen.getByRole("button", { name: "Reconnect" }));
    const content = await screen.findByText("Private beta");
    expect(content.className).toContain("z-[130]");
    expect(content.className).toContain("extra-class");
  });
});
