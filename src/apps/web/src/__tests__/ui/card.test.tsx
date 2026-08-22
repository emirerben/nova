import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import { Card, CardContent } from "@/components/ui/card";

describe("Card", () => {
  it("renders the stock shadcn card shell", () => {
    render(
      <Card className="extra-class">
        <CardContent>Hello</CardContent>
      </Card>
    );
    const card = screen.getByText("Hello").parentElement;
    expect(card).not.toBeNull();
    expect(card!.className).toContain("rounded-xl");
    expect(card!.className).toContain("bg-card");
    expect(card!.className).toContain("extra-class");
  });
});
