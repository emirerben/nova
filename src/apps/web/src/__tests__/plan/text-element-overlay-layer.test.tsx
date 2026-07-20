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

  it("honors explicit shadow off when no stroke is present", () => {
    render(
      <TextElementOverlayLayer
        elements={[
          {
            ...element,
            stroke_width: 0,
            shadow_enabled: false,
          },
        ]}
      />,
    );

    expect(screen.getByText("READY NOW")).not.toHaveStyle({
      textShadow: "0 2px 8px rgba(0,0,0,0.55)",
    });
    expect(resolveTextElementsLayout([{ ...element, shadow_enabled: false }])[0].shadowEnabled).toBe(
      false,
    );
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
