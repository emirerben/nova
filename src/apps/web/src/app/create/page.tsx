"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useSession } from "next-auth/react";
import { useCallback, useEffect, useRef, useState } from "react";

import { VoiceRecorder } from "@/app/generative/VoiceRecorder";
import SignInPrompt from "@/app/plan/_components/SignInPrompt";
import { ProgressTheater } from "@/components/progress";
import { deriveReceiptText } from "@/components/progress/logic";
import { Eyebrow } from "@/components/ui/Eyebrow";
import { LightShell } from "@/components/ui/LightShell";
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";
import {
  createOwnedGenerativeJob,
  GENERATIVE_FAILED_STATUSES,
  GENERATIVE_SUCCESS_STATUSES,
  getOwnedGenerativeJobStatus,
  isGenerativeJobSettled,
  openGenerativeJobInEditor,
  retryOwnedGenerativeJob,
  uploadOwnedGenerativeClip,
  uploadOwnedVoiceover,
  type GenerativeJobStatus,
} from "@/lib/generative-api";
import { GENERATIVE_PHASE_LABEL, GENERATIVE_PHASE_ORDER } from "@/lib/job-phases";
import { jobFailureCopy } from "@/lib/job-failure-copy";

const MAX_GENERATIVE_CLIPS = 20;
const CREATE_DRAFT_STORAGE_VERSION = 1;

type UploadedClip = {
  gcsPath: string;
  kind: "video" | "image";
  name: string;
  order: number;
};

type PendingClip = { file: File; order: number };

type StoredCreateDraft = {
  version: typeof CREATE_DRAFT_STORAGE_VERSION;
  uploads: UploadedClip[];
  direction: string;
  voiceoverPath: string | null;
  jobId: string | null;
};

function createDraftStorageKey(userId: string): string {
  return `kria:create-draft:v${CREATE_DRAFT_STORAGE_VERSION}:${userId}`;
}

function readStoredCreateDraft(key: string): StoredCreateDraft | null {
  try {
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredCreateDraft>;
    if (
      parsed.version !== CREATE_DRAFT_STORAGE_VERSION ||
      !Array.isArray(parsed.uploads) ||
      typeof parsed.direction !== "string" ||
      !(typeof parsed.jobId === "string" || parsed.jobId === null) ||
      !(typeof parsed.voiceoverPath === "string" || parsed.voiceoverPath === null)
    ) {
      return null;
    }
    const uploads = parsed.uploads.filter(
      (upload): upload is UploadedClip =>
        typeof upload?.gcsPath === "string" &&
        (upload?.kind === "video" || upload?.kind === "image") &&
        typeof upload?.name === "string" &&
        Number.isFinite(upload?.order),
    );
    return {
      version: CREATE_DRAFT_STORAGE_VERSION,
      uploads,
      direction: parsed.direction,
      voiceoverPath: parsed.voiceoverPath,
      jobId: parsed.jobId,
    };
  } catch {
    return null;
  }
}

export default function CreatePage() {
  const creationHubEnabled =
    process.env.NEXT_PUBLIC_CREATION_HUB_ENABLED === "true";
  const router = useRouter();
  const { status: authStatus, data: session } = useSession();
  const [uploads, setUploads] = useState<UploadedClip[]>([]);
  const [failedUploads, setFailedUploads] = useState<PendingClip[]>([]);
  const [direction, setDirection] = useState("");
  const [voiceoverPath, setVoiceoverPath] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [draftHydrated, setDraftHydrated] = useState(false);
  const submitInFlightRef = useRef(false);
  const nextOrderRef = useRef(0);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!creationHubEnabled) router.replace("/plan");
  }, [creationHubEnabled, router]);

  const userId = (session?.user as { id?: string } | undefined)?.id;

  useEffect(() => {
    if (authStatus !== "authenticated" || !userId) return;
    const existingJob = new URLSearchParams(window.location.search).get("job");
    const stored = readStoredCreateDraft(createDraftStorageKey(userId));
    const storedMatchesJob = !existingJob || stored?.jobId === existingJob;
    const restoredUploads = storedMatchesJob ? (stored?.uploads ?? []) : [];
    setUploads(restoredUploads);
    setDirection(storedMatchesJob ? (stored?.direction ?? "") : "");
    setVoiceoverPath(storedMatchesJob ? (stored?.voiceoverPath ?? null) : null);
    setJobId(existingJob);
    nextOrderRef.current =
      restoredUploads.reduce((highest, upload) => Math.max(highest, upload.order), -1) + 1;
    setDraftHydrated(true);
  }, [authStatus, userId]);

  useEffect(() => {
    if (!draftHydrated || !userId) return;
    const draft: StoredCreateDraft = {
      version: CREATE_DRAFT_STORAGE_VERSION,
      uploads,
      direction,
      voiceoverPath,
      jobId,
    };
    window.sessionStorage.setItem(createDraftStorageKey(userId), JSON.stringify(draft));
  }, [direction, draftHydrated, jobId, uploads, userId, voiceoverPath]);

  const handleFiles = useCallback(
    async (files: FileList | readonly File[] | readonly PendingClip[] | null) => {
      if (!files || files.length === 0) return;
      const incoming = Array.from(files as ArrayLike<File | PendingClip>);
      if (uploads.length + incoming.length > MAX_GENERATIVE_CLIPS) {
        setError(`You can upload up to ${MAX_GENERATIVE_CLIPS} videos or photos.`);
        return;
      }
      const selected = incoming.map((entry) =>
        entry instanceof File
          ? { file: entry, order: nextOrderRef.current++ }
          : entry,
      );
      setUploading(true);
      setError(null);
      setFailedUploads([]);
      try {
        const settled = await Promise.allSettled(
          selected.map(async ({ file, order }) => {
            const result = await uploadOwnedGenerativeClip(file);
            const completed = {
              gcsPath: result.gcs_path,
              kind: result.kind,
              name: file.name,
              order,
            };
            setUploads((previous) => [...previous, completed]);
            return completed;
          }),
        );
        const failed = settled.flatMap((result, index) =>
          result.status === "rejected" ? [selected[index]] : [],
        );
        setFailedUploads(failed);
        if (failed.length > 0) {
          const firstError = settled.find(
            (result): result is PromiseRejectedResult => result.status === "rejected",
          );
          const reason =
            firstError?.reason instanceof Error ? ` ${firstError.reason.message}` : "";
          const completedCount = settled.length - failed.length;
          setError(
            `${completedCount} uploaded · ${failed.length} didn’t upload.${reason}`,
          );
        }
      } finally {
        setUploading(false);
      }
    },
    [uploads.length],
  );

  const handleGenerate = useCallback(async () => {
    if (uploads.length === 0 || uploading || submitting || submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const orderedPaths = [...uploads]
        .sort((a, b) => a.order - b.order)
        .map((upload) => upload.gcsPath);
      const cleanDirection = direction.trim();
      const response = await createOwnedGenerativeJob(orderedPaths, voiceoverPath, {
        intent: cleanDirection || undefined,
      });
      setJobId(response.job_id);
      const url = new URL(window.location.href);
      url.searchParams.set("job", response.job_id);
      window.history.replaceState(null, "", `${url.pathname}${url.search}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Kria couldn’t start your edit.");
    } finally {
      submitInFlightRef.current = false;
      setSubmitting(false);
    }
  }, [direction, submitting, uploading, uploads, voiceoverPath]);

  if (!creationHubEnabled) return null;

  if (authStatus === "loading") {
    return (
      <LightShell size="narrow">
        <div className="motion-safe:animate-pulse space-y-5 py-10" aria-label="Loading create">
          <div className="h-3 w-24 rounded bg-zinc-200" />
          <div className="h-12 w-3/4 rounded bg-zinc-200" />
          <div className="h-40 rounded-xl bg-zinc-100" />
        </div>
      </LightShell>
    );
  }

  if (authStatus === "unauthenticated") {
    return (
      <LightShell size="narrow">
        <SignInPrompt
          callbackUrl="/create"
          title="Sign in to make your video"
          subtitle="Your footage, edit, and drafts stay connected to your Kria account."
        />
      </LightShell>
    );
  }

  return (
    <LightShell size="narrow" className="text-[#0c0c0e]">
      {!jobId ? (
        <>
          <div className="mb-9">
            <Eyebrow tone="lime" className="mb-3">
              Create with Kria
            </Eyebrow>
            <h1 className="font-display text-4xl leading-tight tracking-[-0.02em] text-[#0c0c0e] sm:text-5xl">
              Start with your footage.
            </h1>
            <p className="mt-4 max-w-xl leading-7 text-[#71717a]">
              Add the clips and photos you want to use. Kria will build the first cut,
              then open it in the editor for you.
            </p>
          </div>

          {error && (
            <div
              role="status"
              aria-live="polite"
              className="mb-5 rounded-lg border border-zinc-200 bg-white px-4 py-3 text-sm text-[#3f3f46]"
            >
              {error}
            </div>
          )}

          <section aria-labelledby="footage-heading">
            <div className="mb-3 flex items-baseline justify-between gap-4">
              <h2 id="footage-heading" className="text-sm font-semibold text-[#0c0c0e]">
                Footage
              </h2>
              <span className="text-xs text-[#a1a1aa]">Videos and photos · up to 20</span>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              accept="video/*,image/*"
              multiple
              disabled={uploading}
              aria-label="Upload footage"
              className="sr-only"
              onChange={(event) => {
                void handleFiles(event.target.files);
                event.target.value = "";
              }}
            />
            <button
              type="button"
              disabled={uploading}
              onClick={() => fileInputRef.current?.click()}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault();
                void handleFiles(event.dataTransfer.files);
              }}
              className="group flex min-h-[172px] w-full flex-col items-center justify-center rounded-xl border border-dashed border-zinc-300 bg-white px-6 py-8 text-center transition-colors hover:border-zinc-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0c0c0e] disabled:cursor-wait disabled:opacity-60"
            >
              <span
                aria-hidden
                className="mb-4 flex h-11 w-11 items-center justify-center rounded-full bg-[#0c0c0e] text-white"
              >
                <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8">
                  <path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5" strokeLinecap="round" strokeLinejoin="round" />
                  <path d="M5 14v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4" strokeLinecap="round" />
                </svg>
              </span>
              <span className="text-sm font-semibold text-[#0c0c0e]">
                {uploading ? "Uploading your footage…" : "Choose footage"}
              </span>
              <span className="mt-1 text-sm text-[#71717a]">
                or drop files here
              </span>
            </button>

            {uploads.length > 0 && (
              <ul className="mt-4 divide-y divide-zinc-100 rounded-xl border border-zinc-200 bg-white px-4">
                {[...uploads]
                  .sort((a, b) => a.order - b.order)
                  .map((upload) => (
                    <li key={upload.order} className="flex min-h-[52px] items-center gap-3 py-2.5">
                      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded bg-zinc-100 text-[10px] font-semibold uppercase text-[#71717a]">
                        {upload.kind === "video" ? "VID" : "IMG"}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-sm text-[#3f3f46]">
                        {upload.name}
                      </span>
                      <button
                        type="button"
                        aria-label={`Remove ${upload.name}`}
                        onClick={() =>
                          setUploads((current) =>
                            current.filter((item) => item.order !== upload.order),
                          )
                        }
                        className="inline-flex min-h-[44px] min-w-[44px] items-center justify-center rounded-full text-lg text-[#a1a1aa] hover:bg-zinc-100 hover:text-[#0c0c0e]"
                      >
                        ×
                      </button>
                    </li>
                  ))}
              </ul>
            )}

            {failedUploads.length > 0 && (
              <div className="mt-4 flex flex-col items-start gap-2 text-sm text-[#71717a] sm:flex-row sm:items-center sm:justify-between">
                <span>Couldn&apos;t upload {failedUploads.map(({ file }) => file.name).join(", ")}</span>
                <button
                  type="button"
                  onClick={() => void handleFiles(failedUploads)}
                  className="min-h-[44px] rounded-full border border-zinc-300 px-4 py-2 font-medium text-[#3f3f46] hover:border-zinc-500"
                >
                  Retry failed files
                </button>
              </div>
            )}
          </section>

          <section className="mt-8 border-t border-zinc-200 pt-7">
            <label htmlFor="create-direction" className="text-sm font-semibold text-[#0c0c0e]">
              Direction for Kria <span className="font-normal text-[#a1a1aa]">(optional)</span>
            </label>
            <p className="mt-1 text-sm text-[#71717a]">
              Tell Kria what the moment is about or what viewers should feel.
            </p>
            <textarea
              id="create-direction"
              value={direction}
              onChange={(event) => setDirection(event.target.value)}
              maxLength={1000}
              rows={3}
              placeholder="For example: Make it feel warm and spontaneous. Lead with the reaction shot."
              className="mt-3 w-full resize-y rounded-xl border border-zinc-200 bg-white px-4 py-3 text-base text-[#0c0c0e] outline-none placeholder:text-[#a1a1aa] focus:border-zinc-500 focus:ring-1 focus:ring-zinc-500"
            />
          </section>

          <details className="mt-6 border-t border-zinc-200 pt-6">
            <summary className="flex min-h-[44px] cursor-pointer list-none items-center justify-between text-sm font-semibold text-[#3f3f46] marker:content-none">
              <span>
                Add a final video voiceover <span className="font-normal text-[#a1a1aa]">(optional)</span>
              </span>
              <span aria-hidden className="text-lg font-normal text-[#a1a1aa]">+</span>
            </summary>
            <p className="mb-3 text-sm leading-6 text-[#71717a]">
              This audio will be used in the finished video. Kria will cut the footage to your voice.
            </p>
            <VoiceRecorder
              onVoiceover={setVoiceoverPath}
              upload={uploadOwnedVoiceover}
            />
          </details>

          <div className="sticky bottom-0 -mx-6 mt-8 border-t border-zinc-200 bg-[#ffffff]/95 px-6 pb-[max(1.5rem,env(safe-area-inset-bottom))] pt-4 backdrop-blur">
            <button
              type="button"
              onClick={() => void handleGenerate()}
              disabled={uploads.length === 0 || uploading || submitting}
              className="inline-flex min-h-[48px] w-full items-center justify-center rounded-full bg-[#0c0c0e] px-7 py-3 text-[15px] font-semibold text-white transition-opacity hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0c0c0e] disabled:cursor-not-allowed disabled:opacity-40"
            >
              {submitting ? "Starting your edit…" : "Make my first cut"}
            </button>
            <p className="mt-2 text-center text-xs text-[#a1a1aa]">
              Your uploads and choices stay here if you need to retry.
            </p>
          </div>
        </>
      ) : (
        <CreationProgress
          jobId={jobId}
          direction={direction}
          hasRetryInputs={uploads.length > 0}
          onStartOver={() => {
            setJobId(null);
            setError(null);
            window.history.replaceState(null, "", "/create");
          }}
        />
      )}
    </LightShell>
  );
}

function CreationProgress({
  jobId,
  direction,
  hasRetryInputs,
  onStartOver,
}: {
  jobId: string;
  direction: string;
  hasRetryInputs: boolean;
  onStartOver: () => void;
}) {
  const router = useRouter();
  const [promotionError, setPromotionError] = useState<string | null>(null);
  const [openingEditor, setOpeningEditor] = useState(false);
  const [retryingRender, setRetryingRender] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const promotionAttemptRef = useRef<string | null>(null);

  const fetcher = useCallback(
    () => getOwnedGenerativeJobStatus(jobId),
    [jobId],
  );
  const isTerminal = useCallback(
    (data: GenerativeJobStatus) => {
      const settled = isGenerativeJobSettled(data.status, data.variants);
      if (!settled) return false;
      return !GENERATIVE_SUCCESS_STATUSES.includes(data.status) || data.finished_at != null;
    },
    [],
  );
  const { data: status, error: pollError, refetch } =
    usePolledJobStatus<GenerativeJobStatus>(fetcher, undefined, isTerminal);

  const openEditor = useCallback(async () => {
    if (promotionAttemptRef.current === jobId) return;
    promotionAttemptRef.current = jobId;
    setOpeningEditor(true);
    setPromotionError(null);
    try {
      const title = direction.trim().slice(0, 160) || undefined;
      const result = await openGenerativeJobInEditor(jobId, title);
      router.replace(
        `/plan/items/${encodeURIComponent(result.plan_item_id)}/edit?variant=${encodeURIComponent(result.variant_id)}`,
      );
    } catch (cause) {
      setOpeningEditor(false);
      setPromotionError(
        cause instanceof Error
          ? cause.message
          : "Your first cut is ready, but the editor could not open.",
      );
    }
  }, [direction, jobId, router]);

  const hasReadyVariant =
    status?.variants.some(
      (variant) => variant.render_status === "ready",
    ) ?? false;
  const readyToPromote =
    status != null &&
    hasReadyVariant;

  useEffect(() => {
    if (readyToPromote) void openEditor();
  }, [openEditor, readyToPromote]);

  const failed = status != null && GENERATIVE_FAILED_STATUSES.includes(status.status);
  const failedVariantClass = status?.variants
    .filter((variant) => variant.render_status === "failed" && variant.error_class)
    .sort((a, b) => a.rank - b.rank)[0]?.error_class;
  const failureCopy = jobFailureCopy(
    failedVariantClass ?? status?.failure_reason ?? (failed ? status?.status : null),
  );
  const canRetryRender = failureCopy.action !== "review_media";

  const retryRender = useCallback(async () => {
    if (retryingRender) return;
    setRetryingRender(true);
    setRetryError(null);
    try {
      await retryOwnedGenerativeJob(jobId);
      promotionAttemptRef.current = null;
      await refetch();
    } catch (cause) {
      setRetryError(cause instanceof Error ? cause.message : "The render could not be retried");
    } finally {
      setRetryingRender(false);
    }
  }, [jobId, refetch, retryingRender]);
  const succeeded = status != null && GENERATIVE_SUCCESS_STATUSES.includes(status.status);
  const currentPhase = status?.current_phase ?? (status?.started_at ? null : "queued");
  const receipt = status
    ? deriveReceiptText(
        status.started_at ?? status.created_at,
        status.finished_at ?? status.updated_at,
      )
    : "Your first cut is ready";

  return (
    <section aria-labelledby="create-progress-title">
      <div className="mb-9">
        <Eyebrow tone="lime" className="mb-3">
          Kria is editing
        </Eyebrow>
        <h1 id="create-progress-title" className="font-display text-4xl leading-tight text-[#0c0c0e]">
          Building your first cut.
        </h1>
        <p className="mt-3 leading-7 text-[#71717a]">
          You can leave this page. When the video is ready, Kria will take you straight to the editor.
        </p>
      </div>

      {pollError && (
        <div role="status" className="mb-5 rounded-lg border border-zinc-200 bg-white px-4 py-3 text-sm text-[#3f3f46]">
          Trouble reaching the video service. Kria is still retrying.
          <button type="button" onClick={refetch} className="ml-2 min-h-[44px] font-semibold underline underline-offset-4">
            Check again
          </button>
        </div>
      )}

      {promotionError && (
        <div role="alert" className="mb-5 rounded-lg border border-zinc-200 bg-white px-4 py-4 text-sm text-[#3f3f46]">
          <p>{promotionError}</p>
          <button
            type="button"
            onClick={() => {
              promotionAttemptRef.current = null;
              void openEditor();
            }}
            className="mt-3 min-h-[44px] rounded-full bg-[#0c0c0e] px-5 py-2 font-semibold text-white"
          >
            Open editor again
          </button>
        </div>
      )}

      {failed && (
        <div role="alert" className="mb-6 rounded-lg border border-zinc-200 bg-white px-4 py-4 text-[#3f3f46]">
          <p className="font-semibold">{failureCopy.title}</p>
          <p className="mt-1 text-sm text-[#71717a]">
            {failureCopy.detail}
          </p>
          {retryError && <p className="mt-2 text-sm text-[#71717a]">{retryError}</p>}
          <div className="mt-4 flex flex-wrap gap-3">
            {canRetryRender && (
              <button type="button" onClick={() => void retryRender()} disabled={retryingRender} className="min-h-[44px] rounded-full bg-[#0c0c0e] px-5 py-2 text-sm font-semibold text-white disabled:opacity-50">
                {retryingRender ? "Retrying…" : "Try render again"}
              </button>
            )}
            <button type="button" onClick={onStartOver} className="min-h-[44px] rounded-full border border-zinc-300 px-5 py-2 text-sm font-medium text-[#3f3f46]">
              {hasRetryInputs || failureCopy.action === "review_media" ? "Review my setup" : "Start a new edit"}
            </button>
          </div>
          <p className="mt-3 text-xs text-[#a1a1aa]">Support reference: {jobId}</p>
        </div>
      )}

      <ProgressTheater
        phases={GENERATIVE_PHASE_ORDER}
        phaseLabels={GENERATIVE_PHASE_LABEL}
        currentPhase={currentPhase}
        expectedPhaseMs={status?.expected_phase_durations ?? null}
        phaseLog={status?.phase_log ?? null}
        startedAt={status?.started_at ?? null}
        jobCreatedAt={status?.created_at ?? new Date().toISOString()}
        isTerminal={status != null && isTerminal(status)}
        isSuccess={succeeded}
        receiptText={receipt}
        variants={status?.variants ?? null}
        retrying={status?.retrying ?? false}
        steps={status?.steps ?? null}
        size="full"
        tone="light"
      >
        <div className="flex min-h-[180px] items-center justify-center rounded-xl border border-dashed border-zinc-300 bg-white px-6 text-center">
          <div>
            <p className="font-display text-2xl text-[#0c0c0e]">
              {openingEditor ? "Opening your edit…" : failed ? "Your setup is still here." : "Your video will open here."}
            </p>
            <p className="mx-auto mt-2 max-w-sm text-sm leading-6 text-[#71717a]">
              {openingEditor
                ? "Kria has chosen the first ready cut and is loading the full editor."
                : "The editor stays focused on the video, with direction and planning available when you need them."}
            </p>
          </div>
        </div>
      </ProgressTheater>

      <Link href="/plan" className="mt-8 inline-flex min-h-[44px] items-center text-sm text-[#71717a] underline-offset-4 hover:text-[#0c0c0e] hover:underline">
        Back to Create
      </Link>
    </section>
  );
}
