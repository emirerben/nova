"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import type { LibraryJob } from "@/lib/me-api";
import { getTikTokPublication, shouldPollTikTokPublication, type TikTokPublication } from "@/lib/tiktok-api";
import { jobFailureCopy } from "@/lib/job-failure-copy";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/cn";

/**
 * One 9:16 video in the library. Light editorial canvas (D20/D21).
 *
 * Poster tile (DESIGN.md §15 / §12, Paper "P1 Home" + "C3 Cards, media &
 * lists"): media + a status badge, and — when the video is pinned to a plan
 * item — the whole tile is a `<Link>` to that item's page with a hover/focus
 * "Open" pill. Download/Publish/Add-to-plan/feedback controls live only on
 * the item page now; nothing else renders as a footer action row here.
 */
export default function LibraryTile({ job }: { job: LibraryJob }) {
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
  const isReady = job.status === "ready" && Boolean(job.output_url);

  const media = (
    <div
      className={cn(
        "relative aspect-[9/16] overflow-hidden rounded-xl border bg-zinc-100",
        isFailed ? "border-dashed border-zinc-300" : "border-zinc-200",
      )}
    >
      {isReady ? (
        <video
          src={job.output_url!}
          preload="metadata"
          muted
          playsInline
          className="h-full w-full object-cover"
          aria-label="Your video"
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
      {latestPublication && <TikTokStatus publication={latestPublication} />}
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
