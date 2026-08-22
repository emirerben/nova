import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import { Textarea } from "@/components/ui/textarea";

describe("Textarea", () => {
  it("renders a native textarea and forwards className", () => {
    render(<Textarea aria-label="Tell Kria" className="extra-class" />);
    const el = screen.getByLabelText("Tell Kria");
    expect(el.tagName).toBe("TEXTAREA");
    expect(el.className).toContain("extra-class");
  });

  it("stays at/above the 16px iOS zoom-on-focus floor below sm", () => {
    render(<Textarea aria-label="Tell Kria" />);
    const className = screen.getByLabelText("Tell Kria").className;
    expect(className).toContain("text-base");
  });
});
