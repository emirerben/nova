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
import { applyTextCase, resolveTextElementsLayout } from "@/lib/overlay-layout";
import { PARITY_VERIFIED_FIELDS } from "@/lib/parity-verified-fields";
import type { TextElement } from "@/lib/plan-api";

// repo_root/tests/fixtures/text-element-parity — shared with the pytest suite.
const FIXTURES_DIR = path.resolve(
  __dirname,
  "../../../../../../tests/fixtures/text-element-parity",
);

/** Fields whose gate is THIS suite (base fields predate the D17 mechanism).
 * Must mirror GATED_STYLE_FIELDS in test_text_element_parity_contract.py. */
const GATED_STYLE_FIELDS = ["text_case"];

interface FixtureCase {
  name: string;
  element: Record<string, unknown>;
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
    (_name, c) => {
      // Same string the Python compiler writes into the burn dict.
      expect(layoutOne(c.element).text).toBe(c.expected.text);
    },
  );

  it.each(cases.map((c) => [c.name, c] as const))(
    "applyTextCase mirrors apply_text_case: %s",
    (_name, c) => {
      expect(
        applyTextCase(c.element.text as string, c.element.text_case as string | undefined),
      ).toBe(c.expected.text);
    },
  );

  it("coerces unknown case values to passthrough (mirrors the schema coercion)", () => {
    expect(applyTextCase("AbC", "sTuDlY")).toBe("AbC");
  });
});
