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
