import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";

import { JsonTreeView } from "@/components/JsonTreeView";

describe("JsonTreeView", () => {
  test("renders null leaf", () => {
    render(<JsonTreeView value={null} />);
    expect(screen.getByText("null")).toBeInTheDocument();
  });

  test("renders primitives", () => {
    const { rerender } = render(<JsonTreeView value={42} />);
    expect(screen.getByText("42")).toBeInTheDocument();
    rerender(<JsonTreeView value={true} />);
    expect(screen.getByText("true")).toBeInTheDocument();
  });

  test("renders empty containers", () => {
    const { rerender } = render(<JsonTreeView value={[]} />);
    expect(screen.getByText("[]")).toBeInTheDocument();
    rerender(<JsonTreeView value={{}} />);
    expect(screen.getByText("{}")).toBeInTheDocument();
  });

  test("renders nested object with collapsible nodes", () => {
    render(
      <JsonTreeView
        value={{ name: "x", inner: { a: 1, b: 2 } }}
        defaultDepth={2}
      />,
    );
    // Top-level keys visible
    expect(screen.getByText("name:")).toBeInTheDocument();
    expect(screen.getByText('"x"')).toBeInTheDocument();
    expect(screen.getByText("inner:")).toBeInTheDocument();
    // Nested keys visible at depth 2
    expect(screen.getByText("a:")).toBeInTheDocument();
    expect(screen.getByText("b:")).toBeInTheDocument();
  });

  test("collapses past defaultDepth", () => {
    render(
      <JsonTreeView
        value={{ outer: { mid: { deep: "hidden" } } }}
        defaultDepth={1}
      />,
    );
    // Only top level expanded
    expect(screen.getByText("outer:")).toBeInTheDocument();
    expect(screen.queryByText('"hidden"')).not.toBeInTheDocument();
  });

  test("long strings get a show-more affordance", () => {
    const long = "x".repeat(500);
    render(<JsonTreeView value={long} />);
    expect(screen.getByText(/show 300 more chars/)).toBeInTheDocument();
    fireEvent.click(screen.getByText(/show 300 more chars/));
    expect(screen.getByText(/collapse/)).toBeInTheDocument();
  });
});
