"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ImageOff, MoreHorizontal, Play, Square } from "lucide-react";
import { toast } from "sonner";
import {
  deleteMyJob,
  getMyJobPlaybackUrl,
  MeApiError,
  type LibraryJob,
} from "@/lib/me-api";
import { libraryPosterIdentity } from "@/lib/library-poster";
import { getTikTokPublication, shouldPollTikTokPublication, type TikTokPublication } from "@/lib/tiktok-api";
import { jobFailureCopy } from "@/lib/job-failure-copy";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/cn";
import { StablePoster } from "@/components/StablePoster";

export const PREVIEW_LOAD_TIMEOUT_MS = 15_000;
const PREVIEW_ACTIVATED_EVENT = "nova:library-preview-activated";

function playbackNeedsDirectGesture(error: unknown): boolean {
  return Boolean(
    error &&
      typeof error === "object" &&
      "name" in error &&
      error.name === "NotAllowedError",
  );
}

/**
 * One 9:16 video in the library. Light editorial canvas (D20/D21).
 *
 * Poster tile (DESIGN.md §15 / §12, Paper "P1 Home" + "C3 Cards, media &
 * lists"): media + a status badge, and — when the video is pinned to a plan
 * item — the whole tile is a `<Link>` to that item's page with a hover/focus
 * "Open" pill. Download/Publish/Add-to-plan/feedback controls live only on
 * the item page now; nothing else renders as a footer action row here.
 */
export default function LibraryTile({
  job,
  onDeleted,
  onPosterLoadError,
  onPosterLoadSuccess,
  posterRecoveryExhausted = false,
  posterRefreshUnavailable = false,
}: {
  job: LibraryJob;
  onDeleted?: (jobId: string) => void;
  onPosterLoadError?: (jobId: string, posterIdentity: string | null) => void;
  onPosterLoadSuccess?: (jobId: string, posterIdentity: string | null) => void;
  posterRecoveryExhausted?: boolean;
  posterRefreshUnavailable?: boolean;
}) {
  const failureCopy = jobFailureCopy(job.error_class ?? job.failure_reason ?? job.raw_status);
  const [latestPublication, setLatestPublication] = useState<TikTokPublication | null>(
    job.tiktok_publication,
  );

  useEffect(() => {
    if (!latestPublication) return;
    if (!shouldPollTikTokPublication(latestPublication)) return;
    const timer = window.setInterval(() => {
      void getTikTokPublication(latestPublication.id).then(setLatestPublication).catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [latestPublication]);

  const href = job.content_plan_item_id ? `/plan/items/${job.content_plan_item_id}` : null;
  const isFailed = job.status === "failed";
  // A list-time signature can fail or expire while the durable video still
  // exists. Readiness comes from job state; playback gets its own fresh URL.
  const isReady = job.status === "ready";
  const isTerminal = job.status === "ready" || isFailed;
  const isTikTokActive = Boolean(latestPublication?.deletion_blocked);
  const canDelete = isTerminal && !isTikTokActive;
  const [menuOpen, setMenuOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [activePreviewUrl, setActivePreviewUrl] = useState<string | null>(null);
  const [posterLoadFailed, setPosterLoadFailed] = useState(false);
  const [previewAttemptFailed, setPreviewAttemptFailed] = useState(false);
  const [previewStatus, setPreviewStatus] = useState<
    "idle" | "refreshing" | "loading" | "awaiting_gesture" | "playing"
  >("idle");
  const triggerRef = useRef<HTMLButtonElement>(null);
  const keepButtonRef = useRef<HTMLButtonElement>(null);
  const previewTriggerRef = useRef<HTMLButtonElement | null>(null);
  const stopPreviewRef = useRef<HTMLButtonElement | null>(null);
  const directPlayRef = useRef<HTMLButtonElement | null>(null);
  const previewVideoRef = useRef<HTMLVideoElement | null>(null);
  const previewRequestGenerationRef = useRef(0);
  const previewAbortControllerRef = useRef<AbortController | null>(null);
  const previewAttemptDeadlineRef = useRef<number | null>(null);
  const previewAttemptActiveRef = useRef(false);
  const restorePreviewFocusRef = useRef(false);

  useEffect(() => {
    setPosterLoadFailed(false);
  }, [job.poster_identity, job.poster_url]);

  async function handleDelete() {
    setIsDeleting(true);
    try {
      await deleteMyJob(job.id);
      toast.success("Video deleted.");
      setDeleteOpen(false);
      onDeleted?.(job.id);
    } catch (error) {
      const status = error instanceof MeApiError ? error.status : undefined;
      if (status === 404) {
        toast.success("Video deleted.");
        setDeleteOpen(false);
        onDeleted?.(job.id);
        return;
      }
      toast.error(
        status === 409
          ? "This video is still being prepared or posted. Try again when it finishes."
          : "We couldn’t delete this video. It’s still safe. Try again.",
      );
      setDeleteOpen(false);
    } finally {
      setIsDeleting(false);
    }
  }

  const isFallbackPreviewActive = Boolean(!href && activePreviewUrl);
  const clearPreviewAttemptDeadline = useCallback(() => {
    if (previewAttemptDeadlineRef.current === null) return;
    window.clearTimeout(previewAttemptDeadlineRef.current);
    previewAttemptDeadlineRef.current = null;
  }, []);
  const deactivatePreview = useCallback((failed: boolean, restoreFocus: boolean) => {
    previewRequestGenerationRef.current += 1;
    previewAttemptActiveRef.current = false;
    previewAbortControllerRef.current?.abort();
    previewAbortControllerRef.current = null;
    clearPreviewAttemptDeadline();
    const video = previewVideoRef.current;
    if (video) {
      try {
        video.pause();
      } catch {
        // A failed media element can reject imperative controls. State cleanup
        // below still unmounts it and releases the resource.
      }
      previewVideoRef.current = null;
    }
    setPreviewAttemptFailed(failed);
    restorePreviewFocusRef.current = restoreFocus;
    setActivePreviewUrl(null);
    setPreviewStatus("idle");
  }, [clearPreviewAttemptDeadline]);
  const failActivePreview = useCallback(() => {
    deactivatePreview(true, true);
  }, [deactivatePreview]);
  const stopActivePreview = useCallback(() => {
    deactivatePreview(false, true);
  }, [deactivatePreview]);

  const handlePreviewPlayRejection = useCallback(
    (video: HTMLVideoElement, error: unknown) => {
      if (previewVideoRef.current !== video) return;
      if (playbackNeedsDirectGesture(error)) {
        // iOS Safari (notably Low Power Mode) may reject the delayed play()
        // after URL refresh because the original tap's activation has ended.
        // Keep this fresh URL mounted and let a direct gesture resume it.
        clearPreviewAttemptDeadline();
        setPreviewStatus("awaiting_gesture");
        return;
      }
      failActivePreview();
    },
    [clearPreviewAttemptDeadline, failActivePreview],
  );

  const attachPreviewVideo = useCallback(
    (video: HTMLVideoElement | null) => {
      const previousVideo = previewVideoRef.current;
      if (!video && previousVideo) {
        try {
          previousVideo.pause();
        } catch {
          // The state transition still releases the element.
        }
      }
      previewVideoRef.current = video;
      if (!video) return;

      setPreviewStatus("loading");
      try {
        void video.play().catch((error: unknown) => {
          handlePreviewPlayRejection(video, error);
        });
      } catch (error) {
        handlePreviewPlayRejection(video, error);
      }
    },
    [handlePreviewPlayRejection],
  );

  const playPreviewFromDirectGesture = useCallback(() => {
    const video = previewVideoRef.current;
    if (!video) {
      failActivePreview();
      return;
    }

    // Keep play() in this click's synchronous call stack. Awaiting anything
    // first loses Safari's user-activation grant and recreates the loop this
    // control exists to break.
    try {
      const playback = video.play();
      setPreviewStatus("loading");
      void playback.catch(() => {
        if (previewVideoRef.current === video) failActivePreview();
      });
    } catch {
      failActivePreview();
    }
  }, [failActivePreview]);

  const startPreview = useCallback(async () => {
    // The list's output_url may already be expired, so it must never be
    // mounted as a media source. The playback endpoint resolves the durable
    // object (or rejects the attempt) at click time.
    if (href || previewStatus === "refreshing") return;

    window.dispatchEvent(
      new CustomEvent(PREVIEW_ACTIVATED_EVENT, { detail: { jobId: job.id } }),
    );
    const requestGeneration = ++previewRequestGenerationRef.current;
    previewAbortControllerRef.current?.abort();
    const abortController = new AbortController();
    previewAbortControllerRef.current = abortController;
    clearPreviewAttemptDeadline();
    previewAttemptDeadlineRef.current = window.setTimeout(() => {
      if (requestGeneration === previewRequestGenerationRef.current) {
        deactivatePreview(true, true);
      }
    }, PREVIEW_LOAD_TIMEOUT_MS);
    previewAttemptActiveRef.current = true;
    restorePreviewFocusRef.current = false;
    setPreviewAttemptFailed(false);
    setActivePreviewUrl(null);
    setPreviewStatus("refreshing");

    try {
      const { video_url: videoUrl } = await getMyJobPlaybackUrl(
        job.id,
        abortController.signal,
      );
      if (requestGeneration !== previewRequestGenerationRef.current) return;
      if (previewAbortControllerRef.current === abortController) {
        previewAbortControllerRef.current = null;
      }
      if (typeof videoUrl !== "string" || videoUrl.length === 0) {
        throw new Error("Playback URL response was empty");
      }
      setActivePreviewUrl(videoUrl);
      setPreviewStatus("loading");
    } catch {
      if (requestGeneration === previewRequestGenerationRef.current) {
        deactivatePreview(true, true);
      }
    }
  }, [clearPreviewAttemptDeadline, deactivatePreview, href, job.id, previewStatus]);

  useEffect(() => {
    const stopOtherPreview = (event: Event) => {
      const activatedJobId = (event as CustomEvent<{ jobId?: string }>).detail?.jobId;
      if (activatedJobId === job.id || !previewAttemptActiveRef.current) return;
      deactivatePreview(false, false);
    };
    window.addEventListener(PREVIEW_ACTIVATED_EVENT, stopOtherPreview);
    return () => {
      window.removeEventListener(PREVIEW_ACTIVATED_EVENT, stopOtherPreview);
      previewRequestGenerationRef.current += 1;
      previewAttemptActiveRef.current = false;
      previewAbortControllerRef.current?.abort();
      previewAbortControllerRef.current = null;
      clearPreviewAttemptDeadline();
    };
  }, [clearPreviewAttemptDeadline, deactivatePreview, job.id]);

  useEffect(() => {
    if (isFallbackPreviewActive) {
      if (previewStatus === "awaiting_gesture") {
        directPlayRef.current?.focus();
      } else {
        stopPreviewRef.current?.focus();
      }
      return;
    }
    if (restorePreviewFocusRef.current) {
      restorePreviewFocusRef.current = false;
      previewTriggerRef.current?.focus();
    }
  }, [isFallbackPreviewActive, previewAttemptFailed, previewStatus]);

  useEffect(() => {
    if (!isFallbackPreviewActive || previewStatus !== "loading") return;

    const timeout = window.setTimeout(failActivePreview, PREVIEW_LOAD_TIMEOUT_MS);
    return () => window.clearTimeout(timeout);
  }, [failActivePreview, isFallbackPreviewActive, previewStatus]);

  const markPreviewPlaying = useCallback(() => {
    clearPreviewAttemptDeadline();
    setPreviewStatus("playing");
  }, [clearPreviewAttemptDeadline]);

  const posterUnavailable =
    job.poster_status === "unavailable" ||
    posterRecoveryExhausted ||
    posterLoadFailed;
  const posterRepairing =
    isReady && !job.poster_url && !posterUnavailable && !posterRefreshUnavailable;
  const posterRecoveryIdentity = libraryPosterIdentity(job);
  const fallbackMedia = isFallbackPreviewActive ? (
    <div className="relative h-full w-full bg-zinc-100">
      <video
        ref={attachPreviewVideo}
        src={activePreviewUrl ?? undefined}
        muted
        loop
        playsInline
        preload="metadata"
        onPlaying={markPreviewPlaying}
        onWaiting={() => {
          if (previewStatus !== "awaiting_gesture") setPreviewStatus("loading");
        }}
        onAbort={failActivePreview}
        onError={failActivePreview}
        aria-label="Your video preview"
        className="h-full w-full object-cover"
      />
      {previewStatus === "awaiting_gesture" ? (
        <div className="absolute inset-0 flex items-center justify-center bg-zinc-100/95 p-4">
          <Button
            ref={directPlayRef}
            type="button"
            variant="secondary"
            aria-label="Tap to play preview"
            className="min-h-11 whitespace-normal bg-white text-[#0c0c0e] shadow-sm hover:bg-white"
            onClick={playPreviewFromDirectGesture}
          >
            <Play className="mr-2 h-4 w-4" fill="currentColor" aria-hidden="true" />
            Tap to play
          </Button>
        </div>
      ) : previewStatus !== "playing" ? (
        <div
          role="status"
          className="pointer-events-none absolute inset-0 flex items-center justify-center bg-zinc-100/95 text-sm font-medium text-[#3f3f46]"
        >
          Loading preview…
        </div>
      ) : null}
      <Button
        ref={stopPreviewRef}
        type="button"
        variant="secondary"
        size="icon"
        aria-label="Stop preview"
        className="absolute left-2 top-2 z-20 h-11 w-11 rounded-full bg-white/95 text-[#0c0c0e] shadow-sm hover:bg-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
        onClick={stopActivePreview}
      >
        <Square className="h-4 w-4" fill="currentColor" aria-hidden="true" />
      </Button>
    </div>
  ) : posterUnavailable && href ? (
    <div
      role="status"
      className="flex h-full w-full flex-col items-center justify-center gap-3 bg-zinc-100 p-5 text-center text-[#52525b]"
    >
      <span className="flex h-12 w-12 items-center justify-center rounded-full bg-white shadow-sm" aria-hidden="true">
        <ImageOff className="h-5 w-5" />
      </span>
      <span className="text-sm font-medium">Thumbnail unavailable</span>
    </div>
  ) : posterRefreshUnavailable && href ? (
    <div
      role="status"
      className="flex h-full w-full flex-col items-center justify-center gap-3 bg-zinc-100 p-5 text-center text-[#3f3f46]"
    >
      <span className="flex h-12 w-12 items-center justify-center rounded-full bg-white shadow-sm" aria-hidden="true">
        <ImageOff className="h-5 w-5" />
      </span>
      <span className="text-sm font-medium">Thumbnail temporarily unavailable</span>
    </div>
  ) : posterRepairing && href ? (
    <div
      role="status"
      className="flex h-full w-full flex-col items-center justify-center gap-3 bg-zinc-100 p-5 text-center text-[#52525b]"
    >
      <span
        className="h-10 w-10 rounded-full border-2 border-zinc-200 border-t-zinc-500 motion-safe:animate-spin"
        aria-hidden="true"
      />
      <span className="text-sm font-medium">Preparing preview…</span>
    </div>
  ) : (
    <div className="h-full w-full bg-zinc-100 text-[#3f3f46]">
      {href ? (
        <div className="flex h-full w-full flex-col items-center justify-center gap-3 p-5 text-center">
          <span className="flex h-12 w-12 items-center justify-center rounded-full bg-white shadow-sm" aria-hidden="true">
            <ImageOff className="h-5 w-5" />
          </span>
          <span className="text-sm font-medium">Preparing preview…</span>
        </div>
      ) : (
        <Button
          ref={previewTriggerRef}
          type="button"
          variant="ghost"
          aria-label={
            previewStatus === "refreshing"
              ? "Loading preview"
              : previewAttemptFailed
                ? "Retry preview"
                : "Play preview"
          }
          disabled={previewStatus === "refreshing"}
          className="h-full min-h-11 w-full min-w-11 flex-col gap-3 whitespace-normal rounded-none p-5 text-center hover:bg-zinc-200/60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-4px] focus-visible:outline-[#0c0c0e]"
          onClick={() => void startPreview()}
        >
          <span className="flex h-12 w-12 items-center justify-center rounded-full bg-white shadow-sm" aria-hidden="true">
            <Play className="ml-0.5 h-5 w-5" fill="currentColor" />
          </span>
          <span className="rounded-md bg-white px-3 py-2 text-sm font-medium shadow-sm">
            {previewStatus === "refreshing"
              ? "Loading preview…"
              : previewAttemptFailed
                ? "Retry preview"
                : "Play preview"}
          </span>
          {previewStatus === "refreshing" && (
            <span role="status" className="whitespace-normal text-xs text-[#3f3f46]">
              Getting a fresh preview…
            </span>
          )}
          {previewAttemptFailed && (
            <span role="status" className="whitespace-normal text-xs text-[#3f3f46]">
              Preview unavailable. You can try again.
            </span>
          )}
          {posterUnavailable && !previewAttemptFailed && (
            <span role="status" className="whitespace-normal text-xs text-[#3f3f46]">
              Thumbnail unavailable. Tap to play.
            </span>
          )}
          {posterRefreshUnavailable && !posterUnavailable && !previewAttemptFailed && (
            <span role="status" className="whitespace-normal text-xs text-[#3f3f46]">
              Thumbnail temporarily unavailable. Tap to play.
            </span>
          )}
        </Button>
      )}
    </div>
  );

  const media = (
    <div
      className={cn(
        "relative aspect-[9/16] overflow-hidden rounded-xl border bg-zinc-100",
        isFailed ? "border-dashed border-zinc-300" : "border-zinc-200",
      )}
    >
      {isReady && isFallbackPreviewActive ? (
        fallbackMedia
      ) : isReady ? (
        <StablePoster
          src={job.poster_url}
          identity={job.poster_identity ?? undefined}
          retryKey={job.poster_url ?? undefined}
          alt="Your video"
          loading="lazy"
          decoding="async"
          className="h-full w-full object-cover"
          fallback={fallbackMedia}
          onError={() => {
            setPosterLoadFailed(true);
            onPosterLoadError?.(job.id, posterRecoveryIdentity);
          }}
          onLoad={() => {
            setPosterLoadFailed(false);
            onPosterLoadSuccess?.(job.id, posterRecoveryIdentity);
          }}
        />
      ) : isFailed ? (
        <div className="flex h-full w-full flex-col items-center justify-center gap-2 p-4 text-center">
          <span className="font-display text-base text-[#3f3f46]">{failureCopy.title}</span>
          <span className="text-xs text-[#71717a]">
            {href ? "Open to retry." : failureCopy.detail}
          </span>
          {failureCopy.action === "contact_support" && (
            <span className="text-[11px] text-[#a1a1aa]">
              Support reference:{" "}
              <code className="select-all font-mono">{job.id}</code>
            </span>
          )}
        </div>
      ) : (
        <div className="h-full w-full bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer" />
      )}

      {isReady && (
        <Badge variant="lime-soft" className="absolute bottom-2 left-2 normal-case tracking-normal">
          Ready to post
        </Badge>
      )}
      {!isReady && !isFailed && (
        <Badge variant="zinc" className="absolute bottom-2 left-2 gap-1.5 normal-case tracking-normal">
          <span className="h-1.5 w-1.5 rounded-full bg-lime-500" aria-hidden="true" />
          Rendering…
        </Badge>
      )}

      {href && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-black/0 opacity-0 transition-all duration-150 group-hover:bg-black/40 group-hover:opacity-100 group-focus-visible:bg-black/40 group-focus-visible:opacity-100">
          <span
            className={cn(
              buttonVariants({ variant: "outline", size: "pill" }),
              "border-transparent bg-white text-[#0c0c0e]",
            )}
          >
            Open
          </span>
        </div>
      )}
    </div>
  );

  return (
    <div className="motion-safe:animate-fade-up">
      <div className="relative">
        {href ? (
          <Link
            href={href}
            className="group block rounded-xl focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0c0c0e]"
          >
            {media}
          </Link>
        ) : (
          media
        )}
        {canDelete && (
          <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
            <DropdownMenuTrigger asChild>
              <Button
                ref={triggerRef}
                type="button"
                aria-label="More video actions"
                variant="ghost"
                size="icon"
                className="absolute right-2 top-2 z-10 h-11 w-11 rounded-full border border-white/70 bg-white/90 text-[#3f3f46] shadow-sm backdrop-blur transition hover:bg-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0c0c0e]"
              >
                <MoreHorizontal className="h-5 w-5" aria-hidden="true" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                disabled={isDeleting}
                onSelect={(event) => {
                  event.preventDefault();
                  setMenuOpen(false);
                  setDeleteOpen(true);
                }}
              >
                Delete video
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
      {latestPublication && <TikTokStatus publication={latestPublication} />}

      <AlertDialog open={deleteOpen} onOpenChange={(open) => !isDeleting && setDeleteOpen(open)}>
        <AlertDialogContent
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            keepButtonRef.current?.focus();
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            triggerRef.current?.focus();
          }}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this video?</AlertDialogTitle>
            <AlertDialogDescription>
              {href
                ? "This removes the finished video. Your edit plan and uploaded footage will stay available."
                : "This removes the video and its uploaded source footage from Kria. You can’t undo this."}
              {latestPublication && " A post already sent to TikTok stays there."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel ref={keepButtonRef} disabled={isDeleting}>
              Keep video
            </AlertDialogCancel>
            <AlertDialogAction
              disabled={isDeleting}
              onClick={(event) => {
                event.preventDefault();
                void handleDelete();
              }}
            >
              {isDeleting ? "Deleting…" : "Delete video"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function TikTokStatus({ publication }: { publication: TikTokPublication }) {
  const metrics = publication.latest_metrics;
  const label = publication.visibility_status === "public"
    ? "Live on TikTok"
    : publication.visibility_status === "draft"
      ? "Waiting in your TikTok app inbox"
    : publication.visibility_status === "removed"
      ? "No longer public"
      : publication.visibility_status === "private"
        ? "Published privately on TikTok"
      : publication.delivery_mode === "draft_upload" && publication.processing_status === "complete"
        ? "Posted from TikTok"
      : publication.processing_status === "submission_unknown"
        ? "Check TikTok before retrying"
        : publication.processing_status === "failed"
          ? publication.retryable ? "TikTok is retrying" : "TikTok publish failed"
          : publication.processing_status === "complete"
            ? "TikTok moderation pending"
            : "Publishing to TikTok…";
  return (
    <div className="mt-3 rounded-lg bg-zinc-50 p-2 text-xs text-[#3f3f46]">
      <p className="font-medium">{label}</p>
      {publication.visibility_status === "draft" ? (
        <p className="mt-0.5 text-[#71717a]">Open the TikTok app, go to Inbox, and tap the notification. It won&apos;t appear on tiktok.com or under Drafts.</p>
      ) : publication.visibility_status !== "public" ? (
        <p className="mt-0.5 text-[#71717a]">Metrics begin when the post is public.</p>
      ) : null}
      {metrics && <p className="mt-1 text-[#71717a]">{formatMetric(metrics.view_count)} views · {formatMetric(metrics.like_count)} likes · {formatMetric(metrics.comment_count)} comments · {formatMetric(metrics.share_count)} shares</p>}
      <p className="mt-1 text-[#a1a1aa]">Updated {new Date(publication.updated_at).toLocaleString()}</p>
    </div>
  );
}

function formatMetric(value: number | null | undefined) {
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(value ?? 0);
}
