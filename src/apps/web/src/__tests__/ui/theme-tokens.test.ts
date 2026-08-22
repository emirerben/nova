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

  it(".dark redefines the semantic set for /template-jobs + /admin (accent stays lime, unchanged)", () => {
    const dark = block(".dark");
    for (const token of CORE_VARS) {
      if (token === "--accent") continue; // intentionally NOT redeclared — inherits :root's lime value
      expect(dark).toContain(`${token}:`);
    }
    expect(dark).not.toContain("--accent:");
  });

  it(":root --ring is lime (hue ~84), not blue", () => {
    const root = block(":root");
    const m = root.match(/--ring:\s*([\d.]+)/);
    expect(m).not.toBeNull();
    const hue = Number(m![1]);
    expect(hue).toBeGreaterThan(70);
    expect(hue).toBeLessThan(100);
  });

  it(".dark --ring is amber (hue ~43)", () => {
    const dark = block(".dark");
    const m = dark.match(/--ring:\s*([\d.]+)/);
    expect(m).not.toBeNull();
    const hue = Number(m![1]);
    expect(hue).toBeGreaterThan(30);
    expect(hue).toBeLessThan(60);
  });

  it("--destructive is never a red hue in either theme — D10 'no red walls'", () => {
    for (const selector of [":root", ".dark"]) {
      const scope = block(selector);
      const m = scope.match(/--destructive:\s*([\d.]+)/);
      expect(m).not.toBeNull();
      const hue = Number(m![1]);
      // Red sits at hue ~0/360 (+/- ~20deg either side incl. orange-red).
      // Kria destructive is zinc, which is hue 240 (achromatic-leaning blue-gray).
      const isRedHue = hue <= 20 || hue >= 340;
      expect(isRedHue).toBe(false);
    }
  });

  it("* selector applies border-border (bare `border` now resolves to zinc-200)", () => {
    expect(CSS).toMatch(/\*\s*\{\s*\n?\s*@apply border-border;/);
  });
});
