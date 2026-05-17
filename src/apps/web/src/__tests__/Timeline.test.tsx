/**
 * Tests for the admin job-debug Timeline view.
 *
 * Coverage focuses on the pure derivation logic (deriveTimeline) — the
 * render-time correctness lives in the data, not the CSS. We assert:
 *
 *   - slot cursor sums target_duration_s into absolute video timestamps
 *   - overlay events from pipeline_trace become the source of truth when
 *     present (post-merge / post-clamp values surface intact)
 *   - missing overlay events fall back to recipe-relative derivation with
 *     the approximate flag set
 *   - missing assembly_plan returns null (renderer shows a placeholder)
 *
 * The renderer itself is a thin CSS-grid; we don't snapshot the DOM.
 */

import { deriveTimeline } from "@/app/admin/jobs/[id]/Timeline";
import type { JobDebugResponse } from "@/lib/admin-jobs-api";

function makeData(overrides: Partial<JobDebugResponse["job"]> = {}): JobDebugResponse {
  return {
    job: {
      id: "j1",
      user_id: "u1",
      status: "done",
      job_type: "template",
      mode: null,
      template_id: "t1",
      music_track_id: null,
      failure_reason: null,
      error_detail: null,
      current_phase: null,
      phase_log: null,
      raw_storage_path: null,
      selected_platforms: null,
      probe_metadata: null,
      transcript: null,
      scene_cuts: null,
      all_candidates: null,
      assembly_plan: null,
      pipeline_trace: null,
      started_at: null,
      finished_at: null,
      created_at: "2026-05-17T00:00:00Z",
      updated_at: "2026-05-17T00:00:00Z",
      ...overrides,
    },
    job_clips: [],
    template: null,
    music_track: null,
    agent_runs: [],
    template_agent_runs: [],
    track_agent_runs: [],
  };
}

describe("deriveTimeline", () => {
  test("returns null when assembly_plan is missing", () => {
    expect(deriveTimeline(makeData())).toBeNull();
  });

  test("returns null when steps array is empty", () => {
    const data = makeData({ assembly_plan: { steps: [] } });
    expect(deriveTimeline(data)).toBeNull();
  });

  test("sums slot durations into cumulative absolute timestamps", () => {
    const data = makeData({
      assembly_plan: {
        steps: [
          { slot: { position: 1, target_duration_s: 3.0 }, clip_id: "a" },
          { slot: { position: 2, target_duration_s: 2.5 }, clip_id: "b" },
          { slot: { position: 3, target_duration_s: 4.0 }, clip_id: "c" },
        ],
      },
    });
    const bundle = deriveTimeline(data);
    expect(bundle).not.toBeNull();
    expect(bundle!.slots.map((s) => [s.abs_start_s, s.abs_end_s])).toEqual([
      [0, 3.0],
      [3.0, 5.5],
      [5.5, 9.5],
    ]);
    expect(bundle!.totalDuration).toBeCloseTo(9.5);
  });

  test("sources overlays from pipeline_trace render_window events when present", () => {
    const data = makeData({
      assembly_plan: {
        steps: [
          { slot: { position: 1, target_duration_s: 3.0 }, clip_id: "a" },
          { slot: { position: 2, target_duration_s: 3.0 }, clip_id: "b" },
        ],
      },
      pipeline_trace: [
        {
          ts: "2026-05-17T00:00:01Z",
          stage: "overlay",
          event: "render_window",
          data: {
            text: "WELCOME",
            abs_start_s: 0.5,
            abs_end_s: 2.8,
            slot_index: 1,
            position: "center",
            clamped_by: null,
            merged_from_slots: null,
          },
        },
        {
          ts: "2026-05-17T00:00:02Z",
          stage: "overlay",
          event: "render_window",
          data: {
            text: "TO MOROCCO",
            abs_start_s: 3.2,
            abs_end_s: 6.0,
            slot_index: 2,
            position: "center",
            clamped_by: "override",
            merged_from_slots: [1, 2],
          },
        },
      ],
    });
    const bundle = deriveTimeline(data)!;
    expect(bundle.overlaysApproximate).toBe(false);
    expect(bundle.overlays).toHaveLength(2);
    const merged = bundle.overlays.find((o) => o.text === "TO MOROCCO")!;
    expect(merged.abs_start_s).toBeCloseTo(3.2);
    expect(merged.abs_end_s).toBeCloseTo(6.0);
    expect(merged.clamped_by).toBe("override");
    expect(merged.merged_from_slots).toEqual([1, 2]);
  });

  test("falls back to recipe-relative overlay derivation when no render_window events", () => {
    const data = makeData({
      assembly_plan: {
        steps: [
          {
            slot: {
              position: 1,
              target_duration_s: 3.0,
              text_overlays: [
                {
                  text: "WELCOME",
                  start_offset_s: 0.5,
                  duration_s: 2.0,
                  position: "center",
                },
              ],
            },
            clip_id: "a",
          },
          {
            slot: {
              position: 2,
              target_duration_s: 3.0,
              text_overlays: [
                {
                  text: "TO MOROCCO",
                  start_offset_s: 0.2,
                  duration_s: 2.5,
                  position: "center",
                },
              ],
            },
            clip_id: "b",
          },
        ],
      },
      pipeline_trace: [],
    });
    const bundle = deriveTimeline(data)!;
    expect(bundle.overlaysApproximate).toBe(true);
    expect(bundle.overlays).toHaveLength(2);
    // Second overlay starts at slot 2 boundary (3.0) + offset (0.2) = 3.2.
    const second = bundle.overlays.find((o) => o.text === "TO MOROCCO")!;
    expect(second.abs_start_s).toBeCloseTo(3.2);
    expect(second.abs_end_s).toBeCloseTo(5.7);
    expect(second.approximate).toBe(true);
  });

  test("flags truncated when pipeline_trace hits the 500-event cap", () => {
    const events = Array.from({ length: 500 }, (_, i) => ({
      ts: `2026-05-17T00:00:${String(i).padStart(2, "0")}Z`,
      stage: "transition",
      event: "xfade_picked",
      data: { slot_index: i },
    }));
    const data = makeData({
      assembly_plan: {
        steps: [{ slot: { position: 1, target_duration_s: 3.0 }, clip_id: "a" }],
      },
      pipeline_trace: events,
    });
    const bundle = deriveTimeline(data)!;
    expect(bundle.truncated).toBe(true);
  });
});
