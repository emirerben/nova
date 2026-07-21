/**
 * ProgressTheater `retrying` contract (2026-07-21 OOM incident).
 *
 * A worker killed mid-render leaves the job "rendering" with zero signal for
 * the whole acks_late redelivery window. When the status route reports
 * `retrying: true` (stale worker heartbeat), the theater must replace the
 * reassuring leave-note with honest recovery copy — and revert once the
 * redelivered attempt resumes.
 */

// @ts-nocheck
import React from "react";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

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

import { ProgressTheater } from "@/components/progress/ProgressTheater";

const BASE_PROPS = {
  phases: ["queued", "assemble"] as const,
  phaseLabels: { queued: "Queued", assemble: "Assembling" },
  currentPhase: "assemble",
  expectedPhaseMs: null,
  phaseLog: null,
  startedAt: new Date().toISOString(),
  jobCreatedAt: new Date().toISOString(),
  isTerminal: false,
  isSuccess: false,
};

describe("ProgressTheater retrying contract", () => {
  it("test_retrying_shows_recovery_note: retrying replaces the leave-note", () => {
    render(<ProgressTheater {...BASE_PROPS} retrying />);
    expect(screen.getByText(/Hit a snag mid-render — retrying automatically/)).toBeInTheDocument();
    expect(screen.queryByText(/You can leave this page/)).not.toBeInTheDocument();
  });

  it("test_not_retrying_keeps_leave_note: default stays on the reassuring copy", () => {
    render(<ProgressTheater {...BASE_PROPS} />);
    expect(screen.getByText(/You can leave this page/)).toBeInTheDocument();
    expect(screen.queryByText(/Hit a snag mid-render/)).not.toBeInTheDocument();
  });

  it("test_terminal_hides_recovery_note: a finished job never shows retry copy", () => {
    render(<ProgressTheater {...BASE_PROPS} retrying isTerminal isSuccess />);
    expect(screen.queryByText(/Hit a snag mid-render/)).not.toBeInTheDocument();
  });

  it("test_retrying_suppresses_eta: no confident ETA above the recovery note", () => {
    const withEta = { ...BASE_PROPS, expectedPhaseMs: { queued: 10000, assemble: 300000 } };
    const { rerender } = render(<ProgressTheater {...withEta} />);
    expect(screen.getByText(/left|less than a minute/)).toBeInTheDocument();
    rerender(<ProgressTheater {...withEta} retrying />);
    expect(screen.queryByText(/left|less than a minute/)).not.toBeInTheDocument();
  });

  it("test_recovery_note_is_a_live_region: screen readers hear the state change", () => {
    render(<ProgressTheater {...BASE_PROPS} retrying />);
    const note = screen.getByText(/Hit a snag mid-render/);
    expect(note).toHaveAttribute("role", "status");
    expect(note).toHaveAttribute("aria-live", "polite");
  });
});
