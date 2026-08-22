import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import { LightShell } from "../../components/ui/LightShell";

// The page body is `bg-black text-white` (src/app/layout.tsx). Light pages
// like this one only override the background, so any stock shadcn primitive
// that inherits color (Label, etc.) rendered white-on-white until
// text-foreground was added to the shell root. Guards that regression.
describe("LightShell", () => {
  it("sets both a token background and text-foreground on its root", () => {
    render(
      <LightShell>
        <p>content</p>
      </LightShell>,
    );

    const root = screen.getByText("content").parentElement?.parentElement;
    expect(root?.className).toContain("text-foreground");
    expect(root?.className).toContain("bg-background");
  });
});
