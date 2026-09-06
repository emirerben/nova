import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import {
  SpeechCleanupDecisionCard,
  SpeechCleanupReceipt,
} from "@/app/plan/_components/workspace/SpeechCleanupDecisionCard";
import type { CreationSpeechCleanupProjection } from "@/lib/creation-thread-api";

const callbacks = {
  onGenerate: jest.fn(),
  onRetryAnalysis: jest.fn(),
  onRetryRender: jest.fn(),
  onCreateWithoutCleanup: jest.fn(),
};

function findings(overrides: Partial<CreationSpeechCleanupProjection> = {}): CreationSpeechCleanupProjection {
  return {
    applicable: true,
    analysis: {
      id: "analysis-1",
      status: "ready",
      has_findings: true,
      candidate_count: 5,
      category_counts: { filler_sound: 4, long_pause: 1 },
      estimated_removed_ms: 2800,
      error: null,
    },
    decision: null,
    requires_choice: true,
    render_blocker: null,
    outcome: null,
    ...overrides,
  };
}

describe("SpeechCleanupDecisionCard", () => {
  beforeEach(() => Object.values(callbacks).forEach((callback) => callback.mockReset()));

  it("matches the approved neutral boxed decision and has semantic structure", () => {
    const { container } = render(
      <SpeechCleanupDecisionCard cleanup={findings()} formatLabel="Talking to camera" {...callbacks} />,
    );

    expect(screen.getByRole("heading", { name: "Speech cleanup" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Clean up 5 speech moments?" })).toBeInTheDocument();
    expect(screen.getByText("Kria found 4 filler sounds and 1 long pause. Cleaning them removes about 2.8 seconds, and captions stay in sync.")).toBeInTheDocument();
    const clean = screen.getByRole("button", { name: "Clean up and create" });
    const keep = screen.getByRole("button", { name: "Keep speech and create" });
    const choiceGrid = screen.getByTestId("speech-cleanup-choice-grid");
    expect(screen.getByTestId("speech-cleanup-findings")).toHaveClass("rounded-2xl", "bg-white", "shadow-sm");
    expect(choiceGrid).toHaveClass("grid-cols-1", "md:grid-cols-2");
    expect(choiceGrid).not.toHaveClass("sm:grid-cols-2");
    expect(clean).toHaveClass("min-h-[112px]", "w-full", "items-start", "justify-start");
    expect(keep).toHaveClass("min-h-[112px]", "w-full", "items-start", "justify-start");
    expect(clean.className).toBe(keep.className);
    expect(clean).toHaveClass("focus-visible:ring-1");
    expect(keep).toHaveClass("focus-visible:ring-1");
    expect(screen.getByText("Remove the detected moments and tighten the delivery.")).toBeInTheDocument();
    expect(screen.getByText("Leave your delivery exactly as recorded.")).toBeInTheDocument();
    expect(screen.getAllByRole("button")).toEqual([clean, keep]);
    expect(clean).not.toHaveAttribute("aria-pressed");
    expect(keep).not.toHaveAttribute("aria-pressed");
    expect(container.querySelector('[aria-live="polite"]')).toBeNull();

    fireEvent.click(clean);
    fireEvent.click(keep);
    expect(callbacks.onGenerate).toHaveBeenNthCalledWith(1, "clean");
    expect(callbacks.onGenerate).toHaveBeenNthCalledWith(2, "keep_original");
  });

  it("degrades findings copy truthfully when category and duration detail are absent", () => {
    const cleanup = findings({
      analysis: { id: "analysis-2", status: "ready", has_findings: true },
    });
    render(<SpeechCleanupDecisionCard cleanup={cleanup} formatLabel="Narrated" {...callbacks} />);
    expect(screen.getByText("Kria found speech moments it can clean up while keeping captions in sync.")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Clean up the speech?" })).toBeInTheDocument();
  });

  it("keeps the caption assurance when duration detail is absent", () => {
    const cleanup = findings({
      analysis: {
        id: "analysis-categories-only",
        status: "ready",
        has_findings: true,
        candidate_count: 2,
        category_counts: { filler_sounds: 2 },
      },
    });
    render(<SpeechCleanupDecisionCard cleanup={cleanup} formatLabel="Narrated" {...callbacks} />);
    expect(screen.getByText("Kria found 2 filler sounds. Captions stay in sync.")).toBeInTheDocument();
  });

  it("shows checking without a fake percentage and respects reduced-motion CSS", () => {
    const cleanup = findings({ analysis: { id: "analysis-1", status: "running" } });
    const { container } = render(<SpeechCleanupDecisionCard cleanup={cleanup} formatLabel="Narrated" {...callbacks} />);
    expect(screen.getByText("Checking for filler sounds…")).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
    expect(container.querySelector(".motion-safe\\:animate-pulse")).toBeInTheDocument();
  });

  it("keeps audio-only evidence visible but sends no premature choice", () => {
    render(
      <SpeechCleanupDecisionCard
        cleanup={findings({ render_blocker: "video_required" })}
        formatLabel="Narrated"
        {...callbacks}
      />,
    );
    expect(screen.getByText("Add at least one video clip to make your video.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clean up and create" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Keep speech and create" })).not.toBeInTheDocument();
    expect(callbacks.onGenerate).not.toHaveBeenCalled();
  });

  it("renders no-findings as a quiet confirmation and omits a choice", () => {
    const cleanup = findings({
      analysis: { id: "analysis-3", status: "no_findings", has_findings: false },
      requires_choice: false,
    });
    render(
      <SpeechCleanupDecisionCard
        cleanup={cleanup}
        formatLabel="Talking to camera"
        direction="Keep the opening concise."
        {...callbacks}
      />,
    );
    expect(screen.getByText("Speech checked. No cleanup suggested.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create this video" }));
    expect(callbacks.onGenerate).toHaveBeenCalledWith();
  });

  it("keeps audio-only no-findings visible without exposing create", () => {
    render(
      <SpeechCleanupDecisionCard
        cleanup={findings({
          analysis: { id: "analysis-audio-none", status: "no_findings", has_findings: false },
          requires_choice: false,
          render_blocker: "video_required",
        })}
        formatLabel="Narrated"
        {...callbacks}
      />,
    );
    expect(screen.getByText("Speech checked. No cleanup suggested.")).toBeInTheDocument();
    expect(screen.getByText("Add at least one video clip to make your video.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create this video" })).not.toBeInTheDocument();
  });

  it("splits retryable and nonretryable analysis recovery", () => {
    const retryable = findings({
      analysis: { id: "analysis-4", status: "failed", error: { code: "analysis_timeout", retryable: true } },
    });
    const view = render(<SpeechCleanupDecisionCard cleanup={retryable} formatLabel="Narrated" {...callbacks} />);
    fireEvent.click(screen.getByRole("button", { name: "Retry speech check" }));
    fireEvent.click(screen.getByRole("button", { name: "Create without cleanup" }));
    expect(callbacks.onRetryAnalysis).toHaveBeenCalledTimes(1);
    expect(callbacks.onCreateWithoutCleanup).toHaveBeenCalledTimes(1);

    view.rerender(
      <SpeechCleanupDecisionCard
        cleanup={findings({
          analysis: { id: "analysis-5", status: "failed", error: { code: "unsupported_media", retryable: false } },
        })}
        formatLabel="Narrated"
        {...callbacks}
      />,
    );
    expect(screen.queryByRole("button", { name: "Retry speech check" })).not.toBeInTheDocument();
    expect(screen.getByText(/Replace the narration/)).toBeInTheDocument();
  });

  it("uses generic truthful guidance for an unknown nonretryable failure", () => {
    render(
      <SpeechCleanupDecisionCard
        cleanup={findings({
          analysis: { id: "analysis-unknown", status: "failed", error: { code: "future_error", retryable: false } },
        })}
        formatLabel="Talking to camera"
        {...callbacks}
      />,
    );
    expect(screen.getByText("Your footage and direction are safe.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry speech check" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create without cleanup" })).toBeInTheDocument();
  });

  it("retries an application failure with the same snapshot or allows explicit bypass", () => {
    const cleanup = findings({
      outcome: {
        job_id: "job-1",
        render_generation_id: "generation-1",
        status: "failed",
        error: { code: "audio_apply_failed", retryable: true },
      },
    });
    render(<SpeechCleanupDecisionCard cleanup={cleanup} formatLabel="Narrated" {...callbacks} />);
    fireEvent.click(screen.getByRole("button", { name: "Retry cleanup" }));
    fireEvent.click(screen.getByRole("button", { name: "Create without cleanup" }));
    expect(callbacks.onRetryRender).toHaveBeenCalledTimes(1);
    expect(callbacks.onCreateWithoutCleanup).toHaveBeenCalledTimes(1);
  });

  it("does not invent retryability for an incomplete application-failure projection", () => {
    render(
      <SpeechCleanupDecisionCard
        cleanup={findings({
          outcome: {
            job_id: "job-unknown",
            status: "failed",
            error: null,
          },
        })}
        formatLabel="Talking to camera"
        {...callbacks}
      />,
    );
    expect(screen.queryByRole("button", { name: "Retry cleanup" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create without cleanup" })).toBeInTheDocument();
  });

  it("keeps the video requirement visible after an application failure", () => {
    render(
      <SpeechCleanupDecisionCard
        cleanup={findings({
          render_blocker: "video_required",
          outcome: {
            job_id: "job-video-required",
            status: "failed",
            error: { code: "audio_apply_failed", retryable: true },
          },
        })}
        formatLabel="Narrated"
        {...callbacks}
      />,
    );
    expect(screen.getByText("Add at least one video clip to make your video.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create without cleanup" })).not.toBeInTheDocument();
  });

  it.each([
    ["applied", "Speech cleanup applied · 5 moments removed"],
    ["checked_no_change", "Speech cleanup checked · no safe cuts applied"],
    ["declined", "Speech kept as recorded"],
    ["bypassed_unchecked", "Created without checking speech cleanup"],
    ["failed", "Speech cleanup did not complete"],
  ] as const)("renders the %s receipt truthfully", (status, expected) => {
    const { container } = render(
      <SpeechCleanupReceipt
        outcome={{ job_id: "job-1", status, removal_count: status === "applied" ? 5 : null }}
      />,
    );
    expect(screen.getByText(expected)).toBeInTheDocument();
    expect(screen.getByTestId("speech-cleanup-receipt")).toHaveClass("border-y", "text-base");
    expect(container.querySelector('[aria-hidden="true"]')).toHaveClass("bg-lime-600");
  });

  it("disables every decision while one action is saving", () => {
    render(
      <SpeechCleanupDecisionCard
        cleanup={findings()}
        formatLabel="Narrated"
        busy
        pendingAction="clean"
        {...callbacks}
      />,
    );
    expect(screen.getByRole("group", { name: "Clean up 5 speech moments?" })).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("button", { name: "Starting cleanup…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Keep speech and create" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Starting cleanup…" }));
    fireEvent.click(screen.getByRole("button", { name: "Keep speech and create" }));
    expect(callbacks.onGenerate).not.toHaveBeenCalled();
  });
});
