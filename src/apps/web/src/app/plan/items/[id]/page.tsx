"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  attachClips,
  changePlanItemStyle,
  generatePlanItem,
  getPlanItem,
  getPlanItemJobStatus,
  NotAuthenticatedError,
  type PlanItem,
  type PlanItemJobStatus,
  type PlanItemVariant,
  requestUploadUrls,
  retextPlanItem,
  setPlanItemIntroSize,
  swapPlanItemSong,
  uploadToGcs,
} from "@/lib/plan-api";
import { getGenerativeStyleSets, type GenerativeStyleSet } from "@/lib/generative-api";
import { getMusicTracks, type MusicTrackSummary } from "@/lib/music-api";
import { FONT_FACES } from "@/lib/font-faces";
import { downloadVideo } from "@/lib/download-video";
import { stripRationalePrefix } from "@/lib/plan-text";
import { GENERATIVE_PHASE_ORDER, GENERATIVE_PHASE_LABEL } from "@/lib/job-phases";
import { ProgressTheater } from "@/components/progress";
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";
import PlanShell from "../../_components/PlanShell";
import PlanFilmstrip from "../../_components/PlanFilmstrip";
import PlanVariantEditor from "../../_components/PlanVariantEditor";
import SignInPrompt from "../../_components/SignInPrompt";
import FeedbackButtons from "../../../library/_components/FeedbackButtons";

function deriveReceiptText(job: PlanItemJobStatus): string {
  if (job.started_at && job.finished_at) {
    const ms = new Date(job.finished_at).getTime() - new Date(job.started_at).getTime();
    const secs = Math.floor(ms / 1000);
    const mins = Math.floor(secs / 60);
    const s = secs % 60;
    return `Ready in ${mins}:${String(s).padStart(2, "0")}`;
  }
  return "Your edits are ready";
}

export default function PlanItemPage() {
  const params = useParams<{ id: string }>();
  const itemId = params.id;

  const [loading, setLoading] = useState(true);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [generating, setGenerating] = useState(false);
  // Song gallery + curated style sets for the per-variant edit controls. Both are
  // public GET endpoints (same as the generative page), so fetch them directly —
  // not through the authenticated /api/plan proxy.
  const [tracks, setTracks] = useState<MusicTrackSummary[]>([]);
  const [styleSets, setStyleSets] = useState<GenerativeStyleSet[]>([]);
  // Which variant the hero shows + the editor edits. Kept valid by an effect
  // below: defaults to the first ready variant, never points at a gone id.
  const [focusedVariantId, setFocusedVariantId] = useState<string | null>(null);
  // variant_id → the output_url at the moment the user submitted an edit. While a
  // variant is in here we keep polling and keep showing its spinner — we can't
  // rely on catching the worker's transient "rendering" flag (the throttled
  // plan-jobs queue may flip it between polls), so we wait for the URL to change.
  const pendingEdits = useRef<Map<string, { priorOutputUrl: string | null }>>(new Map());

  useEffect(() => {
    getMusicTracks()
      .then((r) => setTracks(r.tracks))
      .catch(() => setTracks([]));
    getGenerativeStyleSets()
      .then(setStyleSets)
      .catch(() => setStyleSets([]));
  }, []);

  // Composite fetcher: item + job status in one shot.
  const fetcher = useCallback(async () => {
    const it = await getPlanItem(itemId);
    const jobSt = it.current_job_id
      ? await getPlanItemJobStatus(it.current_job_id)
      : null;
    return { item: it, job: jobSt };
  }, [itemId]);

  const isTerminalFn = useCallback(
    ({ item, job }: { item: PlanItem; job: PlanItemJobStatus | null }) => {
      const anyRendering =
        job?.variants?.some((v) => v.render_status === "rendering") ?? false;
      const pending = pendingEdits.current;
      return (
        !anyRendering &&
        pending.size === 0 &&
        item.status !== "generating" &&
        !(item.current_job_id && item.status !== "ready" && item.status !== "failed")
      );
    },
    [],
  );

  const {
    data,
    error: pollError,
    refetch,
  } = usePolledJobStatus(fetcher, undefined, isTerminalFn);

  // Set loading false once first data arrives (or error).
  useEffect(() => {
    if (data !== null || pollError !== null) setLoading(false);
  }, [data, pollError]);

  // Handle auth errors from the poll.
  useEffect(() => {
    if (pollError instanceof NotAuthenticatedError) setNeedsAuth(true);
    else if (pollError) setError(pollError.message);
  }, [pollError]);

  const item = data?.item ?? null;

  // pendingEdits overlay: remove when output_url changes (not when render_status changes).
  const variants = useMemo(
    () => {
      const rawVariants = data?.job?.variants ?? [];
      return rawVariants.map((v) => {
        const pending = pendingEdits.current.get(v.variant_id);
        if (!pending) return v;
        // Still pending if the output_url hasn't changed.
        if (v.output_url !== pending.priorOutputUrl) {
          pendingEdits.current.delete(v.variant_id);
          return v;
        }
        return { ...v, render_status: "rendering" as const };
      });
    },
    [data],
  );

  // Keep a valid focused variant as renders land: default to the first variant
  // with a playable output (else the first one), and reset if the focused id
  // disappears (e.g. a fresh generate replaces the variant set).
  useEffect(() => {
    if (variants.length === 0) {
      if (focusedVariantId !== null) setFocusedVariantId(null);
      return;
    }
    if (!variants.some((v) => v.variant_id === focusedVariantId)) {
      const firstReady = variants.find((v) => v.output_url) ?? variants[0];
      setFocusedVariantId(firstReady.variant_id);
    }
  }, [variants, focusedVariantId]);

  // Optimistically flip a variant to "rendering" so the card shows a spinner and
  // the poll fires immediately — the worker only sets the real flag once it
  // dequeues the task, after the POST returns.
  const markVariantRendering = useCallback(
    (variantId: string, priorOutputUrl: string | null) => {
      pendingEdits.current.set(variantId, { priorOutputUrl });
      refetch();
    },
    [refetch],
  );

  const runEdit = useCallback(
    async (variantId: string, prevUrl: string | null, action: () => Promise<unknown>) => {
      setError(null);
      try {
        await action();
        // Track until the re-rendered URL lands (see pendingEdits) and keep polling.
        markVariantRendering(variantId, prevUrl);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to update variant");
        refetch(); // clear the optimistic spinner if the request was rejected
      }
    },
    [markVariantRendering, refetch],
  );

  async function handleFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError(null);
    try {
      const list = Array.from(files);
      const urls = await requestUploadUrls(
        itemId,
        list.map((f) => ({
          filename: f.name,
          content_type: f.type || "video/mp4",
          file_size_bytes: f.size,
        })),
      );
      await Promise.all(urls.map((u, i) => uploadToGcs(u.upload_url, list[i])));
      const existing = item?.clip_gcs_paths ?? [];
      await attachClips(itemId, [...existing, ...urls.map((u) => u.gcs_path)]);
      refetch();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function handleGenerate() {
    setGenerating(true);
    setError(null);
    try {
      await generatePlanItem(itemId);
      refetch();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start generation");
    } finally {
      setGenerating(false);
    }
  }

  if (needsAuth) {
    return (
      <PlanShell>
        <SignInPrompt
          callbackUrl={`/plan/items/${itemId}`}
          title="Sign in to continue"
          subtitle="We use your Google account to save your clips and renders."
        />
      </PlanShell>
    );
  }

  if (loading) {
    return (
      <PlanShell>
        <p className="py-24 text-center text-zinc-400">Loading…</p>
      </PlanShell>
    );
  }

  if (item === null) {
    return (
      <PlanShell>
        <div className="motion-safe:animate-fade-up py-24 text-center">
          <p className="mb-6 text-zinc-400">We couldn&apos;t find that idea.</p>
          <Link
            href="/plan"
            className="inline-block rounded-full bg-white px-6 py-3 text-sm font-medium text-black transition-colors hover:bg-zinc-200"
          >
            Back to your plan
          </Link>
        </div>
      </PlanShell>
    );
  }

  const clipCount = item.clip_gcs_paths.length;
  const isGenerating = item.status === "generating";
  const focused = variants.find((v) => v.variant_id === focusedVariantId) ?? null;
  // The focused variant is editable once it has rendered (or failed) at least once.
  const focusedEditable =
    focused && (!!focused.output_url || focused.render_status === "failed");
  const showResults = isGenerating || variants.length > 0;

  // Theater props
  const currentPhase =
    data?.job?.current_phase ??
    (!data?.job?.started_at ? "queued" : null);
  const theaterIsTerminal = !!(item && isTerminalFn({ item, job: data?.job ?? null }));
  const theaterIsSuccess = item?.status === "ready";

  return (
    <PlanShell size="results">
      {/* @font-face for the style-preview chips — fonts lazy-load only when used. */}
      <style dangerouslySetInnerHTML={{ __html: FONT_FACES }} />
      <div className="motion-safe:animate-fade-up py-12">
        {/* ── Editorial header + controls (narrow, readable column) ── */}
        <div className="max-w-2xl">
          <Link
            href="/plan"
            className="text-sm text-zinc-500 underline transition-colors hover:text-white"
          >
            ← back to plan
          </Link>
          <div className="mb-1 mt-4 flex items-center gap-3">
            <span className="rounded bg-zinc-800 px-2 py-0.5 text-xs text-zinc-400">
              Day {item.day_index}
            </span>
          </div>
          <h1 className="font-display text-3xl text-white">{item.theme}</h1>
          <p className="mb-2 mt-2 text-zinc-300">{item.idea}</p>
          {item.rationale && (
            <div className="mb-4 mt-3 rounded-lg border border-zinc-800 bg-zinc-950/40 p-4">
              <p className="mb-1 text-xs font-medium text-amber-300/80">Why this works</p>
              <p className="text-sm text-zinc-300">{stripRationalePrefix(item.rationale)}</p>
            </div>
          )}
          {item.filming_guide && item.filming_guide.length > 0 ? (
            <div className="mb-8 mt-1 rounded-lg border border-zinc-800 bg-zinc-950/40 p-4">
              <p className="mb-2 text-xs font-medium text-amber-300/80">🎬 How to film this</p>
              <ol className="space-y-2">
                {item.filming_guide.map((shot, i) => (
                  <li key={i} className="flex items-start gap-2 text-sm">
                    <span className="shrink-0 rounded bg-zinc-800 px-1.5 py-0.5 text-xs text-zinc-400">
                      {shot.duration_s}s
                    </span>
                    <span>
                      <span className="text-zinc-300">{shot.what}</span>
                      {shot.how ? (
                        <span className="text-zinc-500"> — {shot.how}</span>
                      ) : null}
                    </span>
                  </li>
                ))}
              </ol>
            </div>
          ) : item.filming_suggestion ? (
            <p className="mb-8 text-sm text-zinc-500">🎬 {item.filming_suggestion}</p>
          ) : null}

          {error && (
            <div className="mb-6 rounded border border-red-700 bg-red-950/50 px-4 py-3 text-red-200">
              {error}
            </div>
          )}

          {/* Upload */}
          <section className="mb-8 rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
            <h2 className="mb-2 text-sm font-semibold text-zinc-300">Themed clips</h2>
            <p className="mb-4 text-sm text-zinc-500">
              Upload footage for this idea. {clipCount > 0 ? `${clipCount} uploaded.` : "None yet."}
            </p>
            <label className="block">
              <span className="sr-only">Upload video clips for this idea</span>
              <input
                type="file"
                accept="video/mp4,video/quicktime"
                multiple
                disabled={uploading}
                onChange={(e) => handleFiles(e.target.files)}
                className="block w-full text-sm text-zinc-400 file:mr-3 file:rounded-full file:border-0 file:bg-white file:px-4 file:py-2 file:text-sm file:font-medium file:text-black hover:file:bg-zinc-200"
              />
            </label>
            {uploading && <p className="mt-3 text-sm text-amber-300">Uploading…</p>}
          </section>

          {/* Generate */}
          <button
            onClick={handleGenerate}
            disabled={generating || clipCount === 0 || isGenerating}
            className="rounded-full bg-amber-400 px-6 py-3 font-medium text-black transition-colors hover:bg-amber-300 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-400"
          >
            {isGenerating ? "Generating…" : generating ? "Starting…" : "Generate videos"}
          </button>
          {clipCount === 0 && (
            <p className="mt-2 text-sm text-zinc-500">Upload at least one clip first.</p>
          )}

          {/* ProgressTheater: replaces old StatusLine + "(N of M ready)" text block */}
          {data?.job && (
            <div className="mt-8">
              <ProgressTheater
                phases={GENERATIVE_PHASE_ORDER}
                phaseLabels={GENERATIVE_PHASE_LABEL}
                currentPhase={currentPhase}
                expectedPhaseMs={data.job.expected_phase_durations ?? null}
                phaseLog={data.job.phase_log ?? null}
                startedAt={data.job.started_at ?? null}
                jobCreatedAt={data.job.created_at ?? new Date().toISOString()}
                isTerminal={theaterIsTerminal}
                isSuccess={theaterIsSuccess}
                receiptText={deriveReceiptText(data.job)}
                variants={variants}
                size="full"
              >
                {null}
              </ProgressTheater>
            </div>
          )}
          {isGenerating && (
            <p className="mt-1 text-xs text-zinc-500">
              Usually 2–3 minutes. You can leave this page — we&apos;ll keep rendering.
            </p>
          )}
          {item.status === "failed" && variants.length === 0 && (
            <p className="mt-2 text-sm text-zinc-500">
              Generation failed before any variant rendered. Try generating again.
            </p>
          )}
        </div>

        {/* ── Results: focused player + filmstrip + editor (full width) ── */}
        {showResults && (
          <div className="mt-6 flex flex-col gap-6 lg:flex-row lg:items-start">
            {/* Hero */}
            <div className="w-full shrink-0 sm:max-w-sm lg:w-[380px]">
              <Hero variant={focused} generating={isGenerating} />
              {focused?.output_url && (
                <button
                  type="button"
                  onClick={() =>
                    downloadVideo(
                      focused.output_url!,
                      `nova-${slugify(item.theme) || itemId.slice(0, 8)}.mp4`,
                    )
                  }
                  className="mt-3 inline-flex min-h-11 w-full items-center justify-center rounded-full border border-zinc-700 px-5 py-2 text-sm text-zinc-200 transition-colors hover:border-zinc-400 hover:text-white"
                >
                  Download
                </button>
              )}
            </div>
            {/* Filmstrip + editor */}
            <div className="min-w-0 flex-1 space-y-5">
              {variants.length > 0 && (
                <PlanFilmstrip
                  variants={variants}
                  focusedId={focusedVariantId}
                  onFocus={setFocusedVariantId}
                />
              )}
              {focused && focusedEditable ? (
                <PlanVariantEditor
                  variant={focused}
                  tracks={tracks}
                  styleSets={styleSets}
                  onSwap={(trackId) =>
                    runEdit(focused.variant_id, focused.output_url, () =>
                      swapPlanItemSong(itemId, focused.variant_id, trackId),
                    )
                  }
                  onRetext={(text) =>
                    runEdit(focused.variant_id, focused.output_url, () =>
                      retextPlanItem(itemId, focused.variant_id, { text }),
                    )
                  }
                  onRemoveText={() =>
                    runEdit(focused.variant_id, focused.output_url, () =>
                      retextPlanItem(itemId, focused.variant_id, { remove: true }),
                    )
                  }
                  onChangeStyle={(styleSetId) =>
                    runEdit(focused.variant_id, focused.output_url, () =>
                      changePlanItemStyle(itemId, focused.variant_id, styleSetId),
                    )
                  }
                  onResize={(px) =>
                    runEdit(focused.variant_id, focused.output_url, () =>
                      setPlanItemIntroSize(itemId, focused.variant_id, px),
                    )
                  }
                />
              ) : (
                isGenerating && (
                  <p className="text-sm text-zinc-500">
                    Edit controls unlock as soon as a variant finishes rendering.
                  </p>
                )
              )}
              {/* Per-video feedback (feedback loop, Phase 2): keyed to the item's
                  Job so a reaction here lands in the same store as the library. */}
              {item.current_job_id && !isGenerating && (
                <div className="border-t border-zinc-800 pt-4">
                  <p className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                    How&apos;s this one?
                  </p>
                  <FeedbackButtons jobId={item.current_job_id} initialSignal={null} />
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </PlanShell>
  );
}

/** Large hero player for the focused variant. Keeps the previous video visible
 *  with an overlay while a re-render is in flight (never blanks the payoff). */
function Hero({
  variant,
  generating,
}: {
  variant: PlanItemVariant | null;
  generating: boolean;
}) {
  if (!variant) return <SkeletonTile />;
  const rendering = variant.render_status === "rendering";
  const failed = variant.render_status === "failed";
  return (
    <div className="relative aspect-[9/16] w-full overflow-hidden rounded-xl border border-zinc-800 bg-black">
      {variant.output_url ? (
        <video src={variant.output_url} controls className="h-full w-full object-contain" />
      ) : failed ? (
        <div className="flex h-full items-center justify-center px-4 text-center text-sm text-red-300">
          This variant failed — try editing again.
        </div>
      ) : (
        <div className="flex h-full items-center justify-center text-sm text-zinc-500">
          {generating ? "Rendering…" : "No preview yet"}
        </div>
      )}
      {rendering && variant.output_url && (
        <div
          className="absolute inset-0 flex items-center justify-center bg-black/55 text-sm text-amber-300"
          role="status"
        >
          Rendering new version…
        </div>
      )}
    </div>
  );
}

function SkeletonTile() {
  return (
    <div className="aspect-[9/16] w-full motion-safe:animate-shimmer rounded-xl border border-zinc-800 bg-[length:200%_100%] bg-gradient-to-r from-zinc-900 via-zinc-800 to-zinc-900" />
  );
}

/** Lowercase, hyphenated, ASCII-safe slug for download filenames. */
function slugify(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}
