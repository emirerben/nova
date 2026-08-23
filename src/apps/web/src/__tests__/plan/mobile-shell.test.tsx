/**
 * Mobile baseline guard (plans/013, DESIGN.md §8).
 *
 * The split-rail setup shells shipped a fixed 224px (`w-56`) rail with no
 * breakpoint. On a 390px viewport that left 70px for content, and because flex
 * items default to `min-width: auto` the pane could not shrink into it — the
 * row overflowed the viewport instead of reflowing. The same defect existed
 * twice (onboarding + the record takeover) because the rail was copy-pasted.
 *
 * These assertions pin the properties that keep it fixed:
 *   1. StepRail emits a phone strip AND a `md:`-gated desktop rail.
 *   2. Neither shell's <main> can overflow (`min-w-0`) and both stack below md.
 *   3. The editor's loading skeleton keeps its docked columns breakpoint-gated.
 *   4. The record wrapper uses dvh, not vh.
 *   5. No form control renders below the 16px iOS zoom-on-focus floor.
 *
 * Source-text assertions are used where the defect IS a class string, and
 * rendering the page-level shells would need a dozen network/child mocks to
 * prove a layout fact the class list states outright.
 */

import fs from "fs";
import path from "path";
import React from "react";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import {
  StepRail,
  stepState,
  type StepRailStep,
} from "@/app/plan/_components/ui/StepRail";
import { resolveLayoutMode } from "@/app/plan/items/[id]/_editor/useEditorLayoutMode";

const WEB_SRC = path.join(__dirname, "..", "..");
const ONBOARDING = "app/plan/_components/OnboardingShell.tsx";
const TRANSCRIPT = "app/plan/items/[id]/transcript/page.tsx";
const EDITOR_SHELL = "app/plan/items/[id]/_editor/EditorShell.tsx";

function readSrc(rel: string): string {
  return fs.readFileSync(path.join(WEB_SRC, rel), "utf8");
}

const STEPS: StepRailStep<number>[] = [
  { key: 1, label: "TikTok", state: "done", clickable: true, note: { text: "✓", tone: "lime" } },
  { key: 2, label: "What you make", state: "active" },
  { key: 3, label: "Style", state: "upcoming" },
  { key: 4, label: "Content plan", state: "upcoming" },
];

describe("StepRail — responsive presentations", () => {
  it("renders a phone strip that is hidden from md up", () => {
    render(<StepRail steps={STEPS} onGoBack={() => {}} />);
    const strip = screen
      .getAllByLabelText("Progress")
      .find((n) => n.tagName === "NAV");
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

  it("names the active step on the phone strip", () => {
    render(<StepRail steps={STEPS} onGoBack={() => {}} />);
    expect(screen.getAllByText("What you make").length).toBeGreaterThan(0);
  });

  it("only lets revisitable steps be tapped, and gives every dot a 44px box", () => {
    const onGoBack = jest.fn();
    const { container } = render(<StepRail steps={STEPS} onGoBack={onGoBack} />);
    const strip = container.querySelector("nav")!;

    // "Back to TikTok" is the strip's accessible name for the one done step.
    const back = screen.getByRole("button", { name: "Back to TikTok" });
    expect(back).toHaveClass("h-11", "w-11"); // DESIGN.md §8: >=44px touch target

    // Uniform box on EVERY dot, interactive or not, so the track spaces evenly.
    const boxes = strip.querySelectorAll("ol > li > *");
    expect(boxes).toHaveLength(STEPS.length);
    boxes.forEach((box) => expect(box).toHaveClass("h-11", "w-11"));

    back.click();
    expect(onGoBack).toHaveBeenCalledWith(1);

    // Upcoming steps expose no button in the strip at all.
    expect(
      screen.queryByRole("button", { name: "Back to Style" }),
    ).not.toBeInTheDocument();
  });

  it("distinguishes a skipped step from one not yet reached, for screen readers", () => {
    const withSkip: StepRailStep<number>[] = [
      { key: 1, label: "TikTok", state: "skipped" },
      { key: 2, label: "What you make", state: "active" },
      { key: 3, label: "Style", state: "upcoming" },
    ];
    const { container } = render(<StepRail steps={withSkip} onGoBack={() => {}} />);
    const strip = container.querySelector("nav")!;
    // `note` is desktop-only, so without this the mobile strip would render a
    // skipped step identically to an untouched one.
    expect(strip.textContent).toContain("TikTok (skipped)");
  });

  it("gives interactive dots a visible focus ring (DESIGN.md §8)", () => {
    render(<StepRail steps={STEPS} onGoBack={() => {}} />);
    expect(screen.getByRole("button", { name: "Back to TikTok" }).className).toMatch(
      /focus-visible:outline/,
    );
  });

  it("derives done/active/upcoming from position", () => {
    expect(stepState(1, 2)).toBe("done");
    expect(stepState(2, 2)).toBe("active");
    expect(stepState(3, 2)).toBe("upcoming");
  });
});

describe("split-rail shells cannot overflow a phone viewport", () => {
  const SHELLS: Array<{ rel: string; label: string }> = [
    { rel: ONBOARDING, label: "onboarding" },
    { rel: TRANSCRIPT, label: "record takeover" },
  ];

  it.each(SHELLS)("$label stacks below md and lets <main> shrink", ({ rel }) => {
    const src = readSrc(rel);

    // Outer container stacks on phones instead of forcing a side-by-side row.
    // Asserted independently of the height utility (min-h-screen vs the dvh
    // form) — the stacking is what keeps the rail from eating 57% of the width.
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

  it("keeps the onboarding footage cards single-column on phones", () => {
    // Two columns at 390px wrapped "Talking to camera" onto three lines.
    expect(readSrc(ONBOARDING)).toMatch(/grid-cols-1[^"]*sm:grid-cols-2/);
  });

  it("uses dvh for the record wrapper so the record button stays on screen", () => {
    // 100vh ignores the mobile browser toolbar, pushing the 44px record
    // targets off-screen. This is the highest-severity functional bug after
    // the rail itself, and it regresses invisibly in devtools emulation.
    const src = readSrc(TRANSCRIPT);
    expect(src).toContain("h-[calc(100dvh-8rem)]");
    expect(src).not.toMatch(/h-\[calc\(100vh-8rem\)\]/);
  });
});

describe("editor light mode still owns the sub-1024px range", () => {
  it("resolves to light on a phone", () => {
    expect(resolveLayoutMode(false, false)).toBe("light");
    expect(resolveLayoutMode(false, true)).toBe("overlay");
    expect(resolveLayoutMode(true, true)).toBe("full");
  });

  it("keeps the loading skeleton's docked columns breakpoint-gated", () => {
    const src = readSrc(EDITOR_SHELL);
    const loading = src.slice(src.indexOf("if (loading) {"));
    // Strip comments: this asserts on what the skeleton RENDERS, and prose
    // explaining the old layout legitimately names the classes it replaced.
    const body = loading
      .slice(0, loading.indexOf("if (loadError"))
      .replace(/\{?\/\*[\s\S]*?\*\/\}?/g, "")
      .replace(/^\s*\/\/.*$/gm, "");

    // The 484px column set must never be reachable at phone width. Asserting
    // on the gate (not on which side of a ternary won) means swapping two JSX
    // bodies can't leave this green — there is no branch to swap.
    expect(body).not.toContain("grid-cols-[92px_1fr_320px_72px]");
    for (const col of ["w-[92px]", "w-[320px]", "w-[72px]"]) {
      const rail = body.match(new RegExp(`className="([^"]*${col.replace(/[[\]]/g, "\\$&")}[^"]*)"`));
      expect(rail).not.toBeNull();
      expect(rail?.[1]).toContain("hidden");
      expect(rail?.[1]).toContain("xl:block");
    }

    // And the skeleton must not depend on layoutMode: the hook's server
    // snapshot is "full", so a JS branch flashes the docked columns on the
    // hydration render — the exact overflow this fixes.
    expect(body).not.toContain("layoutMode");
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
    const offenders = new Set<string>();

    /**
     * Return the element's opening tag only, ending at the first `>` that sits
     * outside a JSX expression or string. Needed because `<select>` and
     * `<textarea>` are not self-closing: stopping at the first `/>` swallowed
     * the NEXT element's classes and produced false positives, while stopping
     * at the first bare `>` would trip over arrow functions in handlers.
     */
    const openingTag = (src: string, from: number): string => {
      let quote: string | null = null;
      let depth = 0;
      for (let i = from + 1; i < src.length; i++) {
        const ch = src[i];
        if (quote) {
          if (ch === "\\") i++;
          else if (ch === quote) quote = null;
          continue;
        }
        if (ch === '"' || ch === "'" || ch === "`") quote = ch;
        else if (ch === "{") depth++;
        else if (ch === "}") depth--;
        else if (ch === ">" && depth === 0) return src.slice(from, i + 1);
      }
      return src.slice(from, from + 1400);
    };

    /**
     * Pull every string literal out of the opening tag, whatever shape the
     * className takes. Matching `className="..."` alone missed
     * `className={cn("text-sm", …)}` — the dominant helper in this codebase —
     * so the guard skipped exactly the regressions it exists to catch.
     */
    const sizeTokens = (tag: string): string[] => {
      const tokens: string[] = [];
      for (const lit of Array.from(tag.matchAll(/"([^"]*)"|'([^']*)'|`([^`]*)`/g))) {
        const value = lit[1] ?? lit[2] ?? lit[3] ?? "";
        for (const token of value.split(/\s+/)) {
          // Skip breakpoint/state-prefixed variants (md:text-sm applies only
          // at >=768px, well outside the max-width:639px floor).
          if (token.includes(":")) continue;
          if (/^text-(?:xs|sm)$/.test(token) || /^text-\[\d+px\]$/.test(token)) {
            tokens.push(token);
          }
        }
      }
      return tokens;
    };

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
        // Also match the shadcn/Kria <Input>/<Textarea> primitives (Lane 0) —
        // they carry the same iOS zoom-on-focus risk as raw <input>/<textarea>.
        for (const m of Array.from(src.matchAll(/<(input|textarea|select|Input|Textarea)[\s>]/g))) {
          const from = m.index!;
          for (const token of sizeTokens(openingTag(src, from))) {
            const arb = token.match(/^text-\[(\d+)px\]$/);
            const px = arb ? Number(arb[1]) : token === "text-xs" ? 12 : 14;
            if (px >= 16) continue;
            if (SMALL_CLASSES.includes(token)) continue;
            const line = src.slice(0, from).split("\n").length;
            offenders.add(`${path.relative(WEB_SRC, full)}:${line} — ${token}`);
          }
        }
      }
    };
    walk(path.join(WEB_SRC, "app"));
    walk(path.join(WEB_SRC, "components"));

    // A new sub-16px control means adding its class to the globals.css :is()
    // list (and to SMALL_CLASSES above), not deleting this assertion.
    expect(Array.from(offenders)).toEqual([]);
  });
});
