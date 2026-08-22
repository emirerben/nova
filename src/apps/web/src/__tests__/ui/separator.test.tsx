import { render } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import { Separator } from "@/components/ui/separator";

describe("Separator", () => {
  it("renders a decorative separator and forwards className", () => {
    const { container } = render(<Separator className="extra-class" />);
    const el = container.firstElementChild as HTMLElement;
    expect(el).not.toBeNull();
    expect(el.className).toContain("extra-class");
    expect(el.className).toContain("bg-border");
  });
});
