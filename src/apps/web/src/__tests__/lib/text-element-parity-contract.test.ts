/**
 * Layout-contract tests for parity-gated TextElement style fields (D9/D17) —
 * the TS half of the shared-fixture contract that feeds PARITY_VERIFIED_FIELDS.
 *
 * Every fixture under tests/fixtures/text-element-parity/ (repo root) is
 * asserted by BOTH this suite (resolveTextElementsLayout + the pure resolvers
 * in overlay-layout.ts) and the pytest suite
 * src/apps/api/tests/pipeline/test_text_element_parity_contract.py (burn-dict
 * output of build_overlays_from_text_elements) — same JSON, same expected
 * values, so the CSS preview and the Skia reburn cannot drift silently.
 */

import fs from "fs";
import path from "path";
import {
  applyTextCase,
  blockMetrics,
  CANVAS_W,
  greedyWrapLines,
  resolveLetterSpacingPx,
  resolveLineSpacing,
  resolveMaxWidthFrac,
  resolveTextElementsLayout,
} from "@/lib/overlay-layout";
import { PARITY_VERIFIED_FIELDS } from "@/lib/parity-verified-fields";
import type { TextElement } from "@/lib/plan-api";
import {
  smoothTypeStateAt,
  smoothTypeLineProgresses,
  textMotionDurationS,
  textMotionGraphemeCount,
  textMotionSettleS,
} from "@/lib/text-motion-v2";

// repo_root/tests/fixtures/text-element-parity — shared with the pytest suite.
const FIXTURES_DIR = path.resolve(
  __dirname,
  "../../../../../../tests/fixtures/text-element-parity",
);

/** Fields whose gate is THIS suite (base fields predate the D17 mechanism).
 * Must mirror GATED_STYLE_FIELDS in test_text_element_parity_contract.py. */
const GATED_STYLE_FIELDS = ["text_case", "letter_spacing", "line_spacing", "max_width_frac", "motion"];

interface FixtureCase {
  name: string;
  element: Record<string, unknown>;
  geometry?: Record<string, unknown>;
  expected: Record<string, unknown>;
}

function loadFixture(field: string): { cases: FixtureCase[] } {
  const p = path.join(FIXTURES_DIR, `${field}.json`);
  return JSON.parse(fs.readFileSync(p, "utf-8"));
}

function layoutOne(element: Record<string, unknown>) {
  const layouts = resolveTextElementsLayout([element as unknown as TextElement]);
  expect(layouts).toHaveLength(1);
  return layouts[0];
}

describe("parity registry / fixture coverage", () => {
  it("has the shared fixtures directory", () => {
    expect(fs.existsSync(FIXTURES_DIR)).toBe(true);
  });

  it("every gated field has a fixture AND is in PARITY_VERIFIED_FIELDS", () => {
    const fixtureFields = fs
      .readdirSync(FIXTURES_DIR)
      .filter((f) => f.endsWith(".json"))
      .map((f) => f.replace(/\.json$/, ""));
    for (const field of GATED_STYLE_FIELDS) {
      expect(fixtureFields).toContain(field);
      expect(PARITY_VERIFIED_FIELDS).toContain(field);
    }
    // No orphan fixtures: a fixture without a contract test here would look
    // verified without being tested.
    for (const f of fixtureFields) expect(GATED_STYLE_FIELDS).toContain(f);
  });
});

describe("text_case contract (fixture: text_case.json)", () => {
  const { cases } = loadFixture("text_case");

  it.each(cases.map((c) => [c.name, c] as const))(
    "layout text matches the burn dict: %s",
    (_name: string, c: FixtureCase) => {
      // Same string the Python compiler writes into the burn dict.
      expect(layoutOne(c.element).text).toBe(c.expected.text);
    },
  );

  it.each(cases.map((c) => [c.name, c] as const))(
    "applyTextCase mirrors apply_text_case: %s",
    (_name: string, c: FixtureCase) => {
      expect(
        applyTextCase(c.element.text as string, c.element.text_case as string | undefined),
      ).toBe(c.expected.text);
    },
  );

  it("coerces unknown case values to passthrough (mirrors the schema coercion)", () => {
    expect(applyTextCase("AbC", "sTuDlY")).toBe("AbC");
  });
});

describe("letter_spacing contract (fixture: letter_spacing.json)", () => {
  const { cases } = loadFixture("letter_spacing");

  it.each(cases.map((c) => [c.name, c] as const))(
    "layout em value matches the burn dict: %s",
    (_name: string, c: FixtureCase) => {
      expect(layoutOne(c.element).letterSpacingEm).toBeCloseTo(
        c.expected.letter_spacing_em as number,
        9,
      );
    },
  );

  it.each(cases.map((c) => [c.name, c] as const))(
    "resolveLetterSpacingPx mirrors resolve_letter_spacing_px: %s",
    (_name: string, c: FixtureCase) => {
      expect(
        resolveLetterSpacingPx(
          c.element.letter_spacing as number | null | undefined,
          c.element.size_px as number,
        ),
      ).toBeCloseTo(c.expected.letter_spacing_px as number, 9);
    },
  );
});

describe("line_spacing contract (fixture: line_spacing.json)", () => {
  const { cases } = loadFixture("line_spacing");

  it.each(cases.map((c) => [c.name, c] as const))(
    "layout multiplier matches the burn dict: %s",
    (_name: string, c: FixtureCase) => {
      expect(layoutOne(c.element).lineSpacing).toBeCloseTo(
        c.expected.line_spacing as number,
        9,
      );
    },
  );

  it.each(cases.map((c) => [c.name, c] as const))(
    "resolveLineSpacing + blockMetrics mirror Python geometry: %s",
    (_name: string, c: FixtureCase) => {
      const lineSpacing = resolveLineSpacing(c.element.line_spacing as number | null | undefined);
      expect(lineSpacing).toBeCloseTo(c.expected.line_spacing as number, 9);
      const { lineStep, blockH } = blockMetrics(
        c.geometry?.line_count as number,
        c.geometry?.line_height_px as number,
        lineSpacing,
      );
      expect(lineStep).toBe(c.expected.line_step as number);
      expect(blockH).toBe(c.expected.block_h as number);
    },
  );
});

describe("max_width_frac contract (fixture: max_width_frac.json)", () => {
  const { cases } = loadFixture("max_width_frac");

  it.each(cases.map((c) => [c.name, c] as const))(
    "layout width matches the burn dict: %s",
    (_name: string, c: FixtureCase) => {
      const layout = layoutOne(c.element);
      expect(layout.maxWidthFrac).toBeCloseTo(c.expected.max_width_frac as number, 9);
      expect(layout.maxWidthPx).toBeCloseTo(c.expected.max_width_px as number, 9);
    },
  );

  it.each(cases.map((c) => [c.name, c] as const))(
    "resolveMaxWidthFrac + greedyWrapLines mirror Python geometry: %s",
    (_name: string, c: FixtureCase) => {
      const maxWidthFrac = resolveMaxWidthFrac(
        c.element.max_width_frac as number | null | undefined,
      );
      const maxWidthPx = CANVAS_W * maxWidthFrac;
      const charWidth = c.geometry?.char_width_px as number;
      const lines = greedyWrapLines(c.element.text as string, (text) => text.length * charWidth, maxWidthPx);

      expect(maxWidthFrac).toBeCloseTo(c.expected.max_width_frac as number, 9);
      expect(maxWidthPx).toBeCloseTo(c.expected.max_width_px as number, 9);
      expect(lines).toHaveLength(c.expected.line_count as number);
      expect(Math.max(...lines.map((line) => line.length * charWidth))).toBeLessThanOrEqual(
        maxWidthPx,
      );
    },
  );
});

describe("motion contract (fixture: motion.json)", () => {
  const { cases } = loadFixture("motion");

  it.each(cases.map((c) => [c.name, c] as const))(
    "matches shared timing and frame states: %s",
    (_name: string, c: FixtureCase) => {
      const element = c.element as unknown as TextElement;
      const expected = c.expected as {
        grapheme_count: number;
        settle_s: number;
        total_s: number;
        samples: Array<Record<string, unknown>>;
      };
      expect(textMotionGraphemeCount(element.text)).toBe(expected.grapheme_count);
      expect(textMotionSettleS(element.effect!, element.text, element.motion)).toBeCloseTo(expected.settle_s, 9);
      expect(textMotionDurationS(element.effect!, element.text, element.motion)).toBeCloseTo(expected.total_s, 9);
      for (const sample of expected.samples) {
        const state = smoothTypeStateAt(element.text, sample.t as number, element.motion);
        expect(state.alpha).toBeCloseTo(sample.alpha as number, 9);
        expect(state.xTranslate).toBeCloseTo(sample.x_translate as number, 9);
        expect(state.yTranslate).toBeCloseTo(sample.y_translate as number, 9);
        expect(state.blurPx).toBeCloseTo(sample.blur_px as number, 9);
        expect(state.revealProgress).toBeCloseTo(sample.reveal_progress as number, 9);
        expect(state.revealOrigin).toBe(sample.reveal_origin);
        expect(state.settled).toBe(sample.settled);
      }
      const lineSamples = (c.expected.line_samples ?? []) as Array<{
        t: number;
        progresses: number[];
      }>;
      for (const sample of lineSamples) {
        const progresses = smoothTypeLineProgresses(
          element.text.split("\n"),
          sample.t,
          element.motion,
        );
        expect(progresses).toHaveLength(sample.progresses.length);
        progresses.forEach((progress, index) => {
          expect(progress).toBeCloseTo(sample.progresses[index], 9);
        });
      }
    },
  );
});
