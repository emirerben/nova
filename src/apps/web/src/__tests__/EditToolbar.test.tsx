/**
 * EditToolbar (shared, src/components/variant-editor/EditToolbar.tsx):
 * - Font picker renders one button per INTRO_FONTS entry (linear layout)
 * - Animation picker renders one chip per INTRO_ANIMATIONS entry (linear layout)
 * - Color picker renders an <input type="color">
 * - Text size slider renders for linear layout; hidden for cluster layout
 * - Cluster layout shows Hero/Body/Accent font pickers + 3 per-role size sliders
 * - Done button disabled when draft is clean
 * - Remove / Add text back toggle
 */

import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { EditToolbar } from "@/components/variant-editor/EditToolbar";
import { INTRO_FONTS, INTRO_ANIMATIONS } from "@/lib/overlay-constants";
import type { VariantEditSession } from "@/lib/variant-editor/useVariantEditSession";
import type { GenerativeStyleSet } from "@/lib/generative-api";

function makeSession(over: Partial<VariantEditSession> = {}): VariantEditSession {
  return {
    isEditing: true,
    isSaving: false,
    justSaved: false,
    isActive: true,
    draft: {
      text: "wish you were here",
      removed: false,
      styleSetId: "a",
      sizePx: 56,
      layout: null,
      fontFamily: null,
      animation: null,
      textColor: null,
      clusterHeroFont: null,
      clusterBodyFont: null,
      clusterAccentFont: null,
      clusterHeroSizePx: null,
      clusterBodySizePx: null,
      clusterAccentSizePx: null,
      behindSubject: false,
    },
    isDirty: false,
    commitError: null,
    enterEdit: jest.fn(),
    cancel: jest.fn(),
    setText: jest.fn(),
    setRemoved: jest.fn(),
    setStyle: jest.fn(),
    setSize: jest.fn(),
    setLayout: jest.fn(),
    setFont: jest.fn(),
    setAnimation: jest.fn(),
    setColor: jest.fn(),
    setClusterHeroFont: jest.fn(),
    setClusterBodyFont: jest.fn(),
    setClusterAccentFont: jest.fn(),
    setClusterHeroSizePx: jest.fn(),
    setClusterBodySizePx: jest.fn(),
    setClusterAccentSizePx: jest.fn(),
    setBehindSubject: jest.fn(),
    playToken: 0,
    replay: jest.fn(),
    commit: jest.fn(async () => {}),
    ...over,
  };
}

const STYLE_SETS: GenerativeStyleSet[] = [
  { id: "a", label: "Editorial", tags: [], intro: { css_family: "'Playfair Display', serif", text_color: "#FFF" } },
  { id: "b", label: "Bold", tags: [], intro: { css_family: "'Inter', sans-serif", text_color: "#FFF" } },
];

describe("EditToolbar — linear layout", () => {
  it("renders one font button per INTRO_FONTS entry", () => {
    render(<EditToolbar session={makeSession()} styleSets={STYLE_SETS} fallbackSizePx={56} />);
    // Each font shows its name as a button label.
    for (const f of INTRO_FONTS) {
      expect(screen.getByRole("button", { name: f.name })).toBeInTheDocument();
    }
  });

  it("renders one animation chip per INTRO_ANIMATIONS entry", () => {
    render(<EditToolbar session={makeSession()} styleSets={STYLE_SETS} fallbackSizePx={56} />);
    for (const a of INTRO_ANIMATIONS) {
      expect(screen.getByRole("button", { name: a.label })).toBeInTheDocument();
    }
  });

  it("renders a color input and a text size range slider", () => {
    render(<EditToolbar session={makeSession()} styleSets={STYLE_SETS} fallbackSizePx={56} />);
    expect(screen.getByRole("slider", { name: /intro text size/i })).toBeInTheDocument();
    expect(document.querySelector("input[type='color']")).toBeInTheDocument();
  });

  it("Done button is disabled when draft is clean", () => {
    render(<EditToolbar session={makeSession({ isDirty: false })} styleSets={[]} fallbackSizePx={56} />);
    expect(screen.getByRole("button", { name: /done/i })).toBeDisabled();
  });

  it("Done button is enabled when draft is dirty", () => {
    render(<EditToolbar session={makeSession({ isDirty: true })} styleSets={[]} fallbackSizePx={56} />);
    expect(screen.getByRole("button", { name: /done/i })).not.toBeDisabled();
  });

  it("hides text controls when removed=true", () => {
    render(
      <EditToolbar
        session={makeSession({
          draft: {
            text: "",
            removed: true,
            styleSetId: null,
            sizePx: 56,
            layout: null,
            fontFamily: null,
            animation: null,
            textColor: null,
            clusterHeroFont: null,
            clusterBodyFont: null,
            clusterAccentFont: null,
            clusterHeroSizePx: null,
            clusterBodySizePx: null,
            clusterAccentSizePx: null,
            behindSubject: false,
          },
        })}
        styleSets={STYLE_SETS}
        fallbackSizePx={56}
      />,
    );
    // Font buttons are hidden when text is removed.
    for (const f of INTRO_FONTS) {
      expect(screen.queryByRole("button", { name: f.name })).not.toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: /add text back/i })).toBeInTheDocument();
  });
});

describe("EditToolbar — cluster layout", () => {
  function makeClusterSession(over: Partial<VariantEditSession> = {}) {
    return makeSession({
      draft: {
        text: "what a day",
        removed: false,
        styleSetId: null,
        sizePx: 60,
        layout: "cluster",
        fontFamily: null,
        animation: null,
        textColor: null,
        clusterHeroFont: null,
        clusterBodyFont: null,
        clusterAccentFont: null,
        clusterHeroSizePx: null,
        clusterBodySizePx: null,
        clusterAccentSizePx: null,
        behindSubject: false,
      },
      ...over,
    });
  }

  it("shows Hero font, Body font, and Accent font section labels", () => {
    render(<EditToolbar session={makeClusterSession()} styleSets={[]} fallbackSizePx={60} />);
    expect(screen.getByText(/hero font/i)).toBeInTheDocument();
    expect(screen.getByText(/body font/i)).toBeInTheDocument();
    expect(screen.getByText(/accent font/i)).toBeInTheDocument();
  });

  it("shows per-role size sliders (Hero / Body / Accent)", () => {
    render(<EditToolbar session={makeClusterSession()} styleSets={[]} fallbackSizePx={60} />);
    expect(screen.getByRole("slider", { name: /hero text size/i })).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: /body text size/i })).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: /accent text size/i })).toBeInTheDocument();
  });

  it("hides the global text size slider in cluster mode", () => {
    render(<EditToolbar session={makeClusterSession()} styleSets={[]} fallbackSizePx={60} />);
    expect(screen.queryByRole("slider", { name: /^intro text size/i })).not.toBeInTheDocument();
  });

  it("hides the animation picker in cluster mode", () => {
    render(<EditToolbar session={makeClusterSession()} styleSets={[]} fallbackSizePx={60} />);
    for (const a of INTRO_ANIMATIONS) {
      expect(screen.queryByRole("button", { name: a.label })).not.toBeInTheDocument();
    }
  });
});
