"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { POLL_INTERVAL_MS } from "../components/progress/constants";

/** Hard ceiling on poll duration — matches useJobStream's 30-min cap. */
const DEFAULT_MAX_POLL_MS = 30 * 60 * 1000;

/**
 * Generic poll hook for job status.
 *
 * - Polls at intervalMs until isTerminal(data) returns true.
 * - Stops automatically after maxPollMs (default 30 min) as a safety cap so
 *   no server-side failure can spin the loop forever.
 * - D8: re-fetches immediately on visibilitychange (tab re-focus) so the UI
 *   catches up after the user was away.
 * - Stops polling on unmount and when terminal.
 *
 * @param fetcher       Async function that fetches the current status.
 * @param intervalMs    Poll cadence in ms (default: POLL_INTERVAL_MS = 2000).
 * @param isTerminal    Returns true when no further polling is needed.
 * @param maxPollMs     Hard ceiling on total poll duration (default: 30 min).
 */
export function usePolledJobStatus<T>(
  fetcher: () => Promise<T>,
  intervalMs: number = POLL_INTERVAL_MS,
  isTerminal: (data: T) => boolean,
  maxPollMs: number = DEFAULT_MAX_POLL_MS,
): { data: T | null; error: Error | null; refetch: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const isTerminalRef = useRef(isTerminal);
  const fetcherRef = useRef(fetcher);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const maxPollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  const intervalMsRef = useRef(intervalMs);

  // Keep refs fresh without triggering re-subscriptions.
  isTerminalRef.current = isTerminal;
  fetcherRef.current = fetcher;
  intervalMsRef.current = intervalMs;

  const doFetch = useCallback(async () => {
    try {
      const result = await fetcherRef.current();
      if (!mountedRef.current) return;
      setData(result);
      setError(null);
      // Stop polling if terminal.
      if (isTerminalRef.current(result) && timerRef.current != null) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      // Re-arm when a refetch shows NON-terminal data after polling stopped:
      // a post-terminal mutation (variant re-render / instant-edit commit)
      // flips a variant back to "rendering", and without this the UI would
      // stay blind to its completion until a tab refocus.
      if (!isTerminalRef.current(result) && timerRef.current == null) {
        timerRef.current = setInterval(() => void doFetchRef.current(), intervalMsRef.current);
      }
    } catch (e) {
      if (!mountedRef.current) return;
      setError(e instanceof Error ? e : new Error(String(e)));
      // Do NOT stop polling on transient error — let the interval re-arm.
    }
  }, []);
  const doFetchRef = useRef(doFetch);
  doFetchRef.current = doFetch;

  const refetch = useCallback(() => {
    void doFetch();
  }, [doFetch]);

  // Initial fetch + interval.
  useEffect(() => {
    mountedRef.current = true;
    void doFetch();
    timerRef.current = setInterval(() => void doFetch(), intervalMs);

    // Hard safety ceiling: stop polling after maxPollMs regardless of terminal
    // state.  Mirrors the 30-min cap in useJobStream.  Prevents a permanently
    // non-terminal server response (e.g. a frozen variant after a worker crash)
    // from spinning the loop forever.
    if (maxPollMs > 0) {
      maxPollTimerRef.current = setTimeout(() => {
        if (timerRef.current != null) {
          clearInterval(timerRef.current);
          timerRef.current = null;
        }
      }, maxPollMs);
    }

    return () => {
      mountedRef.current = false;
      if (timerRef.current != null) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      if (maxPollTimerRef.current != null) {
        clearTimeout(maxPollTimerRef.current);
        maxPollTimerRef.current = null;
      }
    };
    // Only depend on intervalMs/maxPollMs — fetcher/isTerminal are via refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, maxPollMs]);

  // D8: visibilitychange → immediate refetch when tab becomes visible.
  useEffect(() => {
    const handleVisibility = () => {
      if (document.visibilityState === "visible") {
        void doFetch();
      }
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => document.removeEventListener("visibilitychange", handleVisibility);
  }, [doFetch]);

  return { data, error, refetch };
}
