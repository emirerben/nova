"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  changePlanItemStyle,
  editPlanItemVariant,
  generatePlanItem,
  getPlanItem,
  getPlanItemJobStatus,
  NotAuthenticatedError,
  type ConformanceVerdict,
  type PlanItem,
  type PlanItemJobStatus,
  type PlanItemVariant,
  requestUploadUrls,
  retextPlanItem,
  setPlanItemIntroSize,
  swapPlanItemSong,
  uploadToGcs,
} from "@/lib/plan-api";
import ShotSlotUploader from "./components/ShotSlotUploader";
import { getGenerativeStyleSets, type GenerativeStyleSet } from "@/lib/generative-api";
import { getMusicTracks, type MusicTrackSummary } from "@/lib/music-api";
import { FONT_FACES } from "@/lib/font-faces";
import { downloadVideo } from "@/lib/download-video";
import { stripRationalePrefix } from "@/lib/plan-text";
import { GENERATIVE_PHASE_ORDER, GENERATIVE_PHASE_LABEL } from "@/lib/job-phases";
import { ProgressTheater } from "@/components/progress";
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";
import { LightShell } from "@/components/ui/LightShell";
import { InkButton } from "@/components/ui/InkButton";
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
  // uploaderBusy: true while ShotSlotUploader has any upload/commit in flight (D6).
  const [uploaderBusy, setUploaderBusy] = useState(false);
  const [tracks, setTracks] = useState<MusicTrackSummary[]>([]);
  const [styleSets, setStyleSets] = useState<GenerativeStyleSet[]>([]);
  const [focusedVariantId, setFocusedVariantId] = useState<string | null>(null);
  const pendingEdits = useRef<Map<string, { priorOutputUrl: string | null }>>(new Map());
  // Conformance polling: keep fetching for up to 3 extra cycles after clips are attached
  // so the verdict panel appears shortly after the async agent finishes (~6s window).
  const conformancePolls = useRef(0);

  useEffect(() => {
    getMusicTracks()
      .then((r) => setTracks(r.tracks))
      .catch(() => setTracks([]));
    getGenerativeStyleSets()
      .then(setStyleSets)
      .catch(() => setStyleSets([]));
  }, []);

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
      const baseTerminal =
        !anyRendering &&
        pending.size === 0 &&
        item.status !== "generating" &&
        !(item.current_job_id && item.status !== "ready" && item.status !== "failed");

      // Keep polling for up to 3 extra cycles when the item has clips but no
      // conformance verdict yet (the async task may still be running).
      const hasClips = (item.clip_gcs_paths?.length ?? 0) > 0;
      const hasFilmingGuide = (item.filming_guide?.length ?? 0) > 0;
      const awaitingConformance =
        hasClips && hasFilmingGuide && !item.conformance && conformancePolls.current < 3;
      if (awaitingConformance) {
        conformancePolls.current += 1;
        return false;
      }
      return baseTerminal;
    },
    [],
  );

  const {
    data,
    error: pollError,
    refetch,
  } = usePolledJobStatus(fetcher, undefined, isTerminalFn);

  useEffect(() => {
    if (data !== null || pollError !== null) setLoading(false);
  }, [data, pollError]);

  useEffect(() => {
    if (pollError instanceof NotAuthenticatedError) setNeedsAuth(true);
    else if (pollError) setError(pollError.message);
  }, [pollError]);

  const item = data?.item ?? null;

  const variants = useMemo(
    () => {
      const rawVariants = data?.job?.variants ?? [];
      return rawVariants.map((v) => {
        const pending = pendingEdits.current.get(v.variant_id);
        if (!pending) return v;
        if (v.output_url !== pending.priorOutputUrl) {
          pendingEdits.current.delete(v.variant_id);
          return v;
        }
        return { ...v, render_status: "rendering" as const };
      });
    },
    [data],
  );

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
        markVariantRendering(variantId, prevUrl);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to update variant");
        refetch();
      }
    },
    [markVariantRendering, refetch],
  );

  // Instructed items: filming_guide present + instruction_level != "none".
  // These use ShotSlotUploader. Uninstructed items keep the legacy pool upload.
  const isInstructed =
    (item?.filming_guide?.length ?? 0) > 0 && item?.instruction_level !== "none";

  // Legacy pool upload handler (uninstructed items only).
  async function handleFiles(files: FileList | null) {
    if (!files || files.length === 0 || isInstructed) return;
    setUploading(true);
    setError(null);
    conformancePolls.current = 0;
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
      const newPaths = urls.map((u) => u.gcs_path);
      const pathsToAttach = [...(item?.clip_gcs_paths ?? []), ...newPaths];
      const { attachClips: attach } = await import("@/lib/plan-api");
      await attach(itemId, pathsToAttach);
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
      <LightShell size="narrow">
        <SignInPrompt
          callbackUrl={`/plan/items/${itemId}`}
          title="Sign in to continue"
          subtitle="We use your Google account to save your clips and renders."
        />
      </LightShell>
    );
  }

  if (loading) {
    return (
      <LightShell size="narrow">
        <p className="py-24 text-center text-[#71717a]">Loading…</p>
      </LightShell>
    );
  }

  if (item === null) {
    return (
      <LightShell size="narrow">
        <div className="motion-safe:animate-fade-up py-24 text-center">
          <p className="mb-6 text-[#71717a]">We couldn&apos;t find that idea.</p>
          <Link href="/plan">
            <InkButton>Back to your plan</InkButton>
          </Link>
        </div>
      </LightShell>
    );
  }

  const clipCount = item.clip_gcs_paths.length;
  const isGenerating = item.status === "generating";
  const focused = variants.find((v) => v.variant_id === focusedVariantId) ?? null;
  const focusedEditable =
    focused && (!!focused.output_url || focused.render_status === "failed");
  const showResults = isGenerating || variants.length > 0;

  const currentPhase =
    data?.job?.current_phase ??
    (!data?.job?.started_at ? "queued" : null);
  const theaterIsTerminal = !!(item && isTerminalFn({ item, job: data?.job ?? null }));
  const theaterIsSuccess = item?.status === "ready";

  return (
    <LightShell size="wide">
      {/* @font-face for style-preview chips */}
      <style dangerouslySetInnerHTML={{ __html: FONT_FACES }} />
      <div className="motion-safe:animate-fade-up">
        {/* ── Editorial header + controls ── */}
        <div className="max-w-2xl">
          <Link
            href="/plan"
            className="text-sm text-[#71717a] underline-offset-2 transition-colors hover:text-[#0c0c0e]"
          >
            ← back to plan
          </Link>
          <div className="mb-1 mt-4 flex items-center gap-3">
            <span className="rounded bg-zinc-100 px-2 py-0.5 text-xs text-[#71717a]">
              Day {item.day_index}
            </span>
          </div>
          <h1 className="font-display text-3xl text-[#0c0c0e]">{item.theme}</h1>
          <p className="mb-4 mt-2 text-[#3f3f46]">{item.idea}</p>

          {/* ── FILM CARD (D5: primary action, above "Why this works") ── */}
          {isInstructed ? (
            <ShotSlotUploader
              item={item}
              onAttached={(updated) => {
                conformancePolls.current = 0;
                // Merge updated item into polling data without waiting for a refetch.
                refetch();
              }}
              onBusyChange={setUploaderBusy}
            />
          ) : (
            <>
              {/* Uninstructed: legacy pool upload section (unchanged) */}
              {item.filming_suggestion ? (
                <p className="mb-4 text-sm text-[#71717a]">📋 {item.filming_suggestion}</p>
              ) : null}
              {error && (
                <div className="mb-6 rounded border border-zinc-200 bg-[#fafaf8] px-4 py-3 text-[#3f3f46]">
                  {error}
                </div>
              )}
              <section className="mb-8 rounded-xl border border-zinc-200 bg-white p-5">
                <h2 className="mb-2 text-sm font-semibold text-[#0c0c0e]">Themed clips</h2>
                <p className="mb-4 text-sm text-[#71717a]">
                  {`Upload footage for this idea. ${clipCount > 0 ? `${clipCount} uploaded.` : "None yet."}`}
                </p>
                <label className="block">
                  <span className="sr-only">Upload video clips for this idea</span>
                  <input
                    type="file"
                    accept="video/mp4,video/quicktime"
                    multiple
                    disabled={uploading}
                    onChange={(e) => handleFiles(e.target.files)}
                    className="block w-full text-sm text-[#71717a] file:mr-3 file:rounded-full file:border-0 file:bg-[#0c0c0e] file:px-4 file:py-2 file:text-sm file:font-medium file:text-white hover:file:opacity-80"
                  />
                </label>
                {uploading && <p className="mt-3 text-sm text-lime-700">Uploading…</p>}
              </section>
            </>
          )}

          {/* Generate button (D6: also disabled while ShotSlotUploader has in-flight uploads) */}
          <InkButton
            onClick={handleGenerate}
            disabled={generating || clipCount === 0 || isGenerating || uploaderBusy}
          >
            {isGenerating
              ? "Generating…"
              : generating
                ? "Starting…"
                : uploaderBusy
                  ? `Finishing upload…`
                  : "Generate videos"}
          </InkButton>
          {clipCount === 0 && !uploaderBusy && (
            <p className="mt-2 text-sm text-[#a1a1aa]">
              {isInstructed
                ? "You can generate with any shots filled — more footage means better edits."
                : "Upload at least one clip first."}
            </p>
          )}
          {uploaderBusy && (
            <p className="mt-2 text-sm text-[#a1a1aa]">Finishing upload…</p>
          )}

          {/* Conformance verdict panel — display-only, never blocks Generate */}
          {item.conformance && (
            <ConformanceVerdictPanel conformance={item.conformance} />
          )}

          {/* "Why this works" — D5: moved below the film card + Generate */}
          {item.rationale && (
            <div className="mb-4 mt-6 rounded-lg border border-zinc-200 bg-white p-4">
              <p className="mb-1 text-xs font-medium text-lime-700">Why this works</p>
              <p className="text-sm text-[#3f3f46]">{stripRationalePrefix(item.rationale)}</p>
            </div>
          )}

          {/* ProgressTheater — light tone */}
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
                tone="light"
              >
                {null}
              </ProgressTheater>
            </div>
          )}
          {isGenerating && (
            <p className="mt-1 text-xs text-[#a1a1aa]">
              Usually 2–3 minutes. You can leave this page — we&apos;ll keep rendering.
            </p>
          )}
          {item.status === "failed" && variants.length === 0 && (
            <p className="mt-2 text-sm text-[#71717a]">
              Generation failed before any variant rendered. Try generating again.
            </p>
          )}
        </div>

        {/* ── Results: focused player + filmstrip + editor ── */}
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
                  className="mt-3 inline-flex min-h-11 w-full items-center justify-center rounded-full border border-zinc-200 px-5 py-2 text-sm text-[#3f3f46] transition-colors hover:border-zinc-400"
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
                  onChangeLayout={(layout) =>
                    runEdit(focused.variant_id, focused.output_url, () =>
                      editPlanItemVariant(itemId, focused.variant_id, {
                        intro_layout: layout,
                      }),
                    )
                  }
                />
              ) : (
                isGenerating && (
                  <p className="text-sm text-[#71717a]">
                    Edit controls unlock as soon as a variant finishes rendering.
                  </p>
                )
              )}
              {item.current_job_id && !isGenerating && (
                <div className="border-t border-zinc-200 pt-4">
                  <p className="text-xs font-semibold uppercase tracking-wide text-[#a1a1aa]">
                    How&apos;s this one?
                  </p>
                  <FeedbackButtons jobId={item.current_job_id} initialSignal={null} />
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </LightShell>
  );
}

/** Large hero player for the focused variant. */
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
    <div className="relative aspect-[9/16] w-full overflow-hidden rounded-xl border border-zinc-200 bg-zinc-100">
      {variant.output_url ? (
        <video src={variant.output_url} controls className="h-full w-full object-contain" />
      ) : failed ? (
        <div className="flex h-full items-center justify-center px-4 text-center text-sm text-red-600">
          This variant failed — try editing again.
        </div>
      ) : (
        <div className="flex h-full items-center justify-center text-sm text-[#71717a]">
          {generating ? "Rendering…" : "No preview yet"}
        </div>
      )}
      {rendering && variant.output_url && (
        <div
          className="absolute inset-0 flex items-center justify-center bg-white/70 text-sm text-lime-700"
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
    <div className="aspect-[9/16] w-full motion-safe:animate-shimmer rounded-xl border border-zinc-200 bg-[length:200%_100%] bg-gradient-to-r from-zinc-100 via-zinc-200 to-zinc-100" />
  );
}

function slugify(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}

// ── Conformance verdict panel ──────────────────────────────────────────────────
// Display-only: shows the ConformanceFeedbackAgent's verdict after clip attach.
// Never disables or blocks the Generate button — purely informational.

const VERDICT_STYLE: Record<
  "on_track" | "minor_drift" | "off_brief",
  { border: string; badge: string; label: string }
> = {
  on_track: {
    border: "border-lime-200",
    badge: "bg-lime-100 text-lime-800",
    label: "On track",
  },
  minor_drift: {
    border: "border-amber-200",
    badge: "bg-amber-100 text-amber-800",
    label: "Minor drift",
  },
  off_brief: {
    border: "border-red-200",
    badge: "bg-red-50 text-red-700",
    label: "Off brief",
  },
};

function ConformanceVerdictPanel({ conformance }: { conformance: ConformanceVerdict }) {
  const style = VERDICT_STYLE[conformance.verdict] ?? VERDICT_STYLE.off_brief;
  return (
    <div
      className={`mb-6 rounded-lg border ${style.border} bg-white p-4`}
      data-testid="conformance-verdict-panel"
    >
      <div className="mb-2 flex items-center gap-2">
        <span className={`rounded px-2 py-0.5 text-xs font-semibold ${style.badge}`}>
          {style.label}
        </span>
        <p className="text-sm text-[#3f3f46]">{conformance.summary}</p>
      </div>
      {conformance.mismatches && conformance.mismatches.length > 0 && (
        <ul className="mb-2 space-y-0.5">
          {conformance.mismatches.map((m, i) => (
            <li key={i} className="text-xs text-[#71717a]">
              &bull; {m}
            </li>
          ))}
        </ul>
      )}
      {conformance.suggestions && conformance.suggestions.length > 0 && (
        <ul className="space-y-0.5">
          {conformance.suggestions.map((s, i) => (
            <li key={i} className="text-xs text-lime-700">
              &rarr; {s}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
