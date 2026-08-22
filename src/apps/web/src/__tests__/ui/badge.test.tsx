import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import { Badge } from "@/components/ui/badge";

describe("Badge", () => {
  it("renders as rounded-md, the stock shadcn default — not a pill", () => {
    render(<Badge className="extra-class">Ready to post</Badge>);
    const badge = screen.getByText("Ready to post");
    expect(badge.className).toContain("rounded-md");
    expect(badge.className).not.toContain("rounded-full");
    expect(badge.className).toContain("extra-class");
  });

  it("lime-soft variant is aliased to the stock secondary look", () => {
    render(<Badge variant="lime-soft">Connected</Badge>);
    const className = screen.getByText("Connected").className;
    expect(className).toContain("bg-secondary");
    expect(className).toContain("text-secondary-foreground");
  });

  it("ink and zinc variants render their aliased stock base", () => {
    render(
      <>
        <Badge variant="ink">Approved</Badge>
        <Badge variant="zinc">Draft</Badge>
      </>
    );
    expect(screen.getByText("Approved").className).toContain("bg-primary");
    expect(screen.getByText("Draft").className).toContain("text-foreground");
  });
});
