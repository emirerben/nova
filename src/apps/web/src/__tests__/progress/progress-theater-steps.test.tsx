/**
 * ProgressTheater × NovaActivityFeed integration — the NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED
 * flag gate. Flag off, or steps absent/empty, must render PhaseChipRow exactly
 * as before (byte-identical fallback — PR3's core additive-safety contract).
 */
// @ts-nocheck
import React from "react";
import { render, screen, within } from "@testing-library/react";
import "@testing-library/jest-dom";
import { ProgressTheater } from "@/components/progress/ProgressTheater";
import type { NovaStep } from "@/lib/job-phases";

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: jest.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: jest.fn(),
    removeListener: jest.fn(),
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    dispatchEvent: jest.fn(),
  })),
});

const phases = ["queued", "analyze_clips", "render_variants"];
const phaseLabels = {
  queued: "Queued",
  analyze_clips: "Analyzing your clips",
  render_variants: "Rendering your edits",
};

const steps: NovaStep[] = [
  { id: "s1", ts: "t1", kind: "phase", label: "Analyzed your clips", detail: null, status: "done" },
  { id: "s2", ts: "t2", kind: "render", label: "Rendering variant 1 of 3", detail: null, status: "active" },
];

function baseProps() {
  return {
    phases,
    phaseLabels,
    currentPhase: "render_variants",
    expectedPhaseMs: null,
    phaseLog: null,
    startedAt: new Date().toISOString(),
    jobCreatedAt: new Date().toISOString(),
    isTerminal: false,
    isSuccess: false,
    size: "full" as const,
    tone: "light" as const,
  };
}

afterEach(() => {
  delete process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED;
});

describe("ProgressTheater — steps feed flag gate", () => {
  it("flag unset: renders PhaseChipRow even when steps is provided", () => {
    delete process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED;
    render(<ProgressTheater {...baseProps()} steps={steps} />);
    expect(screen.getByRole("list", { name: "Processing phases" })).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: /nova ai steps/i })).not.toBeInTheDocument();
  });

  it("flag on, steps empty: falls back to PhaseChipRow", () => {
    process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
    render(<ProgressTheater {...baseProps()} steps={[]} />);
    expect(screen.getByRole("list", { name: "Processing phases" })).toBeInTheDocument();
  });

  it("flag on, steps null: falls back to PhaseChipRow", () => {
    process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
    render(<ProgressTheater {...baseProps()} steps={null} />);
    expect(screen.getByRole("list", { name: "Processing phases" })).toBeInTheDocument();
  });

  it("flag on, steps non-empty: renders NovaActivityFeed instead of PhaseChipRow", () => {
    process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
    render(<ProgressTheater {...baseProps()} steps={steps} />);
    const list = screen.getByRole("list", { name: /nova ai steps/i });
    expect(screen.queryByRole("list", { name: "Processing phases" })).not.toBeInTheDocument();
    expect(within(list).getByText("Rendering variant 1 of 3")).toBeInTheDocument();
  });

  it("passes pending phase labels (beyond currentPhase) into the feed while not terminal", () => {
    process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
    render(
      <ProgressTheater
        {...baseProps()}
        currentPhase="analyze_clips"
        steps={[steps[0]]}
      />,
    );
    // "render_variants" is the only phase after "analyze_clips" in `phases`.
    expect(screen.getByText("Rendering your edits")).toBeInTheDocument();
  });

  it("no NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED consumer touches PhaseChipRow's rendering when flag/steps absent (no prop drift)", () => {
    render(<ProgressTheater {...baseProps()} />);
    expect(screen.getByRole("list", { name: "Processing phases" })).toBeInTheDocument();
  });
});
