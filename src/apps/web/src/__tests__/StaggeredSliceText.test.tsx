import { act, render } from "@testing-library/react";
import { StaggeredSliceText } from "@/components/variant-editor/StaggeredSliceText";

describe("StaggeredSliceText", () => {
  it("keeps final geometry reserved while rendering every line as glyphs", () => {
    const { container } = render(
      <StaggeredSliceText text={"GOAL OF THE\nTOURNAMENT"} tLocal={1.6} durationS={4} />,
    );

    expect(container.querySelectorAll("[data-staggered-slice-line]")).toHaveLength(2);
    expect(container.querySelectorAll("[data-staggered-slice-glyph]")).toHaveLength(21);
    expect(container.querySelectorAll("[data-staggered-slice-band]")).toHaveLength(0);
    expect(container.querySelectorAll('[data-staggered-slice-line="glyphs"]')[1]?.textContent).toContain(
      "TOURNAMENT",
    );
  });

  it("interpolates frames while media timeupdate remains unchanged", () => {
    let frame: FrameRequestCallback | null = null;
    const requestFrame = jest
      .spyOn(window, "requestAnimationFrame")
      .mockImplementation((callback) => {
        frame = callback;
        return 1;
      });
    const cancelFrame = jest.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});

    const { container } = render(
      <StaggeredSliceText text="GOAL" tLocal={0} durationS={2} playing />,
    );
    const firstGlyph = container.querySelector<HTMLElement>("[data-staggered-slice-glyph]");
    expect(firstGlyph?.style.opacity).toBe("0");

    const baseTime = performance.now();
    act(() => frame?.(baseTime + 80));

    expect(Number(firstGlyph?.style.opacity)).toBeGreaterThan(0);
    expect(requestFrame).toHaveBeenCalledTimes(2);

    requestFrame.mockRestore();
    cancelFrame.mockRestore();
  });
});
