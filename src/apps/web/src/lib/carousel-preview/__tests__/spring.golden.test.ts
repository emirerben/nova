/**
 * Golden-trace parity test for the spring physics port. Loads the SAME
 * fixture the Python suite pins against
 * (src/apps/api/tests/pipeline/carousel/golden_spring_trace.json,
 * generated once from the real vendored-bundle-derived Python engine — see
 * spring.py's module docstring) directly across the package boundary,
 * mirroring the path-resolution style
 * src/apps/web/src/__tests__/lib/text-element-parity-contract.test.ts uses
 * to read src/apps/api/tests/pipeline/ fixtures.
 *
 * Companion Python pin: test_spring_golden_trace.py::
 * test_canonical_flick_matches_golden_trace.
 */

import fs from "fs";
import path from "path";
import { CANONICAL_FLICK } from "../gesture";
import { simulate } from "../spring";

const GOLDEN_PATH = path.resolve(
  __dirname,
  "../../../../../../../src/apps/api/tests/pipeline/carousel/golden_spring_trace.json",
);

interface GoldenFrame {
  t_s: number;
  virtual_scroll: number;
  velocity: number;
  target: number;
}

const SNAP_POSITIONS = [0, 1, 2, 3, 4].map((i) => i * 588);
const SNAPPORT_WIDTH = 1080.0;
const EPS = 1e-6;

describe("spring golden trace parity (fixture: golden_spring_trace.json)", () => {
  it("has the shared golden fixture", () => {
    expect(fs.existsSync(GOLDEN_PATH)).toBe(true);
  });

  const golden: GoldenFrame[] = JSON.parse(fs.readFileSync(GOLDEN_PATH, "utf-8"));
  const frames = simulate(CANONICAL_FLICK, SNAP_POSITIONS, SNAPPORT_WIDTH);

  it("produces the exact same frame count as the Python golden trace", () => {
    expect(frames.length).toBe(golden.length);
  });

  it.each(golden.map((g, i) => [i, g] as const))(
    "frame %i matches within 1e-6 abs diff on every field",
    (i, expected) => {
      const frame = frames[i];
      expect(frame.tS).toBeCloseTo(expected.t_s, 6);
      expect(Math.abs(frame.virtualScroll - expected.virtual_scroll)).toBeLessThanOrEqual(EPS);
      expect(Math.abs(frame.velocity - expected.velocity)).toBeLessThanOrEqual(EPS);
      expect(Math.abs(frame.target - expected.target)).toBeLessThanOrEqual(EPS);
    },
  );

  it("reports the max abs diff observed across the whole trace (informational)", () => {
    let maxDiff = 0;
    for (let i = 0; i < golden.length; i += 1) {
      const frame = frames[i];
      const expected = golden[i];
      maxDiff = Math.max(
        maxDiff,
        Math.abs(frame.virtualScroll - expected.virtual_scroll),
        Math.abs(frame.velocity - expected.velocity),
        Math.abs(frame.target - expected.target),
      );
    }
    // eslint-disable-next-line no-console
    console.log(`spring golden trace max abs diff: ${maxDiff}`);
    expect(maxDiff).toBeLessThanOrEqual(EPS);
  });
});
