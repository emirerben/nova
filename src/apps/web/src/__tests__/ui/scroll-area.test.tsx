import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { ScrollArea } from "@/components/ui/scroll-area";

describe("ScrollArea", () => {
  it("renders its viewport content and forwards className to the root", () => {
    const { container } = render(
      <ScrollArea className="extra-class">
        <div>Row content</div>
      </ScrollArea>
    );
    expect(screen.getByText("Row content")).toBeInTheDocument();
    const root = container.firstElementChild as HTMLElement;
    expect(root.className).toContain("extra-class");
  });
});
