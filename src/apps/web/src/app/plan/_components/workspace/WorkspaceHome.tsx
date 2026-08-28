"use client";

/**
 * WorkspaceHome — the /plan home (v0.44 redesign).
 *
 * Design source: Paper file "Kria Plan Redesign", page "FINAL — Basic home"
 * (F1/F4). The page is openly the create-a-new-video surface: a basic create
 * block on top ("Make a new video." → /plan/new), with a PAST EDITS grid of
 * the user's rendered videos underneath (folds in the old /library page).
 * The ideas ledger is gone — items are created by the New-video flow.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  createCreatorWorkspaceRelevanceProposal,
  creatorWorkspaceUploadContentTypeForFile,
  requestCreatorWorkspaceUpload,
  uploadToGcs,
  type ContentPlan,
  type CreatorWorkspaceRelevanceProposal,
} from "@/lib/plan-api";
import {
  listMyJobs,
  refreshMyJobPosters,
  type LibraryJob,
  type PosterRefreshJob,
} from "@/lib/me-api";
import {
  libraryPosterRecoveryKey,
  libraryPosterRecoveryKeyForIdentity,
} from "@/lib/library-poster";
import type { TikTokConnection } from "@/lib/tiktok-api";
import LibraryTile from "@/components/library/LibraryTile";
import TikTokConnectionCard from "@/components/library/TikTokConnectionCard";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import SeedUploadCard from "../SeedUploadCard";
import { CreatorWorkspacePanel } from "./CreatorWorkspacePanel";

const CREATOR_WORKSPACE_UPLOADS_ENABLED =
  process.env.NEXT_PUBLIC_MAIN_CREATOR_AGENT_FREEFORM_UPLOADS_ENABLED === "true";

export const POSTER_RECOVERY_DELAYS_MS = [1_500, 3_000, 6_000, 12_000, 24_000] as const;
export const POSTER_REFRESH_TRANSPORT_DELAYS_MS = [1_500, 3_000, 6_000, 12_000, 24_000] as const;
export const POSTER_ERROR_REFRESH_DEBOUNCE_MS = 400;
export const POSTER_REFRESH_TIMEOUT_MS = 10_000;
export const POSTER_REFRESH_MAX_JOB_IDS = 200;

function needsPosterRecovery(
  job: LibraryJob,
  failedPosterKeys?: ReadonlySet<string>,
): boolean {
  return (
    job.status === "ready" &&
    job.poster_status !== "unavailable" &&
    (!job.poster_url || failedPosterKeys?.has(libraryPosterRecoveryKey(job)) === true)
  );
}

function mergeRefreshedPosterMetadata(
  current: LibraryJob[],
  refreshed: PosterRefreshJob[],
): LibraryJob[] {
  const refreshedById = new Map(refreshed.map((job) => [job.id, job]));
  return current.map((job) => {
    const poster = refreshedById.get(job.id);
    return poster
      ? {
          ...job,
          poster_url: poster.poster_url,
          poster_identity: poster.poster_identity,
          poster_status: poster.poster_status,
        }
      : job;
  });
}

interface PosterRecoveryState {
  posterAttempts: number;
  transportFailures: number;
  nextAttemptAt: number;
}

async function refreshPostersWithTimeout(
  jobIds: string[],
  controller: AbortController,
) {
  let timeoutId: number | null = null;
  try {
    return await Promise.race([
      refreshMyJobPosters(jobIds, controller.signal),
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

interface WorkspaceHomeProps {
  plan: ContentPlan;
  onRefresh: () => void | Promise<unknown>;
  onPlanChange: (plan: ContentPlan) => void;
  onError: (msg: string) => void;
}

export function WorkspaceHome({ plan, onRefresh, onError }: WorkspaceHomeProps) {
  const activating = ["seeding", "activating"].includes(plan.activation_status ?? "");
  const planGenerating = plan.plan_status === "generating";

  const [jobs, setJobs] = useState<LibraryJob[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">("loading");
  const [loadingMore, setLoadingMore] = useState(false);
  const [tiktokConnection, setTikTokConnection] = useState<TikTokConnection | null>(null);
  const [relevanceProposal, setRelevanceProposal] =
    useState<CreatorWorkspaceRelevanceProposal | null>(null);
  const [workspaceFiles, setWorkspaceFiles] = useState<File[]>([]);
  const [uploadingWorkspaceFootage, setUploadingWorkspaceFootage] = useState(false);
  const [workspaceUploadError, setWorkspaceUploadError] = useState<string | null>(null);
  const jobsRef = useRef<LibraryJob[]>([]);
  const posterRecoveryStateRef = useRef(new Map<string, PosterRecoveryState>());
  const failedPosterKeysRef = useRef(new Set<string>());
  const posterErrorRefreshKeysRef = useRef(new Set<string>());
  const pendingPosterErrorsRef = useRef(new Map<string, string>());
  const posterErrorRefreshDueAtRef = useRef<number | null>(null);
  const posterRecoveryInFlightRef = useRef(false);
  const posterRefreshAbortRef = useRef<AbortController | null>(null);
  const posterRefreshRequestIdRef = useRef(0);
  const mountedRef = useRef(true);
  const [posterRecoveryTick, setPosterRecoveryTick] = useState(0);

  jobsRef.current = jobs;

  useEffect(() => {
    // Do not let a proposal or retry from the previous plan leak into a newly
    // selected workspace. The child panel also fences its own polling by plan.
    setRelevanceProposal(null);
    setWorkspaceFiles([]);
    setWorkspaceUploadError(null);
  }, [plan.id]);

  const load = useCallback(async () => {
    setLoadState("loading");
    try {
      const page = await listMyJobs();
      setJobs(page.jobs);
      setCursor(page.next_cursor);
      setLoadState("ready");
    } catch {
      setLoadState("error");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      posterRefreshRequestIdRef.current += 1;
      posterRefreshAbortRef.current?.abort();
      posterRefreshAbortRef.current = null;
      posterRecoveryInFlightRef.current = false;
    };
  }, []);

  const posterRecoveryJobs = useMemo(
    () =>
      jobs.filter((job) =>
        needsPosterRecovery(job, failedPosterKeysRef.current),
      ),
    // The tick makes ref-backed image failures visible to this derived list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [jobs, posterRecoveryTick],
  );
  const exhaustedPosterJobIds = useMemo(
    () =>
      new Set(
        posterRecoveryJobs
          .filter(
            (job) =>
              (posterRecoveryStateRef.current.get(libraryPosterRecoveryKey(job))
                ?.posterAttempts ?? 0) >=
              POSTER_RECOVERY_DELAYS_MS.length,
          )
          .map((job) => job.id),
      ),
    // The tick makes ref-backed attempt changes visible without rebuilding the
    // entire jobs array after a failed background request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [posterRecoveryJobs, posterRecoveryTick],
  );
  const transportUnavailablePosterJobIds = useMemo(
    () =>
      new Set(
        posterRecoveryJobs
          .filter(
            (job) =>
              (posterRecoveryStateRef.current.get(libraryPosterRecoveryKey(job))
                ?.transportFailures ?? 0) >=
              POSTER_REFRESH_TRANSPORT_DELAYS_MS.length,
          )
          .map((job) => job.id),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [posterRecoveryJobs, posterRecoveryTick],
  );

  const posterRecoverySignature = posterRecoveryJobs
    .map(libraryPosterRecoveryKey)
    .sort()
    .join("|");

  useEffect(() => {
    if (posterRecoveryInFlightRef.current) return;

    const now = Date.now();
    const currentJobKeys = new Set(jobsRef.current.map(libraryPosterRecoveryKey));
    for (const key of failedPosterKeysRef.current) {
      if (!currentJobKeys.has(key)) failedPosterKeysRef.current.delete(key);
    }
    for (const key of posterErrorRefreshKeysRef.current) {
      if (!currentJobKeys.has(key)) posterErrorRefreshKeysRef.current.delete(key);
    }
    for (const key of pendingPosterErrorsRef.current.keys()) {
      if (!currentJobKeys.has(key)) pendingPosterErrorsRef.current.delete(key);
    }
    if (pendingPosterErrorsRef.current.size === 0) {
      posterErrorRefreshDueAtRef.current = null;
    }
    const activeRecoveryJobs = jobsRef.current.filter((job) =>
      needsPosterRecovery(job, failedPosterKeysRef.current),
    );
    const activeRecoveryKeys = new Set(
      activeRecoveryJobs.map(libraryPosterRecoveryKey),
    );
    for (const key of posterRecoveryStateRef.current.keys()) {
      if (!activeRecoveryKeys.has(key)) posterRecoveryStateRef.current.delete(key);
    }
    for (const job of activeRecoveryJobs) {
      const key = libraryPosterRecoveryKey(job);
      if (!posterRecoveryStateRef.current.has(key)) {
        posterRecoveryStateRef.current.set(key, {
          posterAttempts: 0,
          transportFailures: 0,
          nextAttemptAt: now + POSTER_RECOVERY_DELAYS_MS[0],
        });
      }
    }

    const nextRecoveryAt = Math.min(
      ...activeRecoveryJobs
        .map((job) =>
          posterRecoveryStateRef.current.get(libraryPosterRecoveryKey(job)),
        )
        .filter(
          (state): state is PosterRecoveryState =>
            Boolean(
              state &&
                state.posterAttempts < POSTER_RECOVERY_DELAYS_MS.length &&
                state.transportFailures <
                  POSTER_REFRESH_TRANSPORT_DELAYS_MS.length,
            ),
        )
        .map((state) => state.nextAttemptAt),
    );
    const nextErrorRefreshAt = posterErrorRefreshDueAtRef.current ?? Infinity;
    const nextRefreshAt = Math.min(nextRecoveryAt, nextErrorRefreshAt);
    if (!Number.isFinite(nextRefreshAt)) return;

    const timer = window.setTimeout(() => {
      if (posterRecoveryInFlightRef.current) return;
      const firedAt = Date.now();
      const targetJobIds = new Set<string>();
      const scheduledRecoveryKeys = new Map<string, string>();
      const duePosterErrorKeys = new Set<string>();

      if (
        posterErrorRefreshDueAtRef.current !== null &&
        posterErrorRefreshDueAtRef.current <= firedAt
      ) {
        for (const [key, jobId] of pendingPosterErrorsRef.current) {
          targetJobIds.add(jobId);
          duePosterErrorKeys.add(key);
        }
        posterErrorRefreshDueAtRef.current = null;
      }

      for (const job of jobsRef.current.filter((candidate) =>
        needsPosterRecovery(candidate, failedPosterKeysRef.current),
      )) {
        const key = libraryPosterRecoveryKey(job);
        const state = posterRecoveryStateRef.current.get(key);
        if (
          !state ||
          state.posterAttempts >= POSTER_RECOVERY_DELAYS_MS.length ||
          state.transportFailures >= POSTER_REFRESH_TRANSPORT_DELAYS_MS.length ||
          state.nextAttemptAt > firedAt
        ) {
          continue;
        }
        targetJobIds.add(job.id);
        scheduledRecoveryKeys.set(job.id, key);
      }

      if (targetJobIds.size === 0) {
        setPosterRecoveryTick((value) => value + 1);
        return;
      }

      posterRecoveryInFlightRef.current = true;
      setPosterRecoveryTick((value) => value + 1);

      const controller = new AbortController();
      const requestId = ++posterRefreshRequestIdRef.current;
      posterRefreshAbortRef.current = controller;
      const requestedIds = [...targetJobIds].slice(0, POSTER_REFRESH_MAX_JOB_IDS);
      const requestedIdSet = new Set(requestedIds);
      for (const key of duePosterErrorKeys) {
        const jobId = pendingPosterErrorsRef.current.get(key);
        if (!jobId || !requestedIdSet.has(jobId)) continue;
        pendingPosterErrorsRef.current.delete(key);
      }
      if (pendingPosterErrorsRef.current.size > 0) {
        posterErrorRefreshDueAtRef.current = firedAt;
      }
      const requestedJobsById = new Map(
        jobsRef.current.map((job) => [job.id, job]),
      );

      const recordTransportFailure = (jobId: string, failedAt: number) => {
        const currentJob = requestedJobsById.get(jobId);
        if (!currentJob) return;
        const key = libraryPosterRecoveryKey(currentJob);
        const state = posterRecoveryStateRef.current.get(key);
        if (!state) return;
        state.transportFailures += 1;
        state.nextAttemptAt =
          state.transportFailures < POSTER_REFRESH_TRANSPORT_DELAYS_MS.length
            ? failedAt +
              POSTER_REFRESH_TRANSPORT_DELAYS_MS[state.transportFailures]
            : Infinity;
      };

      void refreshPostersWithTimeout(requestedIds, controller)
        .then(({ jobs: refreshedJobs }) => {
          if (requestId !== posterRefreshRequestIdRef.current) return;
          const completedAt = Date.now();
          const refreshedById = new Map(
            refreshedJobs.map((job) => [job.id, job]),
          );
          const loadedById = new Map(
            jobsRef.current.map((job) => [job.id, job]),
          );

          for (const jobId of requestedIds) {
            const currentJob = loadedById.get(jobId);
            const refreshed = refreshedById.get(jobId);
            if (!currentJob || !refreshed) {
              recordTransportFailure(jobId, completedAt);
              continue;
            }

            const key = libraryPosterRecoveryKey(currentJob);
            const state = posterRecoveryStateRef.current.get(key);
            if (!state) continue;
            state.transportFailures = 0;

            const refreshedJob = {
              ...currentJob,
              poster_url: refreshed.poster_url,
              poster_identity: refreshed.poster_identity,
              poster_status: refreshed.poster_status,
            };
            if (
              scheduledRecoveryKeys.get(jobId) === key &&
              needsPosterRecovery(refreshedJob, failedPosterKeysRef.current)
            ) {
              state.posterAttempts += 1;
              state.nextAttemptAt =
                state.posterAttempts < POSTER_RECOVERY_DELAYS_MS.length
                  ? completedAt + POSTER_RECOVERY_DELAYS_MS[state.posterAttempts]
                  : Infinity;
            } else if (!needsPosterRecovery(refreshedJob, failedPosterKeysRef.current)) {
              state.nextAttemptAt = Infinity;
            }
          }

          setJobs((current) =>
            mergeRefreshedPosterMetadata(current, refreshedJobs),
          );
        })
        .catch(() => {
          if (requestId !== posterRefreshRequestIdRef.current) return;
          const failedAt = Date.now();
          for (const jobId of requestedIds) recordTransportFailure(jobId, failedAt);
        })
        .finally(() => {
          if (requestId !== posterRefreshRequestIdRef.current) return;
          if (posterRefreshAbortRef.current === controller) {
            posterRefreshAbortRef.current = null;
          }
          posterRecoveryInFlightRef.current = false;
          if (mountedRef.current) {
            setPosterRecoveryTick((value) => value + 1);
          }
        });
    }, Math.max(0, nextRefreshAt - now));

    return () => window.clearTimeout(timer);
  }, [posterRecoverySignature, posterRecoveryTick]);

  const handlePosterLoadError = useCallback(
    (jobId: string, posterIdentity: string | null) => {
      const key = posterIdentity !== null
        ? libraryPosterRecoveryKeyForIdentity(jobId, posterIdentity)
        : (() => {
            const currentJob = jobsRef.current.find((job) => job.id === jobId);
            return currentJob
              ? libraryPosterRecoveryKey(currentJob)
              : libraryPosterRecoveryKeyForIdentity(jobId, null);
          })();
      failedPosterKeysRef.current.add(key);
      if (posterErrorRefreshKeysRef.current.has(key)) return;
      posterErrorRefreshKeysRef.current.add(key);
      pendingPosterErrorsRef.current.set(key, jobId);
      posterErrorRefreshDueAtRef.current ??=
        Date.now() + POSTER_ERROR_REFRESH_DEBOUNCE_MS;
      setPosterRecoveryTick((value) => value + 1);
    },
    [],
  );

  const handlePosterLoadSuccess = useCallback(
    (jobId: string, posterIdentity: string | null) => {
      const key = posterIdentity !== null
        ? libraryPosterRecoveryKeyForIdentity(jobId, posterIdentity)
        : (() => {
            const currentJob = jobsRef.current.find((job) => job.id === jobId);
            return currentJob
              ? libraryPosterRecoveryKey(currentJob)
              : libraryPosterRecoveryKeyForIdentity(jobId, null);
          })();
      const removedFailure = failedPosterKeysRef.current.delete(key);
      const removedErrorRefresh = posterErrorRefreshKeysRef.current.delete(key);
      const removedRecovery = posterRecoveryStateRef.current.delete(key);
      const removedPendingError = pendingPosterErrorsRef.current.delete(key);
      const changed =
        removedFailure ||
        removedErrorRefresh ||
        removedRecovery ||
        removedPendingError;
      if (!changed) return;
      if (pendingPosterErrorsRef.current.size === 0) {
        posterErrorRefreshDueAtRef.current = null;
      }
      setPosterRecoveryTick((value) => value + 1);
    },
    [],
  );

  async function loadMore() {
    if (!cursor) return;
    const requestCursor = cursor;
    setLoadingMore(true);
    try {
      const page = await listMyJobs({ cursor: requestCursor });
      setJobs((prev) => [...prev, ...page.jobs]);
      setCursor(page.next_cursor);
    } catch {
      // leave the existing list
    } finally {
      setLoadingMore(false);
    }
  }

  const handleDeleted = useCallback((jobId: string) => {
    setJobs((current) => current.filter((job) => job.id !== jobId));
  }, []);

  const handleWorkspaceUpload = useCallback(async (files: FileList | File[] | null) => {
    if (!files || files.length === 0 || uploadingWorkspaceFootage) return;
    const selectedFiles = Array.from(files);
    if (selectedFiles.length > 10) {
      setWorkspaceFiles([]);
      setWorkspaceUploadError("Choose up to 10 video clips at a time.");
      return;
    }
    if (selectedFiles.some((file) => !isWorkspaceVideoFile(file))) {
      setWorkspaceFiles([]);
      setWorkspaceUploadError("Choose MP4, MOV, or AVI video clips.");
      return;
    }
    if (selectedFiles.some((file) => file.size > 4 * 1024 * 1024 * 1024)) {
      setWorkspaceFiles([]);
      setWorkspaceUploadError("Each clip must be 4 GB or smaller.");
      return;
    }
    setWorkspaceFiles(selectedFiles);
    setUploadingWorkspaceFootage(true);
    setWorkspaceUploadError(null);
    try {
      const jobIds: string[] = [];
      for (const file of selectedFiles) {
        const metadata = await readWorkspaceVideoMetadata(file);
        if (metadata.duration_s > 1800) {
          throw new Error("Video exceeds 30-minute limit");
        }
        const contentType = creatorWorkspaceUploadContentTypeForFile(file);
        const target = await requestCreatorWorkspaceUpload({
          filename: file.name,
          content_type: contentType,
          file_size_bytes: file.size,
          duration_s: metadata.duration_s,
          aspect_ratio: metadata.aspect_ratio,
        });
        // /uploads/presigned currently signs video/quicktime as video/mp4.
        // Pass the same header explicitly; generic uploadToGcs preserves the
        // exact MIME contracts used by other signed-upload routes.
        await uploadToGcs(target.upload_url, file, { "Content-Type": contentType });
        jobIds.push(target.job_id);
      }
      if (jobIds.length === 0) return;
      const proposal = await createCreatorWorkspaceRelevanceProposal(
        plan.id,
        jobIds,
        workspaceUploadId(),
      );
      setRelevanceProposal(proposal);
      setWorkspaceFiles([]);
    } catch {
      setWorkspaceUploadError(
        "We couldn’t prepare that footage for review. Check the file type and try again.",
      );
    } finally {
      setUploadingWorkspaceFootage(false);
    }
  }, [plan.id, uploadingWorkspaceFootage]);

  const handleProposalChange = useCallback((next: CreatorWorkspaceRelevanceProposal) => {
    if (next.plan_id !== plan.id) return;
    setRelevanceProposal(next);
  }, [plan.id]);

  const currentProposal = relevanceProposal?.plan_id === plan.id ? relevanceProposal : null;

  return (
    <div className="min-h-screen bg-background text-foreground">
      <div className="mx-auto flex max-w-[900px] flex-col gap-10 px-6 pb-24 pt-14">
        {activating && (
          <SeedUploadCard plan={plan} onError={onError} onRefresh={onRefresh} />
        )}

        {/* ---- Create block: the page's stated purpose ---- */}
        <section aria-labelledby="create-heading">
          <h1
            id="create-heading"
            className="font-display text-[32px] font-medium leading-tight text-[#0c0c0e]"
          >
            Create a new video
          </h1>
          <p className="mt-1.5 max-w-md text-sm text-[#71717a]">
            Choose a format, add your footage, and Kria will build a first cut.
          </p>
          <Button asChild variant="ink" size="lg" className="mt-5 w-full sm:w-auto">
            <Link href="/plan/new">Create a video</Link>
          </Button>
          {planGenerating && (
            <p className="mt-3 text-[13px] text-[#71717a]" role="status">
              Your content plan is still being prepared. You can create a video now.
            </p>
          )}
        </section>

        {CREATOR_WORKSPACE_UPLOADS_ENABLED && (
          <section aria-labelledby="workspace-upload-heading" className="rounded-2xl border border-zinc-200 bg-white p-4 text-[#0c0c0e] shadow-sm">
            <h2 id="workspace-upload-heading" className="text-sm font-semibold">Review footage against this plan</h2>
            <p id="workspace-upload-help" className="mt-1 text-sm text-zinc-500">Upload video clips for a relevance proposal. Nothing is added to the plan until you approve it.</p>
            <label className="mt-3 inline-flex min-h-11 cursor-pointer items-center rounded-md border border-zinc-300 bg-white px-3 text-sm font-medium text-[#0c0c0e] transition-colors hover:bg-zinc-50 focus-within:outline-none focus-within:ring-2 focus-within:ring-lime-500 focus-within:ring-offset-2">
              {uploadingWorkspaceFootage ? "Uploading…" : workspaceFiles.length > 0 ? `${workspaceFiles.length} clips selected` : "Choose video clips"}
              <input
                className="sr-only"
                type="file"
                accept="video/mp4,video/quicktime,video/x-msvideo"
                multiple
                disabled={uploadingWorkspaceFootage}
                aria-describedby="workspace-upload-help workspace-upload-status workspace-upload-error"
                onChange={(event) => {
                  void handleWorkspaceUpload(event.target.files);
                  event.currentTarget.value = "";
                }}
              />
            </label>
            {uploadingWorkspaceFootage && (
              <div id="workspace-upload-status" className="mt-3" role="status" aria-live="polite" aria-busy="true">
                <p className="text-sm text-zinc-600">Uploading footage…</p>
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-zinc-100" role="progressbar" aria-label="Uploading footage">
                  <div className="h-full w-1/3 rounded-full bg-lime-600 motion-safe:animate-shimmer" />
                </div>
              </div>
            )}
            {workspaceUploadError && (
              <div id="workspace-upload-error" className="mt-3 flex flex-wrap items-center gap-3 rounded-md border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-zinc-700" role="alert">
                <span>{workspaceUploadError}</span>
                {workspaceFiles.length > 0 && (
                  <Button type="button" size="sm" variant="outline" className="min-h-11" onClick={() => void handleWorkspaceUpload(workspaceFiles)} disabled={uploadingWorkspaceFootage}>
                    Retry upload
                  </Button>
                )}
              </div>
            )}
          </section>
        )}

        <CreatorWorkspacePanel
          planId={plan.id}
          proposal={currentProposal}
          onProposalChange={handleProposalChange}
        />

        {/* ---- Past edits ---- */}
        <section aria-labelledby="past-edits-heading">
          <h2
            id="past-edits-heading"
            className="mb-4 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#3f3f46]"
          >
            Your videos
          </h2>

          {loadState === "loading" && <SkeletonGrid />}

          {loadState === "error" && (
            <div className="py-10">
              <p className="text-[#3f3f46]">We couldn&apos;t load your videos. Your saved videos are still safe.</p>
              <Button variant="outline" size="sm" className="mt-4" onClick={() => void load()}>
                Load your videos again
              </Button>
            </div>
          )}

          {loadState === "ready" && jobs.length === 0 && (
            <div className="py-6">
              <p className="text-[15px] text-[#3f3f46]">Your finished videos will appear here.</p>
              <p className="mt-1 text-sm text-[#71717a]">Create your first video to get started.</p>
            </div>
          )}

          {loadState === "ready" && jobs.length > 0 && (
            <>
              <ul className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
                {jobs.map((job) => (
                  <li key={job.id}>
                    <LibraryTile
                      job={job}
                      onDeleted={handleDeleted}
                      onPosterLoadError={handlePosterLoadError}
                      onPosterLoadSuccess={handlePosterLoadSuccess}
                      posterRecoveryExhausted={exhaustedPosterJobIds.has(job.id)}
                      posterRefreshUnavailable={transportUnavailablePosterJobIds.has(
                        job.id,
                      )}
                    />
                  </li>
                ))}
              </ul>
              {cursor && (
                <div className="mt-8 text-center">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void loadMore()}
                    disabled={loadingMore}
                  >
                    {loadingMore ? "Loading videos…" : "Load more videos"}
                  </Button>
                </div>
              )}
            </>
          )}
        </section>

        {/* ---- Integrations (was /library's TikTok card; release rails link here) ----
            TikTokConnectionCard reports availability via onConnection (and
            renders null itself when unavailable); mount it unconditionally
            so that callback ever fires, but only show the "Integrations"
            heading once availability is confirmed — otherwise the page ends
            in an empty labeled section. */}
        <section id="tiktok" aria-labelledby="integrations-heading">
          {tiktokConnection?.available && (
            <h2
              id="integrations-heading"
              className="mb-4 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#a1a1aa]"
            >
              Connected accounts
            </h2>
          )}
          <TikTokConnectionCard onConnection={setTikTokConnection} />
        </section>
      </div>
    </div>
  );
}

async function readWorkspaceVideoMetadata(
  file: File,
): Promise<{ duration_s: number; aspect_ratio: "16:9" | "9:16" }> {
  const url = URL.createObjectURL(file);
  try {
    const metadata = await new Promise<{ duration: number; width: number; height: number }>(
      (resolve, reject) => {
        const video = document.createElement("video");
        video.preload = "metadata";
        video.onloadedmetadata = () => resolve({ duration: video.duration, width: video.videoWidth, height: video.videoHeight });
        video.onerror = () => reject(new Error("Unable to inspect video"));
        video.src = url;
      },
    );
    if (!Number.isFinite(metadata.duration) || metadata.duration <= 0 || metadata.width <= 0 || metadata.height <= 0) {
      throw new Error("Invalid video metadata");
    }
    return {
      duration_s: metadata.duration,
      aspect_ratio: metadata.width >= metadata.height ? "16:9" : "9:16",
    };
  } finally {
    URL.revokeObjectURL(url);
  }
}

function workspaceUploadId(): string {
  return `creator-workspace-upload-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`;
}

function isWorkspaceVideoFile(file: File): boolean {
  const contentType = creatorWorkspaceUploadContentTypeForFile(file);
  if (["video/mp4", "video/quicktime", "video/x-msvideo"].includes(contentType)) return true;
  return /\.(mp4|mov|avi)$/i.test(file.name);
}

function SkeletonGrid() {
  return (
    <ul
      className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4"
      aria-label="Loading your videos…"
    >
      {Array.from({ length: 4 }).map((_, i) => (
        <li key={i}>
          <Skeleton className="aspect-[9/16] rounded-xl border border-zinc-200 bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer" />
        </li>
      ))}
    </ul>
  );
}
