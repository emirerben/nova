/**
 * Render-attempt clock contract (the "40m 32s" report).
 *
 * A re-render reuses the same Job row. The backend now moves `job.started_at` to
 * the Save press, so the elapsed counter restarts — but three frontend behaviors
 * have to cooperate or the user still never sees it:
 *
 *   1. The D12 receipt band collapses to `opacity-0 h-0` when a render succeeds,
 *      and that collapse used to be one-way. On an in-place edit the component
 *      never remounts, so the restarted clock rendered INSIDE the collapsed band —
 *      present in the DOM, invisible on screen. The tests here assert the WRAPPER
 *      CLASSES, not just the text: with `bandCollapsed` still true the chips and
 *      leave-note are both still findable by text, so a text-only assertion
 *      passes on the broken code.
 *   2. `isGenerativeJobSettled` must not treat a live re-render as settled, or the
 *      poller stops the instant the user hits Save.
 *   3. `deriveReceiptText` must not subtract a stale `finished_at` from a fresh
 *      `started_at` — that rendered "Ready in -36:-12".
 *
 * Everything below imports the REAL production functions. An earlier version of
 * this file re-implemented `deriveReceiptText` locally, which meant deleting the
 * production guard left the suite green.
 */

import React from "react";
import { render, screen, act } from "@testing-library/react";
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
import { deriveReceiptText, RECEIPT_FALLBACK } from "@/components/progress/logic";
import {
  GENERATIVE_FAILED_STATUSES,
  GENERATIVE_SUCCESS_STATUSES,
  GENERATIVE_TERMINAL_STATUSES,
  isGenerativeJobSettled,
} from "@/lib/generative-api";

const BASE_PROPS = {
  phases: ["queued", "assemble"] as readonly string[],
  phaseLabels: { queued: "Queued", assemble: "Assembling" },
  currentPhase: "assemble",
  expectedPhaseMs: null,
  phaseLog: null,
  startedAt: new Date().toISOString(),
  jobCreatedAt: new Date().toISOString(),
  isTerminal: false,
  isSuccess: false,
};

/** The band is the wrapper that carries the collapse classes. */
function bandOf(el: HTMLElement): HTMLElement {
  const band = el.closest("div.space-y-3");
  if (!band) throw new Error("band wrapper not found");
  return band as HTMLElement;
}

describe("ProgressTheater band revival on re-render", () => {
  it("test_collapsed_band_loses_its_zero_height_when_the_job_leaves_terminal", () => {
    jest.useFakeTimers();
    const { rerender } = render(
      <ProgressTheater {...BASE_PROPS} isTerminal isSuccess receiptText="Ready in 5:00" />,
    );

    act(() => {
      jest.advanceTimersByTime(20_000);
    });
    // Control: prove the assertion below can actually fail.
    const collapsed = bandOf(screen.getByText("Ready in 5:00"));
    expect(collapsed.className).toMatch(/h-0/);
    expect(collapsed.className).toMatch(/opacity-0/);

    // The user edits and presses Save: same mount, back to non-terminal.
    rerender(<ProgressTheater {...BASE_PROPS} isTerminal={false} isSuccess={false} />);

    const revived = bandOf(screen.getByText(/You can leave this page/));
    expect(revived.className).not.toMatch(/h-0/);
    expect(revived.className).not.toMatch(/opacity-0/);
    expect(screen.queryByText("Ready in 5:00")).not.toBeInTheDocument();
    jest.useRealTimers();
  });

  it("test_elapsed_reanchors_when_started_at_moves_on_the_same_mount", () => {
    // The item page never remounts across a re-render, so re-anchoring on a prop
    // change IS the frontend half of the fix.
    const { container, rerender } = render(
      <ProgressTheater
        {...BASE_PROPS}
        startedAt={new Date(Date.now() - 40 * 60_000 - 32_000).toISOString()}
      />,
    );
    // The exact string from the bug report.
    expect(container.textContent).toMatch(/40m 3\ds/);

    rerender(<ProgressTheater {...BASE_PROPS} startedAt={new Date().toISOString()} />);
    expect(container.textContent).toContain("0s");
    expect(container.textContent).not.toMatch(/\d+m \d\ds/);
  });

  it("test_a_failed_terminal_also_clears_a_stale_receipt", () => {
    // The reset guard is `!isTerminal || !isSuccess` — this is the second half.
    // A re-render that FAILS stays terminal but drops isSuccess, and the band must
    // not keep showing the previous render's "Ready in …" receipt.
    jest.useFakeTimers();
    const { rerender } = render(
      <ProgressTheater {...BASE_PROPS} isTerminal isSuccess receiptText="Ready in 5:00" />,
    );
    act(() => {
      jest.advanceTimersByTime(20_000);
    });
    expect(bandOf(screen.getByText("Ready in 5:00")).className).toMatch(/h-0/);

    rerender(
      <ProgressTheater {...BASE_PROPS} isTerminal isSuccess={false} receiptText="Ready in 5:00" />,
    );

    expect(screen.queryByText("Ready in 5:00")).not.toBeInTheDocument();
    const revived = bandOf(screen.getByRole("heading", { name: "Assembling" }));
    expect(revived.className).not.toMatch(/h-0/);
    jest.useRealTimers();
  });

  it("test_the_elapsed_clock_ticks_again_after_returning_to_non_terminal", () => {
    // A restarted clock that renders once and freezes is still a broken timer:
    // the elapsed interval is skipped while terminal, so it has to re-subscribe.
    jest.useFakeTimers();
    const { container, rerender } = render(
      <ProgressTheater {...BASE_PROPS} isTerminal isSuccess={false} />,
    );
    rerender(
      <ProgressTheater
        {...BASE_PROPS}
        isTerminal={false}
        isSuccess={false}
        startedAt={new Date().toISOString()}
      />,
    );
    expect(container.textContent).toContain("0s");
    act(() => {
      jest.advanceTimersByTime(6_000);
    });
    expect(container.textContent).toMatch(/[46]s/);
    jest.useRealTimers();
  });
});

describe("isGenerativeJobSettled", () => {
  const rendering = [{ render_status: "rendering", render_started_at: new Date().toISOString() }];
  const ready = [{ render_status: "ready", render_started_at: new Date().toISOString() }];

  it("test_live_rerender_on_a_success_status_is_not_settled", () => {
    // The whole point: a re-render leaves job.status at variants_ready.
    expect(isGenerativeJobSettled("variants_ready", rendering)).toBe(false);
    expect(isGenerativeJobSettled("variants_ready_partial", rendering)).toBe(false);
  });

  it("test_failed_status_wins_over_a_frozen_rendering_variant", () => {
    // Data-integrity escape: a stuck variant after a failed job must never block
    // the UI. This is the guard the fix deliberately preserved.
    expect(isGenerativeJobSettled("variants_failed", rendering)).toBe(true);
    expect(isGenerativeJobSettled("processing_failed", rendering)).toBe(true);
  });

  it("test_all_ready_on_a_success_status_is_settled", () => {
    expect(isGenerativeJobSettled("variants_ready", ready)).toBe(true);
    expect(isGenerativeJobSettled("variants_ready", [])).toBe(true);
    expect(isGenerativeJobSettled("variants_ready", null)).toBe(true);
  });

  it("test_non_terminal_status_is_never_settled", () => {
    expect(isGenerativeJobSettled("processing", ready)).toBe(false);
    expect(isGenerativeJobSettled("rendering", ready)).toBe(false);
    expect(isGenerativeJobSettled(null, ready)).toBe(false);
  });

  it("test_a_render_stuck_past_the_ceiling_settles_instead_of_polling_forever", () => {
    // Without this bound a variant stranded in "rendering" on a variants_ready job
    // would poll every 2s forever with a live-ticking timer, and the 409 gate would
    // lock the user out of retrying until the ~60min server-side reconcile.
    const stale = [
      { render_status: "rendering", render_started_at: new Date(Date.now() - 31 * 60_000).toISOString() },
    ];
    expect(isGenerativeJobSettled("variants_ready", stale)).toBe(true);
    // Just inside the ceiling is still live.
    const fresh = [
      { render_status: "rendering", render_started_at: new Date(Date.now() - 29 * 60_000).toISOString() },
    ];
    expect(isGenerativeJobSettled("variants_ready", fresh)).toBe(false);
  });

  it("test_a_rendering_variant_with_no_or_bad_timestamp_counts_as_live", () => {
    // Just-dispatched, stamp not read back yet — must not settle early.
    expect(isGenerativeJobSettled("variants_ready", [{ render_status: "rendering" }])).toBe(false);
    expect(
      isGenerativeJobSettled("variants_ready", [
        { render_status: "rendering", render_started_at: "nonsense" },
      ]),
    ).toBe(false);
  });
});

describe("deriveReceiptText after a re-render", () => {
  it("test_started_after_finished_never_renders_a_negative_duration", () => {
    const finished = new Date(Date.now() - 35 * 60_000).toISOString();
    const started = new Date().toISOString(); // moved by the Save
    expect(deriveReceiptText(started, finished)).toBe(RECEIPT_FALLBACK);
  });

  it("test_first_render_still_reports_its_real_duration", () => {
    const started = new Date(Date.now() - 5 * 60_000).toISOString();
    const finished = new Date().toISOString();
    expect(deriveReceiptText(started, finished)).toBe("Ready in 5:00");
  });

  it("test_missing_or_malformed_timestamps_fall_back", () => {
    expect(deriveReceiptText(null, null)).toBe(RECEIPT_FALLBACK);
    expect(deriveReceiptText(undefined, new Date().toISOString())).toBe(RECEIPT_FALLBACK);
    expect(deriveReceiptText("nonsense", "nonsense")).toBe(RECEIPT_FALLBACK);
    expect(deriveReceiptText("nonsense", new Date().toISOString())).toBe(RECEIPT_FALLBACK);
  });

  it("test_a_zero_length_render_falls_back_instead_of_claiming_0_00", () => {
    // The guard is `ms <= 0`, not `< 0`: identical timestamps mean we never
    // observed a duration, so "Ready in 0:00" would be a fabricated one.
    const ts = new Date().toISOString();
    expect(deriveReceiptText(ts, ts)).toBe(RECEIPT_FALLBACK);
  });
});

describe("generative terminal-status sets", () => {
  it("test_success_and_failed_halves_partition_the_terminal_set", () => {
    // `GENERATIVE_TERMINAL_STATUSES` is composed from the two halves so a new
    // failure status can never be terminal-but-not-failed — which would make
    // `isGenerativeJobSettled` wait on a dead job's frozen "rendering" variant.
    expect([...GENERATIVE_TERMINAL_STATUSES].sort()).toEqual(
      [...GENERATIVE_SUCCESS_STATUSES, ...GENERATIVE_FAILED_STATUSES].sort(),
    );
    expect(
      GENERATIVE_SUCCESS_STATUSES.filter((s) => GENERATIVE_FAILED_STATUSES.includes(s)),
    ).toEqual([]);
    for (const status of GENERATIVE_TERMINAL_STATUSES) {
      // Every terminal status resolves through exactly one branch of the rule.
      expect(isGenerativeJobSettled(status, [{ render_status: "rendering" }])).toBe(
        GENERATIVE_FAILED_STATUSES.includes(status),
      );
    }
  });
});
