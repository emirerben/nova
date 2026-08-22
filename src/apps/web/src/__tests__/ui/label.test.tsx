import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { Label } from "@/components/ui/label";

describe("Label", () => {
  it("renders a LABEL element associated with its control", () => {
    render(
      <>
        <Label htmlFor="tell-kria" className="extra-class">
          Tell Kria
        </Label>
        <input id="tell-kria" />
      </>
    );
    const label = screen.getByText("Tell Kria");
    expect(label.tagName).toBe("LABEL");
    expect(label.className).toContain("extra-class");
    expect(screen.getByLabelText("Tell Kria")).toBeInTheDocument();
  });
});
