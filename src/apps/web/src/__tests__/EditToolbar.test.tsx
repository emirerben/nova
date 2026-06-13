/**
 * EditToolbar (shared, src/components/variant-editor/EditToolbar.tsx):
 * - one StyleChip per style set, inside a role="radiogroup"
 * - chips render the user's CURRENT draft text as the sample (dark tile)
 * - clicking a chip calls session.setStyle (0 network)
 * - empty styleSets hides the style row entirely
 * - arrow keys move selection across chips (W7)
 */

import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import { EditToolbar } from "@/components/variant-editor/EditToolbar";
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
    playToken: 0,
    replay: jest.fn(),
    commit: jest.fn(async () => {}),
    ...over,
  };
}

const STYLE_SETS: GenerativeStyleSet[] = [
  { id: "a", label: "Editorial", tags: [], intro: { css_family: "'Playfair Display', serif", text_color: "#FFF" } },
  { id: "b", label: "Bold", tags: [], intro: { css_family: "'Inter', sans-serif", text_color: "#FFF" } },
  { id: "c", label: "Script", tags: [], intro: { css_family: "'Dancing Script', cursive", text_color: "#FFF" } },
];

describe("EditToolbar style row", () => {
  it("renders one chip per style set inside a radiogroup", () => {
    render(<EditToolbar session={makeSession()} styleSets={STYLE_SETS} fallbackSizePx={56} />);
    const group = screen.getByRole("radiogroup", { name: "Text style" });
    expect(group).toBeInTheDocument();
    const chips = screen.getAllByRole("radio");
    expect(chips).toHaveLength(3);
  });

  it("samples the user's current draft text into each chip", () => {
    render(<EditToolbar session={makeSession()} styleSets={STYLE_SETS} fallbackSizePx={56} />);
    // One per chip → 3 occurrences of the (truncated) hook text.
    expect(screen.getAllByText("wish you were here")).toHaveLength(3);
  });

  it("clicking a chip calls session.setStyle with its id (no network)", () => {
    const session = makeSession();
    render(<EditToolbar session={session} styleSets={STYLE_SETS} fallbackSizePx={56} />);
    fireEvent.click(screen.getByRole("radio", { name: "Text style: Bold" }));
    expect(session.setStyle).toHaveBeenCalledWith("b");
  });

  it("marks the draft's style set as checked", () => {
    render(
      <EditToolbar
        session={makeSession({ draft: { text: "x", removed: false, styleSetId: "c", sizePx: 56, layout: null, fontFamily: null, animation: null, textColor: null } })}
        styleSets={STYLE_SETS}
        fallbackSizePx={56}
      />,
    );
    expect(screen.getByRole("radio", { name: "Text style: Script" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("radio", { name: "Text style: Editorial" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("hides the style row when there are no style sets", () => {
    render(<EditToolbar session={makeSession()} styleSets={[]} fallbackSizePx={56} />);
    expect(screen.queryByRole("radiogroup", { name: "Text style" })).not.toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  });

  it("arrow keys move the selection across chips", () => {
    const session = makeSession({
      draft: { text: "x", removed: false, styleSetId: "a", sizePx: 56, layout: null, fontFamily: null, animation: null, textColor: null },
    });
    render(<EditToolbar session={session} styleSets={STYLE_SETS} fallbackSizePx={56} />);
    const first = screen.getByRole("radio", { name: "Text style: Editorial" });
    fireEvent.keyDown(first, { key: "ArrowRight" });
    expect(session.setStyle).toHaveBeenCalledWith("b");
    // Wrap-around: ArrowLeft from the first chip lands on the last.
    fireEvent.keyDown(first, { key: "ArrowLeft" });
    expect(session.setStyle).toHaveBeenCalledWith("c");
  });

  it("falls back to the style label as the sample when text is removed", () => {
    render(
      <EditToolbar
        session={makeSession({
          draft: { text: "", removed: true, styleSetId: "a", sizePx: 56, layout: null, fontFamily: null, animation: null, textColor: null },
        })}
        styleSets={STYLE_SETS}
        fallbackSizePx={56}
      />,
    );
    // No live hook → each chip shows its own label as the sample (and sub-label).
    expect(screen.getAllByText("Editorial").length).toBeGreaterThanOrEqual(1);
  });
});
