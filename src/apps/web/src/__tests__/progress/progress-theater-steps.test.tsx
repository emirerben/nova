/**
 * ProgressTheater × NovaActivityFeed integration — the NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED
 * flag gate. Flag off, or steps absent/empty, must render PhaseChipRow exactly
 * as before (byte-identical fallback — PR3's core additive-safety contract).
 */
// @ts-nocheck
import React from "react";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import "@testing-library/jest-dom";
import { ProgressTheater } from "@/components/progress/ProgressTheater";
import { BAND_COLLAPSE_MS, CELEBRATION_HOLD_MS } from "@/components/progress/constants";
import type { NovaStep } from "@/lib/job-phases";

/** The band is the wrapper that carries the D12 collapse classes — mirrors
 *  the helper in attempt-clock.test.tsx. */
function bandOf(el: HTMLElement): HTMLElement {
  const band = el.closest("div.space-y-3");
  if (!band) throw new Error("band wrapper not found");
  return band as HTMLElement;
}

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

/**
 * D12 override: the steps feed must NOT inherit the legacy "collapse the
 * band to height 0 after the celebration hold" behavior. Instead it settles
 * into NovaActivityFeed's own persistent one-line receipt. The flag-off /
 * no-steps path must stay byte-identical to today's D12 collapse — both
 * are pinned here side by side so a future change can't fix one and quietly
 * break the other.
 */
describe("ProgressTheater — D12 receipt-collapse override for the steps feed", () => {
  afterEach(() => {
    jest.useRealTimers();
  });

  it("legacy path (flag off): band collapses to height 0 after the celebration hold + collapse window — UNCHANGED", () => {
    delete process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED;
    jest.useFakeTimers();
    render(
      <ProgressTheater
        {...baseProps()}
        isTerminal
        isSuccess
        receiptText="Ready in 2:41"
        steps={steps}
      />,
    );

    act(() => {
      jest.advanceTimersByTime(CELEBRATION_HOLD_MS + BAND_COLLAPSE_MS + 100);
    });

    const collapsed = bandOf(screen.getByText("Ready in 2:41"));
    expect(collapsed.className).toMatch(/h-0/);
    expect(collapsed.className).toMatch(/opacity-0/);
    // The plain D12 receipt — no step count, no toggle. Passing `steps` when
    // the flag is off must never leak steps-feed UI into the legacy path.
    expect(screen.queryByText(/steps/)).not.toBeInTheDocument();
    expect(screen.queryByText("See what Nova did")).not.toBeInTheDocument();
  });

  it("steps-feed path (flag on): band never collapses — settles into NovaActivityFeed's persistent receipt, toggle keeps working", () => {
    process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
    jest.useFakeTimers();
    render(
      <ProgressTheater
        {...baseProps()}
        isTerminal
        isSuccess
        receiptText="Ready in 2:41"
        steps={steps}
      />,
    );

    act(() => {
      jest.advanceTimersByTime(CELEBRATION_HOLD_MS + BAND_COLLAPSE_MS + 100);
    });

    const settled = bandOf(screen.getByText("Ready in 2:41"));
    // The core assertion: no D12 height-0 collapse for this mode.
    expect(settled.className).not.toMatch(/h-0/);
    expect(settled.className).not.toMatch(/opacity-0/);
    expect(screen.getByText("2 steps")).toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: "See what Nova did" });
    expect(toggle).toBeInTheDocument();

    // Advance well past the old collapse window again — still persistent.
    act(() => {
      jest.advanceTimersByTime(60_000);
    });
    expect(bandOf(screen.getByText("Ready in 2:41")).className).not.toMatch(/h-0/);

    // The toggle keeps working indefinitely, not just within some window.
    fireEvent.click(screen.getByRole("button", { name: "See what Nova did" }));
    expect(screen.getByRole("list", { name: /nova ai steps/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Hide steps" }));
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
    expect(bandOf(screen.getByText("Ready in 2:41")).className).not.toMatch(/h-0/);
  });
});
