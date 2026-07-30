/**
 * usePolledJobStatus — interval lifecycle.
 *
 * Regression (found in instant-edit browser QA): once the job reached a
 * terminal status the interval was cleared permanently; a later `refetch()`
 * that showed NON-terminal data (a variant re-render / instant-edit commit)
 * did a single fetch and never re-armed, so the UI stayed blind to the
 * render's completion until a tab refocus.
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";

interface FakeStatus {
  terminal: boolean;
  seq: number;
}

describe("usePolledJobStatus re-arm", () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => jest.useRealTimers());

  it("stops polling at terminal, re-arms when a refetch goes non-terminal, then stops again", async () => {
    let next: FakeStatus = { terminal: true, seq: 1 };
    const fetcher = jest.fn(async () => next);

    const { result } = renderHook(() =>
      usePolledJobStatus<FakeStatus>(fetcher, 2000, (d) => d.terminal),
    );

    // Initial fetch hits terminal → interval cleared.
    await act(async () => {
      await Promise.resolve();
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    await act(async () => {
      jest.advanceTimersByTime(6000);
    });
    expect(fetcher).toHaveBeenCalledTimes(1); // no polling while terminal

    // A mutation flips the job back to rendering; the UI calls refetch().
    next = { terminal: false, seq: 2 };
    await act(async () => {
      result.current.refetch();
      await Promise.resolve();
    });
    expect(fetcher).toHaveBeenCalledTimes(2);

    // The interval must be re-armed: subsequent ticks poll again.
    await act(async () => {
      jest.advanceTimersByTime(2000);
      await Promise.resolve();
    });
    expect(fetcher).toHaveBeenCalledTimes(3);

    // Render completes → terminal again → polling stops.
    next = { terminal: true, seq: 3 };
    await act(async () => {
      jest.advanceTimersByTime(2000);
      await Promise.resolve();
    });
    const callsAtTerminal = fetcher.mock.calls.length;
    await act(async () => {
      jest.advanceTimersByTime(8000);
    });
    expect(fetcher).toHaveBeenCalledTimes(callsAtTerminal);
    expect(result.current.data?.seq).toBe(3);
  });
});

/**
 * The safety ceiling is armed ONCE at mount. The re-arm branch above restarts the
 * interval, so it has to restart the ceiling too — otherwise a payload that is
 * permanently non-terminal (a variant stranded in "rendering" after a worker
 * crash) polls every 2s forever, with a live-ticking clock, on a page the user
 * left open.
 */
describe("usePolledJobStatus max-poll ceiling", () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => jest.useRealTimers());

  /** Advance in interval-sized steps so no fetch is ever mid-flight when the
   *  ceiling fires (a resolution landing in that window legitimately re-arms). */
  async function step(ms: number) {
    await act(async () => {
      jest.advanceTimersByTime(ms);
      await Promise.resolve();
    });
  }

  it("a post-ceiling re-arm stops again after another maxPollMs", async () => {
    // 5000 is deliberately NOT a multiple of the 2000 interval, so the ceiling
    // fires between ticks with nothing in flight.
    const next = { terminal: false, seq: 1 };
    const fetcher = jest.fn(async () => next);
    const { result } = renderHook(() =>
      usePolledJobStatus(fetcher, 2000, (d) => d.terminal, 5000),
    );

    await step(0); // mount fetch
    await step(2000); // tick
    await step(2000); // tick
    await step(1500); // t=5500: the ceiling fired at 5000 and stopped the loop
    const callsAtCeiling = fetcher.mock.calls.length;
    await step(10_000);
    expect(fetcher).toHaveBeenCalledTimes(callsAtCeiling);

    // A tab refocus / manual refetch still shows non-terminal data → the interval
    // re-arms. Without the ceiling re-arm this loop would now run forever.
    await act(async () => {
      result.current.refetch();
      await Promise.resolve();
    });
    await step(2000);
    expect(fetcher.mock.calls.length).toBeGreaterThan(callsAtCeiling + 1);

    // …and the re-armed loop is capped again rather than polling forever.
    await step(2000);
    await step(1500);
    const callsAtSecondCeiling = fetcher.mock.calls.length;
    await step(30_000);
    expect(fetcher).toHaveBeenCalledTimes(callsAtSecondCeiling);
  });

  it("does not extend a still-pending ceiling when the interval re-arms", async () => {
    // Terminal at mount → interval cleared, mount ceiling still pending. The
    // re-arm must reuse that ceiling (`maxPollTimerRef == null` guard); arming a
    // fresh one would push the cap out on every re-arm.
    let next = { terminal: true, seq: 1 };
    const fetcher = jest.fn(async () => next);
    const { result } = renderHook(() =>
      usePolledJobStatus(fetcher, 1000, (d) => d.terminal, 5000),
    );

    await step(0); // mount fetch is terminal → interval cleared, ceiling pending
    await step(1000);
    await step(1000); // t=2000 — no polling while terminal
    next = { terminal: false, seq: 2 };
    await act(async () => {
      result.current.refetch();
      await Promise.resolve();
    });
    await step(1000); // t=3000 tick
    await step(1000); // t=4000 tick
    expect(fetcher.mock.calls.length).toBeGreaterThan(3);

    // The MOUNT ceiling still owns the cap: it fires at 5000. A re-arm that
    // overwrote it at t=2000 would instead fire at 7000, leaving a tick at 6000.
    await step(1200); // t=5200
    const callsAtCeiling = fetcher.mock.calls.length;
    await step(1500); // t=6700 — no tick may land here
    expect(fetcher).toHaveBeenCalledTimes(callsAtCeiling);
  });

  it("maxPollMs=0 opts out of the ceiling on the re-arm path too", async () => {
    let next = { terminal: true, seq: 1 };
    const fetcher = jest.fn(async () => next);
    const { result } = renderHook(() => usePolledJobStatus(fetcher, 2000, (d) => d.terminal, 0));

    await step(0);
    next = { terminal: false, seq: 2 };
    await act(async () => {
      result.current.refetch();
      await Promise.resolve();
    });
    const callsAtRearm = fetcher.mock.calls.length;
    await step(2000);
    await step(2000);
    // A ceiling armed with 0ms would fire immediately and kill the loop.
    expect(fetcher.mock.calls.length).toBeGreaterThan(callsAtRearm + 1);
  });
});
