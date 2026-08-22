/**
 * Guards the shadcn/ui semantic token block in globals.css (DESIGN.md §15).
 *
 * Source-text assertions, not rendering — the tokens are consumed by
 * Tailwind's `hsl(var(--x))` color config, which isn't observable by
 * rendering a component without a real browser paint.
 */
import fs from "fs";
import path from "path";
import { describe, expect, it } from "@jest/globals";

const CSS = fs.readFileSync(
  path.join(__dirname, "..", "..", "app", "globals.css"),
  "utf8"
);

// globals.css has a pre-existing `:root { ... }` (landing tokens) as well as
// the shadcn semantic block added inside `@layer base` — both match a naive
// `:root { ... }` regex, so pick whichever occurrence actually contains the
// semantic token we're looking for (`--background`, unique to the new block).
function block(selector: string): string {
  const re = new RegExp(
    `(?:^|\\n)\\s*${selector.replace(/[.[\]]/g, "\\$&")}\\s*\\{([\\s\\S]*?)\\n\\s*\\}`,
    "g"
  );
  const matches = Array.from(CSS.matchAll(re));
  expect(matches.length).toBeGreaterThan(0);
  const semantic = matches.find((m) => m[1].includes("--background"));
  expect(semantic).toBeDefined();
  return semantic![1];
}

const CORE_VARS = [
  "--background",
  "--foreground",
  "--card",
  "--popover",
  "--primary",
  "--secondary",
  "--muted",
  "--accent",
  "--destructive",
  "--border",
  "--input",
  "--ring",
];

describe("theme tokens (globals.css)", () => {
  it(":root defines the full shadcn semantic set", () => {
    const root = block(":root");
    for (const token of CORE_VARS) {
      expect(root).toContain(`${token}:`);
    }
    expect(root).toContain("--radius:");
  });

  it(".dark redefines the full semantic set for /template-jobs + /admin (stock shadcn zinc dark theme)", () => {
    const dark = block(".dark");
    for (const token of CORE_VARS) {
      expect(dark).toContain(`${token}:`);
    }
  });

  it(":root --ring is zinc (hue ~240), not lime — stock shadcn new-york", () => {
    const root = block(":root");
    const m = root.match(/--ring:\s*([\d.]+)/);
    expect(m).not.toBeNull();
    const hue = Number(m![1]);
    expect(hue).toBeGreaterThan(220);
    expect(hue).toBeLessThan(260);
  });

  it(".dark --ring is zinc (hue ~240), not amber — stock shadcn new-york", () => {
    const dark = block(".dark");
    const m = dark.match(/--ring:\s*([\d.]+)/);
    expect(m).not.toBeNull();
    const hue = Number(m![1]);
    expect(hue).toBeGreaterThan(220);
    expect(hue).toBeLessThan(260);
  });

  it("--destructive is defined in both themes (stock shadcn: red hue, not zinc — supersedes the pre-migration D10 'no red walls' rule for component chrome)", () => {
    for (const selector of [":root", ".dark"]) {
      const scope = block(selector);
      const m = scope.match(/--destructive:\s*([\d.]+)/);
      expect(m).not.toBeNull();
    }
  });

  it("* selector applies border-border (bare `border` now resolves to zinc-200)", () => {
    expect(CSS).toMatch(/\*\s*\{\s*\n?\s*@apply border-border;/);
  });
});
