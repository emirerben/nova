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

  it("filters by current playback time when provided", () => {
    render(<TextElementOverlayLayer elements={[element]} currentTime={6} />);

    expect(screen.queryByText("READY NOW")).not.toBeInTheDocument();
  });

  it("can reserve full text geometry while a typewriter reveal grows from the left", () => {
    const [layout] = resolveTextElementsLayout([{ ...element, alignment: "center" }]);

    render(
      <TextElementOverlayContent
        layout={layout}
        fontSize="20px"
        reserveText="READY NOW"
        textAlignOverride="left"
      >
        READY
      </TextElementOverlayContent>,
    );

    const reserve = screen.getByText("READY NOW");
    const visible = screen.getByText("READY");
    expect(reserve).toHaveStyle({ visibility: "hidden" });
    expect(visible).toHaveStyle({
      position: "absolute",
      inset: "0.08em 0.18em",
    });
    expect(visible.parentElement).toHaveStyle({
      position: "relative",
      textAlign: "left",
    });
  });

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
});
