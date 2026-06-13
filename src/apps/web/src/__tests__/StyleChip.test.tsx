/**
 * StyleChip (shared, src/components/ui/StyleChip.tsx):
 * - renders sample text in the style's css_family + color (intro role wins)
 * - dark-tile variant renders the sample on the near-black inner tile
 * - selected → aria-checked + lime ring; click fires onSelect
 */

import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import StyleChip from "@/components/ui/StyleChip";
import type { GenerativeStyleSet } from "@/lib/generative-api";

function makeStyle(over: Partial<GenerativeStyleSet> = {}): GenerativeStyleSet {
  return {
    id: "travel_editorial",
    label: "Travel Editorial",
    tags: [],
    css_family: "'Playfair Display', serif",
    text_color: "#111111",
    font_weight: 700,
    intro: {
      css_family: "'Playfair Display', serif",
      text_color: "#FFFFFF",
      font_weight: 700,
    },
    ...over,
  };
}

describe("StyleChip", () => {
  it("renders the sample text in the style's intro-role font + color", () => {
    render(
      <StyleChip
        styleSet={makeStyle()}
        selected={false}
        sampleText="my live hook"
        onSelect={jest.fn()}
      />,
    );
    const sample = screen.getByText("my live hook");
    expect(sample).toHaveStyle({ fontFamily: "'Playfair Display', serif" });
    // Intro role text_color (#FFFFFF) wins over the representative role (#111111).
    expect(sample.style.color).toBe("rgb(255, 255, 255)");
  });

  it("truncates the sample to ~22 chars and falls back to the label then 'Aa'", () => {
    const { rerender } = render(
      <StyleChip
        styleSet={makeStyle()}
        selected={false}
        sampleText="this is a really long hook that should be cut off"
        onSelect={jest.fn()}
      />,
    );
    // sliced to 22 chars ("this is a really long "); getByText trims the
    // trailing space, so match the normalized form.
    expect(screen.getByText("this is a really long")).toBeInTheDocument();

    rerender(<StyleChip styleSet={makeStyle()} selected={false} onSelect={jest.fn()} />);
    // No sample → label is the preview (and also the muted sub-label).
    expect(screen.getAllByText("Travel Editorial").length).toBeGreaterThan(0);

    rerender(
      <StyleChip
        styleSet={makeStyle({ label: "" })}
        selected={false}
        onSelect={jest.fn()}
      />,
    );
    expect(screen.getByText("Aa")).toBeInTheDocument();
  });

  it("dark-tile variant renders the sample on the near-black inner tile", () => {
    render(
      <StyleChip
        styleSet={makeStyle()}
        selected={false}
        sampleText="hook"
        darkTile
        onSelect={jest.fn()}
      />,
    );
    const sample = screen.getByText("hook");
    // The sample sits inside a bg-[#0c0c0e] tile (the parent span).
    expect(sample.parentElement?.className).toContain("bg-[#0c0c0e]");
  });

  it("plain (non-dark) variant does NOT wrap the sample in a dark tile", () => {
    render(
      <StyleChip
        styleSet={makeStyle()}
        selected={false}
        sampleText="hook"
        onSelect={jest.fn()}
      />,
    );
    const sample = screen.getByText("hook");
    expect(sample.parentElement?.className ?? "").not.toContain("bg-[#0c0c0e]");
  });

  it("selected → aria-checked true + lime ring; unselected → no ring", () => {
    const { rerender } = render(
      <StyleChip styleSet={makeStyle()} selected onSelect={jest.fn()} />,
    );
    const chip = screen.getByRole("radio", { name: /Text style: Travel Editorial/ });
    expect(chip).toHaveAttribute("aria-checked", "true");
    expect(chip.className).toContain("ring-1");
    expect(chip.className).toContain("ring-lime-600");

    rerender(<StyleChip styleSet={makeStyle()} selected={false} onSelect={jest.fn()} />);
    const chip2 = screen.getByRole("radio", { name: /Text style: Travel Editorial/ });
    expect(chip2).toHaveAttribute("aria-checked", "false");
    expect(chip2.className).not.toContain("ring-lime-600");
  });

  it("click fires onSelect; disabled blocks it", () => {
    const onSelect = jest.fn();
    const { rerender } = render(
      <StyleChip styleSet={makeStyle()} selected={false} onSelect={onSelect} />,
    );
    fireEvent.click(screen.getByRole("radio"));
    expect(onSelect).toHaveBeenCalledTimes(1);

    rerender(<StyleChip styleSet={makeStyle()} selected={false} disabled onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("radio"));
    expect(onSelect).toHaveBeenCalledTimes(1); // still 1 — disabled swallows the click
  });
});
