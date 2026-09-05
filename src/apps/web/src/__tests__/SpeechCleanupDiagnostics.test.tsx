import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import {
  parseSpeechCleanupAttempts,
  SPEECH_CLEANUP_CANDIDATE_STATUSES,
  SpeechCleanupDiagnostics,
} from "@/app/admin/jobs/[id]/SpeechCleanupDiagnostics";
import type { PipelineTraceEvent } from "@/lib/admin-jobs-api";

function analysisEvent(
  overrides: Record<string, unknown> = {},
  ts = "2026-09-01T10:00:00Z",
): PipelineTraceEvent {
  return {
    ts,
    stage: "silence_cut",
    event: "silence_cut_mixed_gap_analysis",
    data: {
      schema_version: 1,
      detector_version: "mixed-gap-v1",
      analysis_attempt_id: "attempt-a",
      analysis_view: "full_clip",
      source_slot: 0,
      assignment_status: "assigned",
      source_tag: "0123456789abcdef",
      analysis_policy: "required_v1",
      configured_mode: "shadow",
      effective_mode: "shadow",
      candidate_status: "ready",
      rollout_percent: 25,
      rollout_bucket: 17,
      duration_ms: 10_000,
      inputs: {
        asr_word_spans_ms: [
          [2040, 2530],
          [3255, 4359],
        ],
        asr_word_spans_omitted: 0,
        silence_spans_ms: [
          [0, 1215],
          [7978, 8293],
        ],
        silence_spans_omitted: 0,
        lexical_candidates_omitted: 0,
      },
      mixed_gap_scan: {
        records: [
          {
            island_start_ms: 7406,
            island_end_ms: 7978,
            detection: "eligible",
            reason: "bilateral_silence",
            plan_disposition: "selected_full",
          },
          {
            island_start_ms: 6000,
            island_end_ms: 6100,
            detection: "rejected",
            reason: "too_short",
            plan_disposition: "dropped",
          },
        ],
        records_omitted: 0,
      },
      baseline_plan: {
        removed_count: 2,
        removed_ms: 3000,
        removed_spans_ms: [
          [0, 2040],
          [9060, 10000],
        ],
        removed_spans_omitted: 0,
        clamped: false,
        bailout_reason: null,
      },
      candidate_plan: {
        removed_count: 3,
        removed_ms: 3572,
        removed_spans_ms: [
          [0, 2040],
          [7406, 7978],
          [9060, 10000],
        ],
        removed_spans_omitted: 0,
        clamped: true,
        bailout_reason: null,
      },
      selected_plan: "baseline",
      ...overrides,
    },
  };
}

function outcomeEvent(
  generation = "generation-live",
  outcome = "published_baseline_fallback",
): PipelineTraceEvent {
  return {
    ts: "2026-09-01T10:01:00Z",
    stage: "silence_cut",
    event: "speech_cleanup_render_outcome",
    data: {
      detector_version: "mixed-gap-v1",
      analysis_attempt_id: "attempt-a",
      analysis_view: "full_clip",
      source_tag: "0123456789abcdef",
      variant_id: "required-v1",
      render_generation_id: generation,
      outcome,
    },
  };
}

describe("SpeechCleanupDiagnostics", () => {
  test.each([
    "analysis_failed",
    "analysis_not_started",
    "build_failed",
    "outer_media_probe_failed",
    "precheck_clip_too_short",
    "precheck_no_audio",
    "ready",
    "receipt_build_failed",
    "tool_unavailable",
    "validation_failed",
  ])("keeps backend-supported candidate status %s visible", (candidateStatus) => {
    const attempts = parseSpeechCleanupAttempts([
      analysisEvent({ candidate_status: candidateStatus }),
    ]);

    expect(attempts).toHaveLength(1);
    expect(attempts[0].candidateStatus).toBe(candidateStatus);
    expect(SPEECH_CLEANUP_CANDIDATE_STATUSES).toContain(candidateStatus);
  });

  test("renders exact receipt plan bands and distinguishes eligible from rejected islands", () => {
    render(<SpeechCleanupDiagnostics events={[analysisEvent()]} />);

    expect(screen.getByText("Speech cleanup diagnostics")).toBeInTheDocument();
    expect(screen.getByTitle("Baseline cuts: 0 ms–2,040 ms")).toHaveStyle({
      left: "0%",
      width: "20.4%",
    });
    expect(
      screen.getByTitle(
        "Eligible island: 7,406 ms–7,978 ms · bilateral_silence · selected_full",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByTitle("Rejected island: 6,000 ms–6,100 ms · too_short · dropped"),
    ).toBeInTheDocument();
    expect(screen.getByText("baseline")).toBeInTheDocument();
    expect(screen.getByText("ready")).toBeInTheDocument();
  });

  test("defaults to newest attempt and retains an attempt selector", () => {
    const old = analysisEvent();
    const newest = analysisEvent(
      {
        analysis_attempt_id: "attempt-b",
        source_tag: null,
        source_slot: 2,
        assignment_status: "identity_cache_unavailable",
        rollout_bucket: null,
        configured_mode: "apply",
        effective_mode: "shadow",
        selected_plan: "candidate",
      },
      "2026-09-01T11:00:00Z",
    );
    render(<SpeechCleanupDiagnostics events={[old, newest]} />);

    expect(
      (screen.getByLabelText("Speech cleanup attempt") as HTMLSelectElement).value,
    ).toContain("attempt-b");
    expect(screen.getByText("identity_cache_unavailable")).toBeInTheDocument();
    expect(screen.getAllByText("unassigned")).not.toHaveLength(0);

    fireEvent.change(screen.getByLabelText("Speech cleanup attempt"), {
      target: { value: parseSpeechCleanupAttempts([old, newest])[0].key },
    });
    expect(screen.getByText("assigned")).toBeInTheDocument();
    expect(screen.getByLabelText("Speech cleanup attempt")).toHaveClass(
      "min-h-11",
      "w-full",
      "text-base",
      "focus-visible:ring-2",
    );
  });

  test("shortens a long selector label while retaining the full attempt id", () => {
    const longAttemptId = `attempt-${"x".repeat(80)}`;
    render(
      <SpeechCleanupDiagnostics
        events={[
          analysisEvent(),
          analysisEvent(
            { analysis_attempt_id: longAttemptId },
            "2026-09-01T11:00:00Z",
          ),
        ]}
      />,
    );

    const selectedOption = screen.getByRole("option", { selected: true });
    expect(selectedOption).toHaveTextContent("…");
    expect(selectedOption).not.toHaveTextContent(longAttemptId);
    expect(screen.getByText(`Attempt ${longAttemptId}`)).toBeInTheDocument();
  });

  test("correlates terminal publication and only calls the exact current generation live", () => {
    const events = [analysisEvent(), outcomeEvent()];
    const { rerender } = render(
      <SpeechCleanupDiagnostics
        events={events}
        assemblyPlan={{
          variants: [
            { variant_id: "required-v1", render_generation_id: "generation-live" },
          ],
        }}
      />,
    );
    expect(
      screen.getByText("published_baseline_fallback — currently live"),
    ).toBeInTheDocument();

    rerender(
      <SpeechCleanupDiagnostics
        events={events}
        assemblyPlan={{
          variants: [
            { variant_id: "required-v1", render_generation_id: "generation-new" },
          ],
        }}
      />,
    );
    expect(
      screen.getByText("published_baseline_fallback — historical generation"),
    ).toBeInTheDocument();

    rerender(<SpeechCleanupDiagnostics events={events} />);
    expect(
      screen.getByText("published_baseline_fallback — current generation unavailable"),
    ).toBeInTheDocument();
  });

  test("surfaces malformed and version-skewed receipts without breaking the page", () => {
    const badVersion = analysisEvent({ schema_version: 2 });
    const malformed = analysisEvent({ duration_ms: "ten seconds" });
    render(<SpeechCleanupDiagnostics events={[badVersion, malformed]} />);

    expect(screen.getByText("Speech cleanup diagnostics")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "2 analysis receipts are unavailable",
    );
  });

  test("reports discarded receipts while retaining valid attempts", () => {
    render(
      <SpeechCleanupDiagnostics
        events={[analysisEvent(), analysisEvent({ schema_version: 2 })]}
      />,
    );

    expect(screen.getByText("ready")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "1 additional analysis receipt could not be displayed",
    );
  });

  test("renders nothing when no analysis receipt exists", () => {
    const { container } = render(<SpeechCleanupDiagnostics events={[outcomeEvent()]} />);
    expect(container).toBeEmptyDOMElement();
  });

  test("labels truncated timing bands as partial rather than complete", () => {
    const event = analysisEvent({
      candidate_plan: {
        removed_count: 100,
        removed_ms: 5000,
        removed_spans_ms: [[7406, 7978]],
        removed_spans_omitted: 99,
        clamped: true,
      },
    });
    render(<SpeechCleanupDiagnostics events={[event]} />);

    expect(screen.getByRole("status")).toHaveTextContent("Partial receipt");
    expect(screen.getByRole("img", { name: /Candidate cuts:.*partial receipt/ })).toBeInTheDocument();
    expect(screen.getByText("(partial)")).toBeInTheDocument();
  });
});
