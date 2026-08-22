import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "@jest/globals";
import {
  slotWindows,
  type DraftSlot,
} from "@/app/generative/timeline-math";
import {
  buildVirtualTimeline,
  mapVirtualTime,
} from "@/app/plan/items/[id]/_editor/virtual-timeline";

type Segment = {
  segment_id: string;
  media_id: string;
  duration_frames: number;
  source_start_frames?: number;
  source_end_frames?: number;
  transition_after?: "cut" | "crossfade" | "dip_to_black" | "flash";
  transition_duration_frames?: number;
};

type Window = {
  segment_id: string;
  start_frame: number;
  end_frame: number;
  overlap_before_frames: number;
};

type ParityCase = {
  id: string;
  baseline_segments?: Segment[];
  segments: Segment[];
  expected: {
    order: string[];
    windows: Window[];
    total_frames: number;
    scrub?: Array<{
      output_frame: number;
      segment_id: string;
      source_frame: number;
    }>;
    lanes?: Record<string, unknown>;
  };
  lane_inputs?: Record<string, Array<Record<string, number | string>>>;
};

const fixture = JSON.parse(
  readFileSync(
    resolve(process.cwd(), "../../../tests/fixtures/guided-story-parity/v1.json"),
    "utf8",
  ),
) as { frame_rate: number; cases: ParityCase[] };

function draftSlots(segments: Segment[]): DraftSlot[] {
  return segments.map((segment, index) => ({
    key: segment.segment_id,
    slotId: segment.segment_id,
    clipIndex: index,
    inS: (segment.source_start_frames ?? 0) / fixture.frame_rate,
    durationBeats: null,
    durationS: segment.duration_frames / fixture.frame_rate,
    removed: false,
    momentDescription: null,
    transitionAfter: segment.transition_after ?? "cut",
    transitionDurationS:
      (segment.transition_duration_frames ?? 0) / fixture.frame_rate,
  }));
}

function assertWindows(segments: Segment[], expected: Window[], totalFrames: number): void {
  const slots = draftSlots(segments);
  const windows = slotWindows(slots, []);
  expect(windows).toHaveLength(expected.length);
  expect(windows.map((window) => window.startS == null ? null : Math.round(window.startS * fixture.frame_rate))).toEqual(
    expected.map((window) => window.start_frame),
  );
  expect(windows.map((window) => Math.round(((window.startS ?? 0) + window.durationS) * fixture.frame_rate))).toEqual(
    expected.map((window) => window.end_frame),
  );
  expect(Math.round((windows.at(-1)?.startS ?? 0) * fixture.frame_rate + (windows.at(-1)?.durationS ?? 0) * fixture.frame_rate)).toBe(totalFrames);
}

function projectLaneInputs(
  segments: Segment[],
  expectedWindows: Window[],
  laneInputs: Record<string, Array<Record<string, number | string>>>,
) {
  const windows = new Map(expectedWindows.map((window) => [window.segment_id, window]));
  return {
    ...Object.fromEntries(
    Object.entries(laneInputs).map(([lane, items]) => [
      lane,
      items.map((item) => {
        const window = windows.get(String(item.segment_id));
        if (!window) {
          return {
            id: item.id,
            output_start_frame: null,
            output_end_frame: null,
            tombstone: true,
          };
        }
        const start = window.start_frame + Number(item.start_offset_frames);
        const end = Math.min(
          window.end_frame,
          window.start_frame + Number(item.end_offset_frames),
        );
        return {
          id: item.id,
          output_start_frame: start,
          output_end_frame: end,
          tombstone: false,
        };
      }),
    ]),
    ),
    music: { start_frame: 0, clock: "output" },
  };
}

describe("Guided Story V2 shared projection parity", () => {
  it.each(fixture.cases)("projects $id with exact 30fps windows", (testCase) => {
    assertWindows(
      testCase.segments,
      testCase.expected.windows,
      testCase.expected.total_frames,
    );
    expect(testCase.segments.map((segment) => segment.segment_id)).toEqual(
      testCase.expected.order,
    );
  });

  it("keeps output-clock music at frame zero while all timed lanes ripple", () => {
    const testCase = fixture.cases.find(
      (candidate) => candidate.id === "trim_delete_reorder_and_timed_lanes",
    )!;
    const projected = projectLaneInputs(
      testCase.segments,
      testCase.expected.windows,
      testCase.lane_inputs!,
    );
    expect(projected).toEqual(testCase.expected.lanes);
    expect((testCase.expected.lanes?.music as { start_frame: number }).start_frame).toBe(0);
  });

  it("keeps trim/delete/reorder identities stable across the projected draft", () => {
    const testCase = fixture.cases.find(
      (candidate) => candidate.id === "trim_delete_reorder_and_timed_lanes",
    )!;
    expect(testCase.baseline_segments?.map((segment) => segment.segment_id)).toEqual([
      "a",
      "b",
      "c",
    ]);
    expect(testCase.segments.map((segment) => segment.segment_id)).toEqual(["c", "b"]);
    expect(testCase.segments.find((segment) => segment.segment_id === "b")).toMatchObject({
      duration_frames: 45,
      source_start_frames: 15,
      source_end_frames: 60,
    });
    expect(testCase.segments.some((segment) => segment.segment_id === "a")).toBe(false);
  });

  it.each(
    (fixture.cases.filter((testCase) => testCase.expected.scrub) as Array<ParityCase & {
      expected: ParityCase["expected"] & { scrub: NonNullable<ParityCase["expected"]["scrub"]> };
    }>),
  )("uses right-biased inverse scrub mapping for $id", (testCase) => {
    const clips = testCase.segments.map((segment, index) => ({
      clip_index: index,
      signed_url: `https://fixture.invalid/${segment.media_id}.mp4`,
    }));
    const timeline = buildVirtualTimeline(draftSlots(testCase.segments), clips, []);
    for (const point of testCase.expected.scrub) {
      const mapping = mapVirtualTime(
        timeline,
        point.output_frame / fixture.frame_rate,
      );
      expect(mapping?.entry.kind).toBe("clip");
      expect(mapping && mapping.entry.kind === "clip" ? mapping.entry.slotKey : null).toBe(
        point.segment_id,
      );
      expect(mapping?.sourceTimeS == null ? null : Math.round(mapping.sourceTimeS * fixture.frame_rate)).toBe(
        point.source_frame,
      );
    }
  });
});
