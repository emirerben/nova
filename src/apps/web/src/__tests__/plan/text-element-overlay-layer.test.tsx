import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import TextElementOverlayLayer, {
  TextElementOverlayContent,
  textElementAnchorTransform,
  textElementWrapperStyle,
} from "@/app/plan/items/[id]/components/TextElementOverlayLayer";
import { resolveTextElementsLayout } from "@/lib/overlay-layout";
import type { TextElement } from "@/lib/plan-api";

jest.mock("@/lib/font-faces", () => ({ FONT_FACES: "" }));

const element: TextElement = {
  id: "hero",
  role: "generative_intro",
  text: "ready now",
  start_s: 1,
  end_s: 4,
  position: "custom",
  x_frac: 0.12,
  y_frac: 0.34,
  font_family: "PlayfairDisplay-Bold",
  size_px: 96,
  color: "#fed700",
  stroke_width: 3,
  alignment: "left",
  text_case: "upper",
  letter_spacing: 0.05,
  line_spacing: 1.6,
  max_width_frac: 0.42,
};

describe("TextElementOverlayLayer", () => {
  it("renders generated text with the shared layout rules used by editor and preview", () => {
    render(<TextElementOverlayLayer elements={[element]} />);

    const text = screen.getByText("READY NOW");
    const wrapper = text.parentElement;

    expect(wrapper).toHaveStyle({
      left: "12%",
      top: "34%",
      transform: "translate(0, -50%)",
      width: "42%",
    });
    expect(text).toHaveStyle({
      color: "#fed700",
      textAlign: "left",
      letterSpacing: "0.05em",
      lineHeight: "1.6",
      whiteSpace: "pre-wrap",
      wordBreak: "break-word",
    });
    expect(text.style.textShadow).toContain(
      "calc(2 * 0.052083333333333336cqh)",
    );
    expect(text.style.textShadow).toContain(
      "calc(8 * 0.052083333333333336cqh)",
    );
    expect(resolveTextElementsLayout([element])[0].strokeWidth).toBe(3);
  });

  it("uses the same alignment-aware wrapper helper the editor imports", () => {
    const [layout] = resolveTextElementsLayout([{ ...element, alignment: "right" }]);

    expect(textElementAnchorTransform("right")).toBe("translate(-100%, -50%)");
    expect(
      textElementWrapperStyle({
        layout,
        xFrac: 0.8,
        yFrac: 0.2,
        maxWidthFrac: 0.5,
        zIndex: 4,
      }),
    ).toMatchObject({
      left: "80%",
      top: "20%",
      transform: "translate(-100%, -50%)",
      width: "50%",
      zIndex: 4,
    });
  });

  it("applies explicit text rotation in the wrapper transform", () => {
    const [layout] = resolveTextElementsLayout([{ ...element, rotation_deg: 90 }]);

    expect(layout.rotationDeg).toBe(90);
    expect(textElementWrapperStyle({ layout })).toMatchObject({
      transform: "translate(0, -50%) rotate(90deg)",
    });
  });

  it("filters by current playback time when provided", () => {
    render(<TextElementOverlayLayer elements={[element]} currentTime={6} />);

    expect(screen.queryByText("READY NOW")).not.toBeInTheDocument();
  });

  it("animates authoritative ink-reveal elements from the video playhead", () => {
    const inkReveal = { ...element, effect: "ink-reveal" as const };
    const { container, rerender } = render(
      <TextElementOverlayLayer elements={[inkReveal]} currentTime={1} />,
    );
    const painted = () =>
      container.querySelector<HTMLElement>("[data-ink-reveal] > div");

    expect(painted()?.style.clipPath).toContain("100%");

    rerender(<TextElementOverlayLayer elements={[inkReveal]} currentTime={2.2} />);
    expect(painted()?.style.clipPath).toContain("calc(");
    expect(painted()?.style.clipPath).not.toContain("100%");

    rerender(<TextElementOverlayLayer elements={[inkReveal]} currentTime={3.3} />);
    expect(painted()?.style.clipPath).toBe("");
  });

  it.each(["left", "center", "right"] as const)(
    "reserves settled geometry for a partial reveal while preserving %s alignment",
    (alignment) => {
      const [layout] = resolveTextElementsLayout([{ ...element, alignment }]);

      const { container } = render(
        <TextElementOverlayContent
          layout={layout}
          fontSize="20px"
          reserveText={"READY NOW\nWRAPPED LINE"}
          showCursor
        >
          READY
        </TextElementOverlayContent>,
      );

      const visible = screen.getByText("READY");
      const remainder = container.querySelector("[data-reveal-remainder]");
      expect(visible.parentElement).toHaveStyle({ textAlign: alignment });
      expect(remainder).toHaveStyle({ visibility: "hidden" });
      expect(remainder).toHaveTextContent("NOW WRAPPED LINE");
      expect(container.querySelector('[style*="width: 0"]')).toHaveTextContent("|");
    },
  );

  it.each(["left", "center", "right"] as const)(
    "clips ink reveal on the shrink-to-fit painted node for %s alignment",
    (alignment) => {
      const [layout] = resolveTextElementsLayout([
        {
          ...element,
          alignment,
          text: "READY NOW\nWRAPPED LINE",
          rotation_deg: 8,
          letter_spacing: 0.05,
          effect: "ink-reveal",
        },
      ]);
      const { container } = render(
        <TextElementOverlayContent
          layout={layout}
          fontSize="20px"
          revealProgress={0.25}
        />,
      );

      const reveal = container.querySelector("[data-ink-reveal]");
      const painted = reveal?.firstElementChild;
      expect(reveal).toHaveStyle({
        display: "flex",
        justifyContent:
          alignment === "left"
            ? "flex-start"
            : alignment === "right"
              ? "flex-end"
              : "center",
      });
      expect(painted).toHaveStyle({
        display: "inline-block",
        width: "max-content",
        maxWidth: "100%",
        letterSpacing: "0.05em",
      });
      expect((painted as HTMLElement).style.clipPath).toContain("75%");
      expect((painted as HTMLElement).style.clipPath).toContain("calc(-");
      expect((painted as HTMLElement).style.willChange).toBe("clip-path");
    },
  );

  it("removes ink-reveal clipping and paint hints after settlement", () => {
    const [layout] = resolveTextElementsLayout([
      { ...element, text: "SETTLED", effect: "ink-reveal" },
    ]);
    const { container } = render(
      <TextElementOverlayContent layout={layout} fontSize="20px" revealProgress={1} />,
    );
    const painted = container.querySelector<HTMLElement>("[data-ink-reveal] > div");

    expect(painted?.style.clipPath).toBe("");
    expect(painted?.style.willChange).toBe("");
  });

  it("starts the ink-reveal clip at zero padded width", () => {
    const [layout] = resolveTextElementsLayout([
      { ...element, text: "INK", shadow_enabled: true, effect: "ink-reveal" },
    ]);
    const { container } = render(
      <TextElementOverlayContent layout={layout} fontSize="20px" revealProgress={0} />,
    );
    const painted = container.querySelector<HTMLElement>("[data-ink-reveal] > div");

    expect(painted?.style.clipPath).toContain("100%");
    expect(painted?.style.clipPath).toContain("+ 42");
  });

  it("draws handwriting as authored sequential SVG strokes", () => {
    const [layout] = resolveTextElementsLayout([
      {
        ...element,
        text: "WRITE",
        effect: "handwriting",
        glow_color: "#ff484c",
        glow_strength: 0.5,
        shadow_enabled: true,
      },
    ]);
    const { container, rerender } = render(
      <TextElementOverlayContent layout={layout} fontSize="20px" revealProgress={0} />,
    );

    const paths = () =>
      Array.from(
        container.querySelectorAll<SVGPathElement>(
          "[data-handwriting-strokes] g:last-of-type path",
        ),
      );
    const shadowGroups = container.querySelectorAll<SVGGElement>("[data-handwriting-shadow]");
    const glowGroups = container.querySelectorAll<SVGGElement>("[data-handwriting-glow]");
    expect(paths().length).toBeGreaterThan(5);
    expect(paths().every((path) => path.getAttribute("stroke-dashoffset") === "1")).toBe(true);
    expect(glowGroups).toHaveLength(2);
    expect(shadowGroups).toHaveLength(2);
    expect(shadowGroups[0]).toHaveAttribute("opacity", String(115 / 255));
    expect(shadowGroups[0]).toHaveAttribute("transform", `translate(0 ${8 / 96})`);
    expect(shadowGroups[1]).toHaveAttribute("opacity", String(200 / 255));
    expect(shadowGroups[1]).toHaveAttribute("transform", `translate(0 ${2 / 96})`);
    expect(
      container.querySelector('filter[id$="-shadow-0"] feGaussianBlur'),
    ).toHaveAttribute("stdDeviation", String(14 / 96));
    expect(
      container.querySelector('filter[id$="-shadow-1"] feGaussianBlur'),
    ).toHaveAttribute("stdDeviation", String(3 / 96));

    rerender(
      <TextElementOverlayContent layout={layout} fontSize="20px" revealProgress={0.5} />,
    );
    const middleOffsets = paths().map((path) =>
      Number(path.getAttribute("stroke-dashoffset")),
    );
    expect(middleOffsets.some((offset) => offset < 1)).toBe(true);
    expect(middleOffsets.some((offset) => offset === 1)).toBe(true);

    rerender(
      <TextElementOverlayContent layout={layout} fontSize="20px" revealProgress={1} />,
    );
    expect(paths().every((path) => path.getAttribute("stroke-dashoffset") === "0")).toBe(true);
  });

  it("honors explicit shadow off", () => {
    render(
      <TextElementOverlayLayer
        elements={[
          {
            ...element,
            shadow_enabled: false,
          },
        ]}
      />,
    );

    expect(screen.getByText("READY NOW").style.textShadow).toBe("");
    expect(resolveTextElementsLayout([{ ...element, shadow_enabled: false }])[0].shadowEnabled).toBe(
      false,
    );
  });

  it("removes both handwriting shadow path layers when disabled", () => {
    const [layout] = resolveTextElementsLayout([
      { ...element, effect: "handwriting", shadow_enabled: false },
    ]);
    const { container } = render(
      <TextElementOverlayContent layout={layout} fontSize="20px" revealProgress={1} />,
    );

    expect(container.querySelectorAll("[data-handwriting-shadow]")).toHaveLength(0);
  });

  it("keeps handwriting ink geometry anchored when shadow overflow is toggled", () => {
    const [withShadow] = resolveTextElementsLayout([
      { ...element, effect: "handwriting", shadow_enabled: true },
    ]);
    const [withoutShadow] = resolveTextElementsLayout([
      { ...element, effect: "handwriting", shadow_enabled: false },
    ]);
    const { container, rerender } = render(
      <TextElementOverlayContent layout={withShadow} fontSize="20px" revealProgress={1} />,
    );
    const before = container.querySelector<SVGSVGElement>("[data-handwriting-strokes]");
    const beforeGeometry = {
      viewBox: before?.getAttribute("viewBox"),
      width: before?.style.width,
      height: before?.style.height,
    };
    expect(beforeGeometry.viewBox?.startsWith("0 0 ")).toBe(true);

    rerender(
      <TextElementOverlayContent layout={withoutShadow} fontSize="20px" revealProgress={1} />,
    );
    const after = container.querySelector<SVGSVGElement>("[data-handwriting-strokes]");
    expect({
      viewBox: after?.getAttribute("viewBox"),
      width: after?.style.width,
      height: after?.style.height,
    }).toEqual(beforeGeometry);
  });

  it("matches renderer-authored italic font style and editorial glow", () => {
    render(
      <TextElementOverlayLayer
        elements={[
          {
            ...element,
            font_family: "Playfair Display Italic",
            stroke_width: 0,
            shadow_enabled: false,
            glow_color: "#7CFF8A",
            glow_strength: 0.8,
          },
        ]}
      />,
    );

    expect(screen.getByText("READY NOW")).toHaveStyle({
      fontStyle: "italic",
    });
    expect(screen.getByText("READY NOW").style.textShadow).toContain(
      "calc(8 * 0.052083333333333336cqh)",
    );
    expect(screen.getByText("READY NOW").style.textShadow).toContain(
      "calc(20 * 0.052083333333333336cqh)",
    );
  });
});
