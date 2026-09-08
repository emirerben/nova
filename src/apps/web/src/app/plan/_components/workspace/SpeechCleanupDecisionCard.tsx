"use client";

import { RefreshCw, Sparkles } from "lucide-react";
import { ChatArtifactCard } from "@/components/chat/ChatArtifactCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type {
  CreationSpeechCleanupChoice,
  CreationSpeechCleanupOutcome,
  CreationSpeechCleanupProjection,
} from "@/lib/creation-thread-api";

export type SpeechCleanupPendingAction =
  | CreationSpeechCleanupChoice
  | "no_findings"
  | "retry_analysis"
  | "retry_render"
  | "bypass"
  | null;

interface SpeechCleanupDecisionCardProps {
  cleanup: CreationSpeechCleanupProjection;
  formatLabel: string;
  direction?: string;
  busy?: boolean;
  pendingAction?: SpeechCleanupPendingAction;
  onGenerate: (choice?: CreationSpeechCleanupChoice) => void;
  onRetryAnalysis: () => void;
  onRetryRender: () => void;
  onCreateWithoutCleanup: () => void;
}

const CATEGORY_LABELS: Record<string, string> = {
  filler: "filler sound",
  fillers: "filler sound",
  filler_sound: "filler sound",
  filler_sounds: "filler sound",
  awkward_sound: "awkward sound",
  awkward_sounds: "awkward sound",
  long_pause: "long pause",
  long_pauses: "long pause",
  pause: "long pause",
  pauses: "long pause",
  retake: "retake",
  retakes: "retake",
};

function pluralize(count: number, label: string): string {
  return `${count} ${label}${count === 1 ? "" : "s"}`;
}

/** Clip-length copy: precise under a minute, readable above it. */
function formatClipLength(ms: number): string {
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)}m ${whole % 60}s`;
}

function finiteMs(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function joinList(parts: string[]): string {
  if (parts.length <= 1) return parts[0] ?? "";
  if (parts.length === 2) return `${parts[0]} and ${parts[1]}`;
  return `${parts.slice(0, -1).join(", ")}, and ${parts.at(-1)}`;
}

function evidenceCopy(cleanup: CreationSpeechCleanupProjection): string {
  const analysis = cleanup.analysis;
  const categories = Object.entries(analysis?.category_counts ?? {})
    .filter(([, count]) => Number.isFinite(count) && count > 0)
    .map(([category, count]) => pluralize(count, CATEGORY_LABELS[category] ?? category.replaceAll("_", " ")));
  const durationMs = analysis?.estimated_removed_ms;
  const categoryCopy = joinList(categories);
  // The removal is capped only by a minimum output length, so the delta alone
  // can hide a two-thirds cut. State the resulting length in the same breath.
  const sourceMs = finiteMs(analysis?.source_duration_ms);
  const resultMs = finiteMs(analysis?.result_duration_ms);
  const outcomeCopy = sourceMs !== null && sourceMs > 0 && resultMs !== null
    ? ` — your ${formatClipLength(sourceMs)} clip becomes ${formatClipLength(resultMs)}`
    : "";
  const durationCopy = typeof durationMs === "number" && Number.isFinite(durationMs) && durationMs > 0
    ? ` Cleaning them removes about ${(durationMs / 1000).toFixed(durationMs < 10_000 ? 1 : 0)} seconds${outcomeCopy}, and captions stay in sync.`
    : " Captions stay in sync.";
  if (categoryCopy) return `Kria found ${categoryCopy}.${durationCopy}`;
  return "Kria found speech moments it can clean up while keeping captions in sync.";
}

function failureGuidance(code: string | undefined): string {
  if (code === "unsupported_media") return "Replace the narration with a supported audio or video file.";
  if (code === "no_speech_track") return "Record a narration or replace this source with one that contains speech.";
  if (code === "snapshot_mismatch") return "The narration changed, so Kria is preparing a new speech check.";
  return "Your footage and direction are safe.";
}

function VideoRequiredNote() {
  return (
    <p className="w-full border-t border-zinc-200 pt-4 text-base leading-6 text-zinc-700">
      Add at least one video clip to make your video.
    </p>
  );
}

export function SpeechCleanupReceipt({
  outcome,
  preflightNoFindings = false,
}: {
  outcome?: CreationSpeechCleanupOutcome | null;
  preflightNoFindings?: boolean;
}) {
  let copy: string | null = null;
  if (preflightNoFindings) copy = "Speech checked. No cleanup suggested.";
  else if (outcome?.status === "applied") {
    copy = typeof outcome.removal_count === "number"
      ? `Speech cleanup applied · ${pluralize(outcome.removal_count, "moment")} removed`
      : "Speech cleanup applied";
  } else if (outcome?.status === "checked_no_change") copy = "Speech cleanup checked · no safe cuts applied";
  else if (outcome?.status === "declined") copy = "Speech kept as recorded";
  else if (outcome?.status === "bypassed_unchecked") copy = "Created without checking speech cleanup";
  else if (outcome?.status === "failed") copy = "Speech cleanup did not complete";
  if (!copy) return null;
  return (
    <p
      className="flex items-start gap-3 border-y border-zinc-200 py-3 text-base leading-6 text-zinc-700"
      data-testid="speech-cleanup-receipt"
    >
      <span className="mt-2 size-2 shrink-0 rounded-full bg-lime-600" aria-hidden />
      <span>{copy}</span>
    </p>
  );
}

export function speechCleanupAnnouncement(
  cleanup: CreationSpeechCleanupProjection | null | undefined,
  options: { rendering: boolean; ready: boolean },
): string {
  if (!cleanup?.applicable) return options.rendering ? "Creating your video." : "";
  const outcome = cleanup.outcome;
  if (cleanup.analysis?.status === "queued" || cleanup.analysis?.status === "running") return "Checking for filler sounds.";
  if (outcome?.status === "failed") return "Speech cleanup did not complete. Choose a recovery action.";
  if (options.ready && outcome?.status === "applied") return "Your video is ready with speech cleanup applied.";
  if (options.ready && outcome?.status === "checked_no_change") return "Your video is ready. Speech was checked and no safe cuts were needed.";
  if (options.ready && outcome?.status === "declined") return "Your video is ready with speech kept as recorded.";
  if (options.ready && outcome?.status === "bypassed_unchecked") return "Your video is ready without a speech check.";
  if (options.rendering) return cleanup.decision === "clean" ? "Cleaning speech and creating your video." : "Creating your video.";
  if (cleanup.analysis?.status === "no_findings") return "Speech check complete. No cleanup suggested.";
  if (cleanup.analysis?.status === "failed") return "Kria could not check the speech. Choose a recovery action.";
  if (cleanup.analysis?.status === "ready" && cleanup.analysis.has_findings) return "Speech check complete. Choose whether to clean up the speech.";
  return "";
}

export function SpeechCleanupDecisionCard({
  cleanup,
  formatLabel,
  direction,
  busy = false,
  pendingAction = null,
  onGenerate,
  onRetryAnalysis,
  onRetryRender,
  onCreateWithoutCleanup,
}: SpeechCleanupDecisionCardProps) {
  const analysis = cleanup.analysis;
  const blockedByVideo = cleanup.render_blocker === "video_required";
  const cardClass = "rounded-2xl border-zinc-200 bg-white shadow-sm";

  if (analysis?.status === "queued" || analysis?.status === "running" || !analysis) {
    return (
      <ChatArtifactCard
        className={cardClass}
        title={<h2>Speech cleanup</h2>}
        description={<span className="text-base">Checking for filler sounds…</span>}
        data-testid="speech-cleanup-checking"
      >
        <div className="h-2 w-full motion-safe:animate-pulse rounded-full bg-muted" aria-hidden />
        {blockedByVideo ? <div className="mt-4"><VideoRequiredNote /></div> : null}
      </ChatArtifactCard>
    );
  }

  if (cleanup.outcome?.status === "failed") {
    const retryable = cleanup.outcome.error?.retryable === true;
    return (
      <ChatArtifactCard
        className={cardClass}
        title={<h2>Speech cleanup needs attention</h2>}
        description={<span className="text-base">The cleanup did not complete, so Kria did not publish an uncleaned result.</span>}
        data-testid="speech-cleanup-application-failed"
      >
        <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap" aria-busy={busy}>
          {retryable ? (
            <Button type="button" className="min-h-11 min-w-40 flex-1" disabled={busy} onClick={onRetryRender}>
              <RefreshCw />{pendingAction === "retry_render" ? "Retrying cleanup…" : "Retry cleanup"}
            </Button>
          ) : null}
          {!blockedByVideo ? (
            <Button type="button" variant="outline" className="min-h-11 min-w-40 flex-1" disabled={busy} onClick={onCreateWithoutCleanup}>
              {pendingAction === "bypass" ? "Creating video…" : "Create without cleanup"}
            </Button>
          ) : <VideoRequiredNote />}
        </div>
      </ChatArtifactCard>
    );
  }

  if (analysis.status === "no_findings" || (analysis.status === "ready" && analysis.has_findings === false)) {
    return (
      <ChatArtifactCard
        className={cardClass}
        badge={<Badge variant="secondary">Creative direction</Badge>}
        title={<h2>{formatLabel} is ready to make</h2>}
        description={<span className="text-base">{direction || "I’ll shape your footage into a concise first cut."}</span>}
        data-testid="speech-cleanup-no-findings"
      >
        <div className="space-y-4">
          <SpeechCleanupReceipt preflightNoFindings />
          {blockedByVideo ? <VideoRequiredNote /> : (
            <Button
              type="button"
              className="min-h-11 w-full"
              disabled={busy}
              onClick={() => onGenerate()}
            >
              <Sparkles />{pendingAction === "no_findings" ? "Creating video…" : "Create this video"}
            </Button>
          )}
        </div>
      </ChatArtifactCard>
    );
  }

  if (analysis.status === "failed") {
    const retryable = analysis.error?.retryable === true;
    return (
      <ChatArtifactCard
        className={cardClass}
        title={<h2>Kria couldn’t check the speech</h2>}
        description={<span className="text-base">{failureGuidance(analysis.error?.code)}</span>}
        data-testid="speech-cleanup-failed"
      >
        <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap" aria-busy={busy}>
          {retryable ? (
            <Button type="button" className="min-h-11 min-w-40 flex-1" disabled={busy} onClick={onRetryAnalysis}>
              <RefreshCw />{pendingAction === "retry_analysis" ? "Retrying speech check…" : "Retry speech check"}
            </Button>
          ) : null}
          {!blockedByVideo ? (
            <Button type="button" variant="outline" className="min-h-11 min-w-40 flex-1" disabled={busy} onClick={onCreateWithoutCleanup}>
              {pendingAction === "bypass" ? "Creating video…" : "Create without cleanup"}
            </Button>
          ) : <VideoRequiredNote />}
        </div>
      </ChatArtifactCard>
    );
  }

  const count = analysis.candidate_count;
  const question = typeof count === "number" && Number.isFinite(count) && count > 0
    ? `Clean up ${pluralize(count, "speech moment")}?`
    : "Clean up the speech?";

  return (
    <ChatArtifactCard
      className={cardClass}
      title={<h2>Speech cleanup</h2>}
      data-testid="speech-cleanup-findings"
    >
      <fieldset className="space-y-4" aria-busy={busy}>
        <legend className="text-xl font-semibold leading-tight text-foreground">{question}</legend>
        <p className="text-base leading-6 text-muted-foreground">{evidenceCopy(cleanup)}</p>
        {blockedByVideo ? <VideoRequiredNote /> : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2" data-testid="speech-cleanup-choice-grid">
            <Button
              type="button"
              variant="outline"
              aria-label={pendingAction === "clean" ? "Starting cleanup…" : "Clean up and create"}
              className="h-auto min-h-[112px] w-full min-w-0 flex-col items-start justify-start whitespace-normal p-4 text-left"
              disabled={busy}
              onClick={() => onGenerate("clean")}
            >
              <span className="text-base font-semibold leading-6">
                {pendingAction === "clean" ? "Starting cleanup…" : "Clean up and create"}
              </span>
              <span className="mt-2 text-base font-normal leading-6 text-muted-foreground">
                Remove the detected moments and tighten the delivery.
              </span>
            </Button>
            <Button
              type="button"
              variant="outline"
              aria-label={pendingAction === "keep_original" ? "Creating video…" : "Keep speech and create"}
              className="h-auto min-h-[112px] w-full min-w-0 flex-col items-start justify-start whitespace-normal p-4 text-left"
              disabled={busy}
              onClick={() => onGenerate("keep_original")}
            >
              <span className="text-base font-semibold leading-6">
                {pendingAction === "keep_original" ? "Creating video…" : "Keep speech and create"}
              </span>
              <span className="mt-2 text-base font-normal leading-6 text-muted-foreground">
                Leave your delivery exactly as recorded.
              </span>
            </Button>
          </div>
        )}
      </fieldset>
    </ChatArtifactCard>
  );
}
