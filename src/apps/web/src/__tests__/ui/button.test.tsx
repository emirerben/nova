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
    expect(className).toContain("px-9");
    expect(className).toContain("py-[15px]");
    expect(className).toContain("text-[15px]");
    expect(className).toContain("bg-[#0c0c0e]");
  });

  it("pins the sm size tokens", () => {
    render(<Button size="sm">Save</Button>);
    const className = screen.getByRole("button", { name: "Save" }).className;
    expect(className).toContain("h-9");
    expect(className).toContain("px-5");
    expect(className).toContain("text-[13px]");
  });

  it("default variant renders the same look as the explicit ink variant", () => {
    render(<Button>Save</Button>);
    const className = screen.getByRole("button", { name: "Save" }).className;
    expect(className).toContain("bg-[#0c0c0e]");
    expect(className).toContain("text-white");
  });

  it("destructive contains no red — D10 'no red walls'", () => {
    render(<Button variant="destructive">Delete</Button>);
    const className = screen.getByRole("button", { name: "Delete" }).className;
    expect(className.toLowerCase()).not.toContain("red");
    expect(className).toContain("bg-[#3f3f46]");
  });

  it("renders pills, not the shadcn default rounded-md", () => {
    render(<Button>Save</Button>);
    expect(screen.getByRole("button").className).toContain("rounded-full");
  });

  it("supports asChild for link-styled CTAs", () => {
    render(
      <Button asChild variant="outline">
        <a href="/plan">Open</a>
      </Button>
    );
    const link = screen.getByRole("link", { name: "Open" });
    expect(link.tagName).toBe("A");
    expect(link.className).toContain("rounded-full");
  });
});
