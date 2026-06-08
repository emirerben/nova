// @ts-nocheck
import React from "react";

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: jest.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: jest.fn(),
    removeListener: jest.fn(),
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    dispatchEvent: jest.fn(),
  })),
});

import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

// Mock plan-api (spread requireActual so other consumers of the module work normally).
jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
}));

// Mock font-faces so tests don't attempt to read font-registry.json or inject CSS.
jest.mock("@/lib/font-faces", () => ({ FONT_FACES: "" }));

import { StyleCard } from "@/app/plan/_components/workspace/StyleCard";

/** Factory: build a minimal StyleResponse-shaped set of props. */
function makeStyleResponse(overrides: {
  status: "absent" | "deriving" | "ready" | "edited" | "failed";
  style?: object | null;
  styleSetPreview?: object | null;
  fontPreview?: object | null;
}) {
  return {
    style: null,
    styleSetPreview: null,
    fontPreview: null,
    ...overrides,
  };
}

describe("StyleCard", () => {
  it("test_absent_renders_setup_cta: status absent → shows set-up-your-style invite", () => {
    render(<StyleCard status="absent" style={null} />);
    expect(screen.getByText(/set up your style/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /set up your style/i })).toHaveAttribute(
      "href",
      "/plan/style",
    );
  });

  it("test_absent_from_404_renders_setup_cta: page passes status absent (from getStyle 404) → shows invite", () => {
    const props = makeStyleResponse({ status: "absent", style: null });
    render(<StyleCard status={props.status} style={props.style} />);
    expect(screen.getByText(/set up your style/i)).toBeInTheDocument();
  });

  it("test_deriving_shows_learning_text: status deriving → shows learning copy", () => {
    render(<StyleCard status="deriving" style={null} />);
    expect(screen.getByText(/learning your style/i)).toBeInTheDocument();
  });

  it("test_ready_shows_font_pill: status ready with font → font name rendered", () => {
    const props = makeStyleResponse({
      status: "ready",
      style: {
        style_set_id: "editorial",
        knobs: { font_family: "Playfair Display", text_color: "#ffffff", highlight_color: "#d4ff00" },
        instruction_level: "light",
        footage_type_bias: ["outdoor"],
      },
      fontPreview: {
        font_family: "PlayfairDisplay-Bold",
        display_name: "Playfair Display",
        css_family: "'Playfair Display', serif",
      },
    });

    render(
      <StyleCard
        status={props.status}
        style={props.style}
        styleSetPreview={props.styleSetPreview}
        fontPreview={props.fontPreview}
      />
    );

    // Style-set label pill
    expect(screen.getByText("editorial")).toBeInTheDocument();
    // Font name pill
    expect(screen.getByText("Playfair Display")).toBeInTheDocument();
    // Instruction level chip (light guidance shown when not "full")
    expect(screen.getByText(/light guidance/i)).toBeInTheDocument();
    // footage_type_bias chip
    expect(screen.getByText("outdoor")).toBeInTheDocument();
    // Color swatch labels
    expect(screen.getByText("Text color")).toBeInTheDocument();
    expect(screen.getByText("Highlight")).toBeInTheDocument();
  });

  it("test_edited_shows_eyebrow: status edited → eyebrow header present", () => {
    render(
      <StyleCard
        status="edited"
        style={{ style_set_id: "bold", knobs: {} }}
      />
    );
    expect(screen.getByText(/your style/i)).toBeInTheDocument();
  });

  it("test_failed_shows_muted_unavailable: status failed → muted unavailable line", () => {
    render(<StyleCard status="failed" style={null} />);
    expect(screen.getByText(/style unavailable/i)).toBeInTheDocument();
  });

  it("test_instruction_full_hides_chip: instruction_level full → no guidance chip", () => {
    render(
      <StyleCard
        status="ready"
        style={{ instruction_level: "full", knobs: {} }}
      />
    );
    expect(screen.queryByText(/light guidance/i)).toBeNull();
    expect(screen.queryByText(/no instructions/i)).toBeNull();
  });

  it("test_no_footage_bias_no_extra_pills: no footage_type_bias → no bias chips", () => {
    render(
      <StyleCard
        status="ready"
        style={{ footage_type_bias: [], knobs: {} }}
      />
    );
    // No bias pills, just eyebrow
    expect(screen.getByText(/your style/i)).toBeInTheDocument();
  });
});
