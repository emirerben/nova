import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import { Button } from "@/components/ui/button";

describe("Button", () => {
  it("renders as a native button and forwards className", () => {
    render(<Button className="extra-class">Save</Button>);
    const btn = screen.getByRole("button", { name: "Save" });
    expect(btn.tagName).toBe("BUTTON");
    expect(btn.className).toContain("extra-class");
  });

  it("pins the ink/default size tokens (InkButton wrapper contract)", () => {
    render(<Button variant="ink">Save</Button>);
    const className = screen.getByRole("button", { name: "Save" }).className;
    expect(className).toContain("h-9");
    expect(className).toContain("px-4");
    expect(className).toContain("py-2");
    expect(className).toContain("bg-primary");
  });

  it("pins the sm size tokens", () => {
    render(<Button size="sm">Save</Button>);
    const className = screen.getByRole("button", { name: "Save" }).className;
    expect(className).toContain("h-8");
    expect(className).toContain("px-3");
    expect(className).toContain("text-xs");
  });

  it("default variant renders the same look as the explicit ink variant", () => {
    render(<Button>Save</Button>);
    const className = screen.getByRole("button", { name: "Save" }).className;
    expect(className).toContain("bg-primary");
    expect(className).toContain("text-primary-foreground");
  });

  it("destructive uses the stock destructive token", () => {
    render(<Button variant="destructive">Delete</Button>);
    const className = screen.getByRole("button", { name: "Delete" }).className;
    expect(className).toContain("bg-destructive");
    expect(className).toContain("text-destructive-foreground");
  });

  it("renders rounded-md, the stock shadcn default — not a pill", () => {
    render(<Button>Save</Button>);
    const className = screen.getByRole("button").className;
    expect(className).toContain("rounded-md");
    expect(className).not.toContain("rounded-full");
  });

  it("supports asChild for link-styled CTAs", () => {
    render(
      <Button asChild variant="outline">
        <a href="/plan">Open</a>
      </Button>
    );
    const link = screen.getByRole("link", { name: "Open" });
    expect(link.tagName).toBe("A");
    expect(link.className).toContain("rounded-md");
  });
});
