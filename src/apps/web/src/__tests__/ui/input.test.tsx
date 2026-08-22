import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import { Input } from "@/components/ui/input";

describe("Input", () => {
  it("renders a native input and forwards className", () => {
    render(<Input aria-label="Handle" className="extra-class" />);
    const input = screen.getByLabelText("Handle");
    expect(input.tagName).toBe("INPUT");
    expect(input.className).toContain("extra-class");
  });

  it("stays at/above the 16px iOS zoom-on-focus floor below sm", () => {
    render(<Input aria-label="Handle" />);
    const className = screen.getByLabelText("Handle").className;
    expect(className).toContain("text-base");
    expect(className).toContain("h-11");
  });
});
