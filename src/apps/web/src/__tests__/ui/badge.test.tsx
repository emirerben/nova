import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import { Badge } from "@/components/ui/badge";

describe("Badge", () => {
  it("renders as a pill, not the shadcn default rounded-md", () => {
    render(<Badge className="extra-class">Ready to post</Badge>);
    const badge = screen.getByText("Ready to post");
    expect(badge.className).toContain("rounded-full");
    expect(badge.className).toContain("extra-class");
  });

  it("lime-soft variant uses the soft lime pill tokens", () => {
    render(<Badge variant="lime-soft">Connected</Badge>);
    const className = screen.getByText("Connected").className;
    expect(className).toContain("bg-lime-50");
    expect(className).toContain("text-lime-800");
  });

  it("ink and zinc variants contain no red", () => {
    render(
      <>
        <Badge variant="ink">Approved</Badge>
        <Badge variant="zinc">Draft</Badge>
      </>
    );
    expect(screen.getByText("Approved").className.toLowerCase()).not.toContain("red");
    expect(screen.getByText("Draft").className.toLowerCase()).not.toContain("red");
  });
});
