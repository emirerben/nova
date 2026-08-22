import { render } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import { Skeleton } from "@/components/ui/skeleton";

describe("Skeleton", () => {
  it("renders a pulsing placeholder block and forwards className", () => {
    const { container } = render(<Skeleton className="h-4 w-full extra-class" />);
    const el = container.firstElementChild as HTMLElement;
    expect(el.className).toContain("animate-pulse");
    expect(el.className).toContain("extra-class");
  });
});
