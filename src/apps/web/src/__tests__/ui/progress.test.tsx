import { render } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import { Progress } from "@/components/ui/progress";

describe("Progress", () => {
  it("renders a progressbar role reflecting the value", () => {
    const { getByRole } = render(<Progress value={40} className="extra-class" />);
    const bar = getByRole("progressbar");
    expect(bar.className).toContain("extra-class");
    expect(bar.className).toContain("bg-primary/20");
  });
});
