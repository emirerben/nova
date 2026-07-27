/**
 * Mobile baseline guard (plans/013, DESIGN.md §8).
 *
 * The split-rail setup shells shipped a fixed 224px (`w-56`) rail with no
 * breakpoint. On a 390px viewport that left 70px for content, and because flex
 * items default to `min-width: auto` the pane could not shrink into it — the
 * row overflowed the viewport instead of reflowing. The same defect existed
 * twice (onboarding + the record takeover) because the rail was copy-pasted.
 *
 * These assertions pin the four properties that keep it fixed:
 *   1. StepRail emits a phone strip AND a `md:`-gated desktop rail.
 *   2. Neither shell's <main> can overflow (`min-w-0`) and both stack below md.
 *   3. The editor's light mode is still what resolves below 1024px.
 *   4. No form control renders below the 16px iOS zoom-on-focus floor.
 *
 * (2) and (4) are asserted against source text on purpose: the defect *is* a
 * class string, and rendering the two page-level shells would need a dozen
 * network/child mocks to prove a layout fact that the class list states outright.
 */

import fs from "fs";
import path from "path";
import React from "react";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import { StepRail, type StepRailStep } from "@/app/plan/_components/ui/StepRail";
import { resolveLayoutMode } from "@/app/plan/items/[id]/_editor/useEditorLayoutMode";

const WEB_SRC = path.join(__dirname, "..", "..");

function readSrc(rel: string): string {
  return fs.readFileSync(path.join(WEB_SRC, rel), "utf8");
}

const STEPS: StepRailStep[] = [
  { key: 1, label: "TikTok", state: "done", clickable: true, note: { text: "✓", tone: "lime" } },
  { key: 2, label: "What you make", state: "active" },
  { key: 3, label: "Style", state: "upcoming" },
  { key: 4, label: "First plan", state: "upcoming" },
];

describe("StepRail — responsive presentations", () => {
  it("renders a phone strip that is hidden from md up", () => {
    render(<StepRail steps={STEPS} onGoBack={() => {}} />);
    const navs = screen.getAllByLabelText("Progress");
    const strip = navs.find((n) => n.tagName === "NAV");
    expect(strip).toBeDefined();
    expect(strip).toHaveClass("md:hidden");
    // The strip must not reserve horizontal width the way the rail did.
    expect(strip?.className).not.toMatch(/\bw-56\b/);
  });

  it("keeps the 224px rail behind the md breakpoint", () => {
    const { container } = render(<StepRail steps={STEPS} onGoBack={() => {}} />);
    const aside = container.querySelector("aside");
    expect(aside).not.toBeNull();
    // w-56 is still the desktop width — but only from md up, and hidden below it.
    expect(aside).toHaveClass("w-56", "hidden", "md:flex");
  });

  it("surfaces the active step's label and position on the phone strip", () => {
    render(<StepRail steps={STEPS} onGoBack={() => {}} />);
    // Active label appears twice (strip + desktop rail); the counter is strip-only.
    expect(screen.getAllByText("What you make").length).toBeGreaterThan(0);
    expect(screen.getByText("2 of 4")).toBeInTheDocument();
  });

  it("only lets revisitable steps be tapped, and gives them a 44px target", () => {
    const onGoBack = jest.fn();
    render(<StepRail steps={STEPS} onGoBack={onGoBack} />);

    // "Back to TikTok" is the strip's accessible name for the one done step.
    const back = screen.getByRole("button", { name: "Back to TikTok" });
    expect(back).toHaveClass("h-11", "w-11"); // DESIGN.md §8: >=44px touch target
    back.click();
    expect(onGoBack).toHaveBeenCalledWith(1);

    // Upcoming steps expose no button in the strip at all.
    expect(
      screen.queryByRole("button", { name: "Back to Style" }),
    ).not.toBeInTheDocument();
  });
});

describe("split-rail shells cannot overflow a phone viewport", () => {
  const SHELLS: Array<{ rel: string; label: string }> = [
    { rel: "app/plan/_components/OnboardingShell.tsx", label: "onboarding" },
    { rel: "app/plan/items/[id]/transcript/page.tsx", label: "record takeover" },
  ];

  it.each(SHELLS)("$label stacks below md and lets <main> shrink", ({ rel }) => {
    const src = readSrc(rel);

    // Outer container stacks on phones instead of forcing a side-by-side row.
    // Asserted independently of the height utility (min-h-screen vs the dvh
    // form) — the stacking is the property that keeps the rail from eating
    // 57% of the viewport width.
    const outer = src.match(/<div className="(flex min-h-[^"]*)"/);
    expect(outer).not.toBeNull();
    expect(outer?.[1]).toContain("flex-col");
    expect(outer?.[1]).toContain("md:flex-row");

    // <main> carries min-w-0, or a wide child's min-content re-inflates the row.
    const main = src.match(/<main\s+className="([^"]*)"/);
    expect(main).not.toBeNull();
    expect(main?.[1]).toContain("min-w-0");

    // Neither shell may reintroduce its own ungated rail.
    expect(src).not.toMatch(/<aside className="flex w-56/);
  });

  it("no longer duplicates the rail — both shells use the shared component", () => {
    for (const { rel } of SHELLS) {
      expect(readSrc(rel)).toMatch(/from "[^"]*ui\/StepRail"/);
    }
  });
});

describe("editor light mode still owns the sub-1024px range", () => {
  it("resolves to light on a phone", () => {
    expect(resolveLayoutMode(false, false)).toBe("light");
    expect(resolveLayoutMode(false, true)).toBe("overlay");
    expect(resolveLayoutMode(true, true)).toBe("full");
  });

  it("does not paint the 484px docked skeleton in light mode", () => {
    const src = readSrc("app/plan/items/[id]/_editor/EditorShell.tsx");
    // The loading branch must choose a skeleton by layoutMode.
    expect(src).toMatch(/if \(loading\) \{[\s\S]{0,600}?layoutMode === "light" \?/);
  });
});

describe("iOS zoom-on-focus floor (DESIGN.md §8)", () => {
  const SMALL_CLASSES = [
    "text-xs",
    "text-sm",
    "text-[11px]",
    "text-[12px]",
    "text-[13px]",
    "text-[15px]",
  ];

  it("globals.css raises every covered sub-16px control at the base tier", () => {
    const css = readSrc("app/globals.css");
    const block = css.match(/@media \(max-width: 639px\) \{[\s\S]*?\n\}/);
    expect(block).not.toBeNull();
    const rule = block![0];
    expect(rule).toMatch(/:is\(input, textarea, select\)/);
    expect(rule).toContain("font-size: 16px");
    for (const cls of SMALL_CLASSES) {
      // Arbitrary values are escaped in CSS: text-[11px] -> text-\[11px\]
      const escaped = cls.replace("[", "\\[").replace("]", "\\]");
      expect(rule).toContain(`.${escaped}`);
    }
  });

  it("no form control uses a sub-16px size outside the covered list", () => {
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          // /admin and /dev-qa are desktop-only tools (plans/013 out of scope).
          if (entry.name === "admin" || entry.name === "dev-qa") continue;
          walk(full);
          continue;
        }
        if (!entry.name.endsWith(".tsx")) continue;
        const src = fs.readFileSync(full, "utf8");
        // Array.from, not for-of: this tsconfig target predates iterating a
        // RegExp string iterator directly (TS2802).
        for (const m of Array.from(src.matchAll(/<(input|textarea|select)\b/g))) {
          const seg = src.slice(m.index!, m.index! + 1400);
          const cls = seg.match(/className=(?:"([^"]*)"|\{`([^`]*)`\})/);
          if (!cls) continue;
          const value = cls[1] ?? cls[2] ?? "";
          const sizes = Array.from(
            value.matchAll(/text-(?:xs|sm)\b|text-\[(\d+)px\]/g),
          );
          for (const sm of sizes) {
            const px = sm[1] ? Number(sm[1]) : sm[0] === "text-xs" ? 12 : 14;
            if (px >= 16) continue;
            if (SMALL_CLASSES.includes(sm[0])) continue;
            const line = src.slice(0, m.index!).split("\n").length;
            offenders.push(`${path.relative(WEB_SRC, full)}:${line} — ${sm[0]}`);
          }
        }
      }
    };
    walk(path.join(WEB_SRC, "app"));
    walk(path.join(WEB_SRC, "components"));

    // A new sub-16px control means adding its class to the globals.css :is()
    // list (and to SMALL_CLASSES above), not deleting this assertion.
    expect(offenders).toEqual([]);
  });
});
