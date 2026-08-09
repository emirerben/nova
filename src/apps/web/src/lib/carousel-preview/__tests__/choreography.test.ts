/**
 * Cross-language parity test for the choreography timeline authoring port.
 *
 * Loads src/apps/api/tests/pipeline/carousel/choreography_traces.json — a
 * fixture generated ONCE by running the real Python
 * `choreography.build_timeline`/`rolling_timeline` (see
 * src/apps/api/scripts/dev/dump_choreography_fixture.py) and checked in —
 * and asserts this TS port reproduces it frame-for-frame within 1e-6,
 * including every jittered hold/pad's FRAME COUNT (which depends on
 * reproducing CPython's Mersenne Twister bit stream exactly — see
 * python-random.ts).
 *
 * Companion Python pin: test_choreography_fixture.py (asserts the Python
 * builders still match this same checked-in fixture, so it can't silently
 * drift out from under this TS test without failing on the Python side
 * too).
 */

import fs from "fs";
import path from "path";
import { buildTimeline, rollingTimeline } from "../choreography";
import type { CardGeometry, FrameState } from "../types";

const FIXTURE_PATH = path.resolve(
  __dirname,
  "../../../../../../../src/apps/api/tests/pipeline/carousel/choreography_traces.json",
);

interface GoldenFrame {
  t_s: number;
  scroll_x: number;
  focus_card: number | null;
  focus_t: number;
  dim: number;
}

interface Fixture {
  geometry: { card_w: number; card_h: number; gap: number; corner_radius: number };
  viewport_w: number;
  n_cards: number;
  seed: number;
  build_timeline: {
    focus_moments: { card_index: number; hold_s: number; zoom_s: number }[];
    frames: GoldenFrame[];
  };
  rolling_timeline: {
    duration_s: number;
    frames: GoldenFrame[];
  };
}

const EPS = 1e-6;

function expectFramesMatch(frames: FrameState[], golden: GoldenFrame[]): void {
  expect(frames.length).toBe(golden.length);
  for (let i = 0; i < golden.length; i += 1) {
    const frame = frames[i];
    const expected = golden[i];
    expect(Math.abs(frame.tS - expected.t_s)).toBeLessThanOrEqual(EPS);
    expect(Math.abs(frame.scrollX - expected.scroll_x)).toBeLessThanOrEqual(EPS);
    expect(frame.focusCard).toBe(expected.focus_card);
    expect(Math.abs(frame.focusT - expected.focus_t)).toBeLessThanOrEqual(EPS);
    expect(Math.abs(frame.dim - expected.dim)).toBeLessThanOrEqual(EPS);
  }
}

describe("choreography fixture parity (fixture: choreography_traces.json)", () => {
  it("has the shared fixture", () => {
    expect(fs.existsSync(FIXTURE_PATH)).toBe(true);
  });

  const fixture: Fixture = JSON.parse(fs.readFileSync(FIXTURE_PATH, "utf-8"));
  const geo: CardGeometry = {
    cardW: fixture.geometry.card_w,
    cardH: fixture.geometry.card_h,
    gap: fixture.geometry.gap,
    cornerRadius: fixture.geometry.corner_radius,
  };

  it("buildTimeline(n_cards=4, focus on card 1, seed 0) matches the Python trace", () => {
    const moments = fixture.build_timeline.focus_moments.map((m) => ({
      cardIndex: m.card_index,
      holdS: m.hold_s,
      zoomS: m.zoom_s,
    }));
    const frames = buildTimeline(fixture.n_cards, geo, fixture.viewport_w, {
      focusMoments: moments,
      seed: fixture.seed,
    });
    expectFramesMatch(frames, fixture.build_timeline.frames);
  });

  it("rollingTimeline(n_cards=4, duration=6, seed=0) matches the Python trace", () => {
    const frames = rollingTimeline(
      fixture.n_cards,
      geo,
      fixture.viewport_w,
      fixture.rolling_timeline.duration_s,
      { seed: fixture.seed },
    );
    expectFramesMatch(frames, fixture.rolling_timeline.frames);
  });

  it("reports the max abs diff observed across both traces (informational)", () => {
    const moments = fixture.build_timeline.focus_moments.map((m) => ({
      cardIndex: m.card_index,
      holdS: m.hold_s,
      zoomS: m.zoom_s,
    }));
    const buildFrames = buildTimeline(fixture.n_cards, geo, fixture.viewport_w, {
      focusMoments: moments,
      seed: fixture.seed,
    });
    const rollFrames = rollingTimeline(
      fixture.n_cards,
      geo,
      fixture.viewport_w,
      fixture.rolling_timeline.duration_s,
      { seed: fixture.seed },
    );

    let maxDiff = 0;
    const diffAll = (frames: FrameState[], golden: GoldenFrame[]): void => {
      for (let i = 0; i < golden.length; i += 1) {
        maxDiff = Math.max(
          maxDiff,
          Math.abs(frames[i].tS - golden[i].t_s),
          Math.abs(frames[i].scrollX - golden[i].scroll_x),
          Math.abs(frames[i].focusT - golden[i].focus_t),
          Math.abs(frames[i].dim - golden[i].dim),
        );
      }
    };
    diffAll(buildFrames, fixture.build_timeline.frames);
    diffAll(rollFrames, fixture.rolling_timeline.frames);

    // eslint-disable-next-line no-console
    console.log(`choreography fixture max abs diff: ${maxDiff}`);
    expect(maxDiff).toBeLessThanOrEqual(EPS);
  });
});
