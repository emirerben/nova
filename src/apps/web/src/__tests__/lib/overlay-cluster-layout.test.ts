/**
 * PARITY test for the TS port of the server's editorial word-cluster intro
 * geometry (src/lib/variant-editor/overlay-cluster-layout.ts ↔
 * src/apps/api/app/pipeline/intro_cluster.py `_compute_styled_blocks`).
 *
 * THE GATE: prove the TS geometry reproduces the Python's per-block placement
 * EXACTLY. Strategy mirrors overlay-layout.test.ts — measurement is injected, so
 * the geometry ALGORITHM is verified independent of font-metric drift. Here the
 * injected measure returns the Python's OWN Skia measurements (wPx/hPx per
 * family+text+px), dumped from the venv. So a passing test means: given the same
 * glyph metrics, the TS port computes byte-identical role derivation, block
 * grouping, face assignment, per-role px, cluster-atomic shrink, and the
 * diagonal-cascade x/y placement (recenter + clamp). The only residual gap at
 * RUNTIME is canvas-vs-Skia metric drift (~1%, documented + accepted, same as
 * the linear path) — not testable in jsdom (no real font metrics).
 *
 * Fixtures: src/__tests__/lib/fixtures/cluster-layout-fixtures.json — regenerate
 * with the venv when intro_cluster.py's geometry changes:
 *
 *   cd src/apps/api && PYTHONPATH=$PWD \
 *     /Users/emirerben/Projects/nova/src/apps/api/.venv-test/bin/python \
 *     scripts/dump_cluster_fixtures.py   # (or the inline generator in the PR notes)
 *
 * The fixtures pin EDITORIAL_STYLE (the path generative/plan cluster variants
 * render — `cluster_style=EDITORIAL_STYLE`), word_roles=None (the frontend never
 * receives roles; an edit drops them so the render re-derives heuristically),
 * base_size_px=60, reveal_window_s=3.0, accent_parity=0.
 */

import fixtures from "@/__tests__/lib/fixtures/cluster-layout-fixtures.json";
import {
  computeClusterBlocks,
  deriveWordRoles,
  groupStyledBlocks,
  normalizeTypography,
  type ClusterRole,
  type MeasureCluster,
} from "@/lib/variant-editor/overlay-cluster-layout";

interface PyBlock {
  text: string;
  role: string;
  text_size_px: number;
  font_family: string;
  position_x_frac: number;
  position_y_frac: number;
  start_offset_s: number;
  reveal_s: number;
}
interface PyCase {
  hook: string;
  base_size_px: number;
  reveal_window_s: number;
  blocks: PyBlock[] | null;
}
type Measure = { wPx: number; hPx: number };

const cases = fixtures.cases as PyCase[];
const table = fixtures.measure_table as Record<string, Measure>;

// Injected measure: look up the Python's exact Skia measurement. A miss means
// the TS port requested a (family, text, px) the Python ladder never did — i.e.
// the algorithm diverged — so we throw loudly rather than silently mis-measure.
const measure: MeasureCluster = (family, text, px) => {
  const key = `${family}|||${text}|||${px}`;
  const m = table[key];
  if (!m) {
    throw new Error(`measure miss (TS diverged from Python ladder): ${key}`);
  }
  return m;
};

describe("computeClusterBlocks — Python parity (EDITORIAL_STYLE)", () => {
  for (const c of cases) {
    it(`matches Python for: "${c.hook}"`, () => {
      const got = computeClusterBlocks(c.hook, measure, {
        baseSizePx: c.base_size_px,
        revealWindowS: c.reveal_window_s,
        accentParity: 0,
      });

      if (c.blocks === null) {
        expect(got).toBeNull();
        return;
      }

      expect(got).not.toBeNull();
      expect(got!.length).toBe(c.blocks.length);

      got!.forEach((b, i) => {
        const py = c.blocks![i];
        // Exact: text, role, face, and size are pure arithmetic / string ops.
        expect(b.text).toBe(py.text);
        expect(b.role).toBe(py.role);
        expect(b.family).toBe(py.font_family);
        expect(b.textSizePx).toBe(py.text_size_px);
        // Positions are 6-decimal rounded on both sides; allow ≤2px (~0.001 frac
        // on the 1080/1920 canvas) for float-order differences in the cascade
        // sum. In practice these match to the rounded digit.
        expect(Math.abs(b.positionXFrac - py.position_x_frac)).toBeLessThanOrEqual(0.001);
        expect(Math.abs(b.positionYFrac - py.position_y_frac)).toBeLessThanOrEqual(0.001);
        expect(b.startOffsetS).toBeCloseTo(py.start_offset_s, 3);
        expect(b.revealS).toBeCloseTo(py.reveal_s, 3);
      });
    });
  }
});

describe("deriveWordRoles — heuristic role parity", () => {
  it("stopwords → connector, content words → hero, terminal-punct final → closer", () => {
    expect(deriveWordRoles(["what", "is", "your", "favorite", "place"])).toEqual([
      "connector",
      "connector",
      "connector",
      "hero",
      "closer", // 4+ words, no closer signal, final hero demoted (contrast guarantee)
    ]);
  });

  it("promotes the longest non-closer word when no hero survives", () => {
    // all stopwords + terminal punct on last → final is closer, longest promoted.
    const roles = deriveWordRoles(["the", "is", "to", "of?"]);
    expect(roles).toContain("hero");
  });

  it("terminal punctuation on the last word makes it a closer", () => {
    expect(deriveWordRoles(["where", "did", "summer", "go?"])[3]).toBe("closer");
  });
});

describe("groupStyledBlocks — one emphasis group + adjacent merge", () => {
  it("merges adjacent same-role words and keeps ONE hero run", () => {
    const words = ["the", "days", "we", "lost", "found", "us"];
    const roles: ClusterRole[] = [
      "connector",
      "hero",
      "connector",
      "hero",
      "hero",
      "closer",
    ];
    const blocks = groupStyledBlocks(words, roles);
    // first hero run stays hero; later heroes demote to closer and merge.
    const heroCount = blocks.filter((b) => b.role === "hero").length;
    expect(heroCount).toBe(1);
    expect(blocks.map((b) => b.text).join("|")).toBe("the|days|we lost found us");
  });

  it("promotes the longest block when the annotation has no hero", () => {
    // adjacent same-role words merge FIRST, so three connectors collapse to one
    // block which is then promoted (matches Python _group_styled_blocks).
    const blocks = groupStyledBlocks(
      ["a", "bb", "cccc"],
      ["connector", "connector", "connector"],
    );
    expect(blocks.length).toBe(1);
    expect(blocks[0].role).toBe("hero");
    expect(blocks[0].text).toBe("a bb cccc");
  });

  it("promotes the longest of multiple non-hero blocks (no merge across roles)", () => {
    // closer | connector | closer — three separate blocks, longest promoted.
    const blocks = groupStyledBlocks(
      ["x", "and", "yyyyyy"],
      ["closer", "connector", "closer"],
    );
    expect(blocks.length).toBe(3);
    expect(blocks.find((b) => b.role === "hero")!.text).toBe("yyyyyy");
  });

  it("caps at maxBlocks=3 by folding tail non-hero neighbors", () => {
    const words = ["a", "BIG", "b", "c", "d", "e"];
    const roles: ClusterRole[] = [
      "connector",
      "hero",
      "connector",
      "connector",
      "connector",
      "connector",
    ];
    const blocks = groupStyledBlocks(words, roles);
    expect(blocks.length).toBeLessThanOrEqual(3);
  });
});

describe("normalizeTypography — typewriter → typographic", () => {
  it("apostrophe, ellipsis, and context-resolved double quotes", () => {
    expect(normalizeTypography("it's")).toBe("it’s");
    expect(normalizeTypography("wait...")).toBe("wait…");
    expect(normalizeTypography('"hi"')).toBe("“hi”");
    expect(normalizeTypography('say "go"')).toBe("say “go”");
  });
});
