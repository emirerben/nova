import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";

describe("Sheet", () => {
  it("renders a dialog role with the Kria z-[100] treatment when open", () => {
    render(
      <Sheet open>
        <SheetContent side="bottom" className="extra-class">
          <SheetTitle>Plan with Kria</SheetTitle>
        </SheetContent>
      </Sheet>
    );
    const sheet = screen.getByRole("dialog", { name: "Plan with Kria" });
    expect(sheet.className).toContain("z-[100]");
    expect(sheet.className).toContain("rounded-t-2xl");
    expect(sheet.className).toContain("extra-class");
  });
});
