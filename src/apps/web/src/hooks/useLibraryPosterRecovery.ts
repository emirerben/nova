"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import {
  refreshMyJobPosters,
  type LibraryJob,
  type PosterRefreshJob,
} from "@/lib/me-api";
import {
  libraryPosterRecoveryKey,
  libraryPosterRecoveryKeyForIdentity,
} from "@/lib/library-poster";

export const POSTER_RECOVERY_DELAYS_MS = [
  1_500, 3_000, 6_000, 12_000, 24_000, 60_000, 120_000,
] as const;
export const POSTER_REFRESH_TRANSPORT_DELAYS_MS = [
  1_500, 3_000, 6_000, 12_000, 24_000,
] as const;
export const POSTER_ERROR_REFRESH_DEBOUNCE_MS = 400;
export const POSTER_REFRESH_TIMEOUT_MS = 10_000;
const POSTER_REFRESH_MAX_JOB_IDS = 200;

interface RecoveryState {
  posterAttempts: number;
  transportFailures: number;
  nextAttemptAt: number;
}

function needsRecovery(
  job: LibraryJob,
  failedKeys: ReadonlySet<string>,
): boolean {
  return (
    job.status === "ready" &&
    job.poster_status !== "unavailable" &&
    (!job.poster_url || failedKeys.has(libraryPosterRecoveryKey(job)))
  );
}

function mergePosterMetadata(
  current: LibraryJob[],
  refreshed: PosterRefreshJob[],
): LibraryJob[] {
  const byId = new Map(refreshed.map((job) => [job.id, job]));
  return current.map((job) => {
    const poster = byId.get(job.id);
    return poster ? { ...job, ...poster } : job;
  });
}

async function refreshWithTimeout(
  jobIds: string[],
  controller: AbortController,
  brokenJobIds: string[],
) {
  let timeoutId: number | null = null;
  try {
    return await Promise.race([
      refreshMyJobPosters(jobIds, controller.signal, brokenJobIds),
      new Promise<never>((_, reject) => {
        timeoutId = window.setTimeout(() => {
          controller.abort();
          reject(new Error("Poster refresh timed out"));
        }, POSTER_REFRESH_TIMEOUT_MS);
      }),
    ]);
  } finally {
    if (timeoutId !== null) window.clearTimeout(timeoutId);
  }
}

/**
 * Preserves the bounded, server-backed poster repair that used to live in the
 * legacy plan home. The Gallery owns only presentation; Job remains the source
 * of truth and this hook merely reconciles newly signed poster metadata.
 */
export function useLibraryPosterRecovery({
  enabled,
  jobs,
  setJobs,
}: {
  enabled: boolean;
  jobs: LibraryJob[];
  setJobs: Dispatch<SetStateAction<LibraryJob[]>>;
}) {
  const jobsRef = useRef(jobs);
  const statesRef = useRef(new Map<string, RecoveryState>());
  const failedKeysRef = useRef(new Set<string>());
  const pendingBrokenRef = useRef(new Map<string, string>());
  const brokenRefreshDueAtRef = useRef<number | null>(null);
  const requestIdRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const inFlightRef = useRef(false);
  const [tick, setTick] = useState(0);
  jobsRef.current = jobs;

  useEffect(
    () => () => {
      requestIdRef.current += 1;
      abortRef.current?.abort();
      abortRef.current = null;
      inFlightRef.current = false;
    },
    [],
  );

  const recoveryJobs = useMemo(
    () => jobs.filter((job) => needsRecovery(job, failedKeysRef.current)),
    // Ref-backed image failures intentionally use tick as their render signal.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [jobs, tick],
  );
  const exhaustedJobIds = useMemo(
    () =>
      new Set(
        recoveryJobs
          .filter(
            (job) =>
              (statesRef.current.get(libraryPosterRecoveryKey(job))
                ?.posterAttempts ?? 0) >= POSTER_RECOVERY_DELAYS_MS.length,
          )
          .map((job) => job.id),
      ),
    [recoveryJobs],
  );
  const refreshUnavailableJobIds = useMemo(
    () =>
      new Set(
        recoveryJobs
          .filter(
            (job) =>
              (statesRef.current.get(libraryPosterRecoveryKey(job))
                ?.transportFailures ?? 0) >=
              POSTER_REFRESH_TRANSPORT_DELAYS_MS.length,
          )
          .map((job) => job.id),
      ),
    [recoveryJobs],
  );
  const signature = recoveryJobs.map(libraryPosterRecoveryKey).sort().join("|");

  useEffect(() => {
    if (!enabled || inFlightRef.current) return;
    const now = Date.now();
    const activeJobs = jobsRef.current.filter((job) =>
      needsRecovery(job, failedKeysRef.current),
    );
    const activeKeys = new Set(activeJobs.map(libraryPosterRecoveryKey));

    for (const key of statesRef.current.keys()) {
      if (!activeKeys.has(key)) statesRef.current.delete(key);
    }
    for (const key of failedKeysRef.current) {
      if (
        !jobsRef.current.some((job) => libraryPosterRecoveryKey(job) === key)
      ) {
        failedKeysRef.current.delete(key);
        pendingBrokenRef.current.delete(key);
      }
    }
    for (const job of activeJobs) {
      const key = libraryPosterRecoveryKey(job);
      if (!statesRef.current.has(key)) {
        statesRef.current.set(key, {
          posterAttempts: 0,
          transportFailures: 0,
          nextAttemptAt: now + POSTER_RECOVERY_DELAYS_MS[0],
        });
      }
    }

    const nextScheduledAt = Math.min(
      ...activeJobs.flatMap((job) => {
        const state = statesRef.current.get(libraryPosterRecoveryKey(job));
        return state &&
          state.posterAttempts < POSTER_RECOVERY_DELAYS_MS.length &&
          state.transportFailures < POSTER_REFRESH_TRANSPORT_DELAYS_MS.length
          ? [state.nextAttemptAt]
          : [];
      }),
      brokenRefreshDueAtRef.current ?? Infinity,
    );
    if (!Number.isFinite(nextScheduledAt)) return;

    const timer = window.setTimeout(
      () => {
        if (inFlightRef.current) return;
        const firedAt = Date.now();
        const requested = new Set<string>();
        const scheduledKeys = new Map<string, string>();

        if (
          brokenRefreshDueAtRef.current !== null &&
          brokenRefreshDueAtRef.current <= firedAt
        ) {
          for (const jobId of pendingBrokenRef.current.values())
            requested.add(jobId);
          brokenRefreshDueAtRef.current = null;
        }
        for (const job of jobsRef.current.filter((candidate) =>
          needsRecovery(candidate, failedKeysRef.current),
        )) {
          const key = libraryPosterRecoveryKey(job);
          const state = statesRef.current.get(key);
          if (
            !state ||
            state.posterAttempts >= POSTER_RECOVERY_DELAYS_MS.length ||
            state.transportFailures >=
              POSTER_REFRESH_TRANSPORT_DELAYS_MS.length ||
            state.nextAttemptAt > firedAt
          )
            continue;
          requested.add(job.id);
          scheduledKeys.set(job.id, key);
        }

        const requestedIds = [...requested].slice(
          0,
          POSTER_REFRESH_MAX_JOB_IDS,
        );
        if (requestedIds.length === 0) {
          setTick((value) => value + 1);
          return;
        }
        const requestedSet = new Set(requestedIds);
        for (const [key, jobId] of pendingBrokenRef.current) {
          if (requestedSet.has(jobId)) pendingBrokenRef.current.delete(key);
        }
        if (pendingBrokenRef.current.size > 0)
          brokenRefreshDueAtRef.current = firedAt;

        const requestedJobs = new Map(
          jobsRef.current.map((job) => [job.id, job]),
        );
        const brokenIds = requestedIds.filter((jobId) => {
          const job = requestedJobs.get(jobId);
          return Boolean(
            job?.poster_url &&
            failedKeysRef.current.has(libraryPosterRecoveryKey(job)),
          );
        });
        const recordTransportFailure = (jobId: string, failedAt: number) => {
          const job = requestedJobs.get(jobId);
          if (!job) return;
          const state = statesRef.current.get(libraryPosterRecoveryKey(job));
          if (!state) return;
          state.transportFailures += 1;
          state.nextAttemptAt =
            state.transportFailures < POSTER_REFRESH_TRANSPORT_DELAYS_MS.length
              ? failedAt +
                POSTER_REFRESH_TRANSPORT_DELAYS_MS[state.transportFailures]
              : Infinity;
        };

        inFlightRef.current = true;
        const controller = new AbortController();
        const requestId = ++requestIdRef.current;
        abortRef.current = controller;
        void refreshWithTimeout(requestedIds, controller, brokenIds)
          .then(({ jobs: refreshed }) => {
            if (requestId !== requestIdRef.current) return;
            const completedAt = Date.now();
            const refreshedById = new Map(
              refreshed.map((job) => [job.id, job]),
            );
            for (const jobId of requestedIds) {
              const current = requestedJobs.get(jobId);
              const poster = refreshedById.get(jobId);
              if (!current || !poster) {
                recordTransportFailure(jobId, completedAt);
                continue;
              }
              const key = libraryPosterRecoveryKey(current);
              const state = statesRef.current.get(key);
              if (!state) continue;
              state.transportFailures = 0;
              const next = { ...current, ...poster };
              if (
                scheduledKeys.get(jobId) === key &&
                needsRecovery(next, failedKeysRef.current)
              ) {
                state.posterAttempts += 1;
                state.nextAttemptAt =
                  state.posterAttempts < POSTER_RECOVERY_DELAYS_MS.length
                    ? completedAt +
                      POSTER_RECOVERY_DELAYS_MS[state.posterAttempts]
                    : Infinity;
              } else if (!needsRecovery(next, failedKeysRef.current)) {
                state.nextAttemptAt = Infinity;
              }
            }
            setJobs((current) => mergePosterMetadata(current, refreshed));
          })
          .catch(() => {
            if (requestId !== requestIdRef.current) return;
            const failedAt = Date.now();
            for (const jobId of requestedIds)
              recordTransportFailure(jobId, failedAt);
          })
          .finally(() => {
            if (requestId !== requestIdRef.current) return;
            if (abortRef.current === controller) abortRef.current = null;
            inFlightRef.current = false;
            setTick((value) => value + 1);
          });
      },
      Math.max(0, nextScheduledAt - now),
    );

    return () => window.clearTimeout(timer);
  }, [enabled, setJobs, signature, tick]);

  const posterKey = useCallback((jobId: string, identity: string | null) => {
    if (identity !== null)
      return libraryPosterRecoveryKeyForIdentity(jobId, identity);
    const current = jobsRef.current.find((job) => job.id === jobId);
    return current
      ? libraryPosterRecoveryKey(current)
      : libraryPosterRecoveryKeyForIdentity(jobId, null);
  }, []);

  const onPosterLoadError = useCallback(
    (jobId: string, identity: string | null) => {
      const key = posterKey(jobId, identity);
      failedKeysRef.current.add(key);
      pendingBrokenRef.current.set(key, jobId);
      brokenRefreshDueAtRef.current ??=
        Date.now() + POSTER_ERROR_REFRESH_DEBOUNCE_MS;
      setTick((value) => value + 1);
    },
    [posterKey],
  );

  const onPosterLoadSuccess = useCallback(
    (jobId: string, identity: string | null) => {
      const key = posterKey(jobId, identity);
      const removedFailure = failedKeysRef.current.delete(key);
      const removedPending = pendingBrokenRef.current.delete(key);
      const removedState = statesRef.current.delete(key);
      const changed = removedFailure || removedPending || removedState;
      if (!changed) return;
      if (pendingBrokenRef.current.size === 0)
        brokenRefreshDueAtRef.current = null;
      setTick((value) => value + 1);
    },
    [posterKey],
  );

  return {
    exhaustedJobIds,
    onPosterLoadError,
    onPosterLoadSuccess,
    refreshUnavailableJobIds,
  };
}
