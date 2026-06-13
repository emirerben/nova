/**
 * LayoutPreviewCard (shared, src/components/variant-editor/LayoutPreviewCard.tsx):
 * - Classic + Editorial both render as role="radio" with the user's text
 * - selected → aria-checked + lime ring; selection fires onSelect
 * - disabled blocks the callback
 * - exposes the title (used by VariantCard for the gating hint / sync copy)
 *
 * The 3-6-word gating + hint live in VariantCard (it decides `disabled`/`title`);
 * those are covered in VariantCardEditMode.test.tsx. This suite tests the card
 * primitive in isolation.
 */

import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import { LayoutPreviewCard } from "@/components/variant-editor/LayoutPreviewCard";

// jsdom lacks ResizeObserver (the Editorial mock observes the tile to scale the
// real cluster geometry). A non-firing mock keeps width=0 → the card renders the
// representative static fallback (no canvas in jsdom), which is what these
// primitive-level assertions check.
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

describe("LayoutPreviewCard", () => {
  it("renders Classic with the user's hook text", () => {
    render(
      <LayoutPreviewCard
        kind="classic"
        text="your favorite place"
        selected
        onSelect={jest.fn()}
      />,
    );
    const card = screen.getByRole("radio", { name: "Classic layout" });
    expect(card).toBeInTheDocument();
    expect(card).toHaveTextContent("your favorite place");
    expect(card).toHaveTextContent("Classic");
  });

  it("renders Editorial as a staggered word-cluster mock of the hook", () => {
    render(
      <LayoutPreviewCard
        kind="editorial"
        text="what a view today"
        selected={false}
        onSelect={jest.fn()}
      />,
    );
    const card = screen.getByRole("radio", { name: "Editorial layout" });
    expect(card).toBeInTheDocument();
    // First few words of the hook appear in the mock.
    expect(card).toHaveTextContent("what");
    expect(card).toHaveTextContent("view");
    expect(card).toHaveTextContent("Editorial");
  });

  it("selected → aria-checked + lime ring; unselected has no ring", () => {
    const { rerender } = render(
      <LayoutPreviewCard kind="classic" text="hi" selected onSelect={jest.fn()} />,
    );
    const card = screen.getByRole("radio", { name: "Classic layout" });
    expect(card).toHaveAttribute("aria-checked", "true");
    expect(card.className).toContain("ring-lime-600");

    rerender(<LayoutPreviewCard kind="classic" text="hi" selected={false} onSelect={jest.fn()} />);
    const card2 = screen.getByRole("radio", { name: "Classic layout" });
    expect(card2).toHaveAttribute("aria-checked", "false");
    expect(card2.className).not.toContain("ring-lime-600");
  });

  it("selection fires the callback; disabled blocks it", () => {
    const onSelect = jest.fn();
    const { rerender } = render(
      <LayoutPreviewCard kind="editorial" text="x y z" selected={false} onSelect={onSelect} />,
    );
    fireEvent.click(screen.getByRole("radio", { name: "Editorial layout" }));
    expect(onSelect).toHaveBeenCalledTimes(1);

    rerender(
      <LayoutPreviewCard
        kind="editorial"
        text="x y z"
        selected={false}
        disabled
        title="needs a 3-6 word hook"
        onSelect={onSelect}
      />,
    );
    const card = screen.getByRole("radio", { name: "Editorial layout" });
    expect(card).toBeDisabled();
    expect(card).toHaveAttribute("title", "needs a 3-6 word hook");
    fireEvent.click(card);
    expect(onSelect).toHaveBeenCalledTimes(1); // disabled swallows the click
  });

  it("falls back to placeholder copy when the hook is empty", () => {
    render(<LayoutPreviewCard kind="classic" text="" selected={false} onSelect={jest.fn()} />);
    expect(screen.getByRole("radio", { name: "Classic layout" })).toHaveTextContent("Your hook");
  });
});
