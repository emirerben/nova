/**
 * Reusable job polling hook: poll an endpoint at interval, stop on terminal state.
 *
 * Extracted from template-jobs/[id]/page.tsx polling logic.
 */

import { useCallback, useEffect, useRef, useState } from "react";

const DEFAULT_INTERVAL_MS = 4000;
const DEFAULT_TIMEOUT_MS = 10 * 60 * 1000; // 10 min

interface UseJobPollerOptions<T> {
  /** Function that fetches the current job status. */
  fetchStatus: (jobId: string) => Promise<T>;
  /** Predicate that returns true when the job is in a terminal state. */
  isTerminal: (data: T) => boolean;
  /** Poll interval in ms. Default: 4000. */
  intervalMs?: number;
  /** Timeout in ms. Default: 10 min. */
  timeoutMs?: number;
  /** Called when polling times out. */
  onTimeout?: () => void;
}

interface UseJobPollerResult<T> {
  data: T | null;
  error: string | null;
  polling: boolean;
  /** Manually restart polling (e.g. after creating a new job). */
  restart: (newJobId: string) => void;
  /** Stop polling. */
  stop: () => void;
}

export function useJobPoller<T>(
  jobId: string | null,
  options: UseJobPollerOptions<T>,
): UseJobPollerResult<T> {
  const {
    fetchStatus,
    isTerminal,
    intervalMs = DEFAULT_INTERVAL_MS,
    timeoutMs = DEFAULT_TIMEOUT_MS,
    onTimeout,
  } = options;

  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startTimeRef = useRef(0);
  const activeJobIdRef = useRef<string | null>(null);

  const stopPolling = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    setPolling(false);
  }, []);

  const startPolling = useCallback(
    (id: string) => {
      stopPolling();
      activeJobIdRef.current = id;
      setData(null);
      setError(null);
      setPolling(true);
      startTimeRef.current = Date.now();

      async function poll() {
        if (Date.now() - startTimeRef.current > timeoutMs) {
          stopPolling();
          setError("Polling timed out. The worker may be down.");
          onTimeout?.();
          return;
        }
        try {
          const result = await fetchStatus(id);
          setData(result);
          if (isTerminal(result)) {
            stopPolling();
          }
        } catch (err) {
          setError(err instanceof Error ? err.message : "Failed to fetch status");
          stopPolling();
        }
      }

      // Immediately poll, then set interval
      poll();
      intervalRef.current = setInterval(poll, intervalMs);
    },
    [fetchStatus, isTerminal, intervalMs, timeoutMs, onTimeout, stopPolling],
  );

  // Start polling when jobId changes (including initial mount)
  useEffect(() => {
    if (jobId && jobId !== activeJobIdRef.current) {
      startPolling(jobId);
    } else if (!jobId) {
      stopPolling();
      activeJobIdRef.current = null;
      setData(null);
      setError(null);
    }
    return () => stopPolling();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  const restart = useCallback(
    (newJobId: string) => {
      startPolling(newJobId);
    },
    [startPolling],
  );

  return { data, error, polling, restart, stop: stopPolling };
}
