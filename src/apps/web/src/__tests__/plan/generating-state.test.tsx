/**
 * Tests for plan/_components/GeneratingState.tsx (PR4).
 *
 * Covers:
 *   1. startedAt provided → elapsed clock renders (contains time like "0:00").
 *   2. startedAt absent → no clock, no fabricated number.
 */

// @ts-nocheck
import React from "react";

import { act, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import GeneratingState from "@/app/plan/_components/GeneratingState";

describe("GeneratingState — elapsed clock", () => {
  beforeAll(() => {
    jest.useFakeTimers();
  });

  afterAll(() => {
    jest.useRealTimers();
  });

  it("test_elapsed_clock_with_started_at: shows elapsed time", async () => {
    const startedAt = new Date(Date.now() - 5000).toISOString(); // 5 seconds ago

    await act(async () => {
      render(
        <GeneratingState
          title="Building…"
          subtitle="Hang tight."
          startedAt={startedAt}
        />,
      );
    });

    // Advance timers so the effect runs.
    await act(async () => {
      jest.advanceTimersByTime(100);
    });

    // Should show elapsed time (e.g. "0:05" or "0:00").
    const clockEl = screen.queryByText(/\d+:\d{2}/);
    expect(clockEl).not.toBeNull();
  });

  it("test_no_clock_without_started_at: no fabricated number", async () => {
    await act(async () => {
      render(
        <GeneratingState
          title="Building…"
          subtitle="Hang tight."
          // No startedAt.
        />,
      );
    });

    await act(async () => {
      jest.advanceTimersByTime(100);
    });

    // No elapsed time displayed.
    const clockEl = screen.queryByText(/\d+:\d{2}/);
    expect(clockEl).toBeNull();
  });
});
