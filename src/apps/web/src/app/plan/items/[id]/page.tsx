"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  attachClips,
  changePlanItemStyle,
  dismissConformance,
  generatePlanItem,
  getPlanItem,
  getPlanItemJobStatus,
  NotAuthenticatedError,
  setClipNote,
  type ClipAssignment,
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
import ShotSlotUploader, { ClipNoteControl } from "./components/ShotSlotUploader";
import AskNovaPanel from "./components/AskNovaPanel";
import { getGenerativeStyleSets, type GenerativeStyleSet } from "@/lib/generative-api";
import { getMusicTracks, type MusicTrackSummary } from "@/lib/music-api";
import { FONT_FACES } from "@/lib/font-faces";
import { downloadVideo } from "@/lib/download-video";
import { variantFailureCopy } from "@/lib/variant-failure-copy";
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

// How long a dispatched render may take to register its Job before we admit
// failure. Celery pickup on a busy local worker regularly exceeds 10s; prod
// queue waits can too. Keep this comfortably above both.
const RENDER_REGISTER_TIMEOUT_MS = 45_000;
const RENDER_REGISTER_ERROR = "The render didn't register — give it another go.";

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
  // Ask Nova advisor panel: closed | opened normally | opened via "Tell Nova".
  const [askNova, setAskNova] = useState<null | "default" | "contest">(null);
  const pendingEdits = useRef<Map<string, { priorOutputUrl: string | null }>>(new Map());
  // Conformance polling: keep fetching for up to 3 extra cycles after clips are attached
  // so the verdict panel appears shortly after the async agent finishes (~6s window).
  const conformancePolls = useRef(0);
  // Render-start window: POST /generate dispatches a Celery task that mints the
  // Job AFTER the response — keep polling until current_job_id appears, or the
  // first click silently "does nothing" (dogfood). Time-based, not poll-count:
  // a busy worker can take >12s to pick the task up (second dogfood round: the
  // count-based window expired, showed the error, THEN the render started).
  const awaitingJobSince = useRef<number | null>(null);

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

      // Keep polling while a just-dispatched render hasn't minted its Job yet.
      if (item.current_job_id || item.status === "generating") {
        awaitingJobSince.current = null;
      } else if (
        awaitingJobSince.current !== null &&
        Date.now() - awaitingJobSince.current < RENDER_REGISTER_TIMEOUT_MS
      ) {
        return false;
      }

      // Keep polling for up to 3 extra cycles when the item has clips but no
      // conformance verdict yet (the async task may still be running).
      const hasClips = (item.clip_gcs_paths?.length ?? 0) > 0;
      const hasFilmingGuide = (item.filming_guide?.length ?? 0) > 0;
      // Gate on the absence of a VERDICT, not the conformance object — after a
      // note edit the carry-over stub ({contested:true}, no verdict) is truthy,
      // so the old `!item.conformance` check never resumed polling and the
      // re-read never appeared (review finding).
      const awaitingConformance =
        hasClips && hasFilmingGuide && !item.conformance?.verdict && conformancePolls.current < 3;
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
      // Pass full assignments (not bare paths) so existing clips keep their
      // user_note across an append — the bare-paths legacy form resets them.
      const assignments = [
        ...(item?.clip_assignments ?? []).map((a) => ({
          gcs_path: a.gcs_path,
          shot_id: a.shot_id,
          user_note: a.user_note ?? "",
        })),
        ...newPaths.map((p) => ({ gcs_path: p, shot_id: null, user_note: "" })),
      ];
      await attachClips(
        itemId,
        assignments.map((a) => a.gcs_path),
        assignments,
      );
      refetch();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  // ── Uninstructed clip actions (no-shot-list items: feedback #3 + pool Keep) ──

  async function saveUninstructedNote(a: ClipAssignment, note: string) {
    await setClipNote(itemId, a.gcs_path, note);
    conformancePolls.current = 0;
    refetch();
  }

  async function keepUninstructedMatch(a: ClipAssignment) {
    try {
      await saveUninstructedNote(a, a.user_note ?? "");
    } catch {
      setError("Couldn't keep that clip — try again.");
    }
  }

  async function removeUninstructedClip(a: ClipAssignment) {
    const remaining = (item?.clip_assignments ?? [])
      .filter((x) => x.gcs_path !== a.gcs_path)
      .map((x) => ({ gcs_path: x.gcs_path, shot_id: x.shot_id, user_note: x.user_note ?? "" }));
    try {
      await attachClips(
        itemId,
        remaining.map((x) => x.gcs_path),
        remaining,
      );
      refetch();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't remove that clip");
    }
  }

  async function handleGenerate() {
    setGenerating(true);
    setError(null);
    // Arm the wait window BEFORE the POST so the release-effect can't fire
    // early while the request is still in flight.
    awaitingJobSince.current = Date.now();
    try {
      await generatePlanItem(itemId);
      refetch();
    } catch (err) {
      awaitingJobSince.current = null;
      setError(err instanceof Error ? err.message : "Failed to start generation");
      setGenerating(false);
    }
  }

  // Release the Generate lock once the render registers (or the wait window
  // expires without a job — surface that instead of silently doing nothing).
  useEffect(() => {
    const registered = !!(item?.current_job_id || item?.status === "generating");
    if (registered) {
      // A registered render moots any earlier didn't-register complaint —
      // clear it even if it was shown in a previous attempt (dogfood: the
      // banner outlived the render it was wrong about).
      setError((prev) => (prev === RENDER_REGISTER_ERROR ? null : prev));
    }
    if (!generating) return;
    if (registered) {
      awaitingJobSince.current = null;
      setGenerating(false);
    } else if (
      awaitingJobSince.current !== null &&
      Date.now() - awaitingJobSince.current >= RENDER_REGISTER_TIMEOUT_MS &&
      data !== null
    ) {
      awaitingJobSince.current = null;
      setGenerating(false);
      setError(RENDER_REGISTER_ERROR);
    }
  }, [generating, item?.current_job_id, item?.status, data]);

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
  // Conformance in-flight: clips attached + guide present + verdict pending,
  // bounded by the poll window — resolves to the tile, the on-track line, or
  // (when guards skipped the run) silently vanishes. Never hangs.
  const conformanceChecking =
    clipCount > 0 &&
    (item.filming_guide?.length ?? 0) > 0 &&
    item.instruction_level !== "none" &&
    !item.conformance?.verdict &&
    conformancePolls.current < 3;
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
              <section className="mb-8 rounded-xl border border-zinc-200 bg-white p-5">
                <h2 className="mb-2 text-sm font-semibold text-[#0c0c0e]">Themed clips</h2>
                <p className="mb-4 text-sm text-[#71717a]">
                  {clipCount > 0
                    ? "The editor will use the best parts."
                    : "Upload footage for this idea. None yet."}
                </p>
                {(item.clip_assignments?.length ?? 0) > 0 && (
                  <ul className="mb-4 space-y-3">
                    {item.clip_assignments!.map((a) => {
                      const raw = a.gcs_path.split("/").pop() ?? a.gcs_path;
                      const name = raw.includes("-") ? raw.slice(raw.indexOf("-") + 1) : raw;
                      return (
                        <li key={a.gcs_path} className="border-b border-zinc-100 pb-3 last:border-0 last:pb-0">
                          <div className="flex items-center gap-3">
                            {a.machine_matched ? (
                              <span className="flex min-w-0 items-center gap-1 rounded border border-dashed border-lime-300 bg-white px-2 py-0.5 text-xs text-lime-800">
                                <span className="max-w-[180px] truncate">{name}</span>
                                <span className="shrink-0 text-lime-700">· Matched — keep?</span>
                              </span>
                            ) : (
                              <span className="flex min-w-0 items-center gap-1 rounded border border-lime-200 bg-lime-50 px-2 py-0.5 text-xs text-lime-800">
                                <span>✓</span>
                                <span className="max-w-[220px] truncate">{name}</span>
                              </span>
                            )}
                            {a.machine_matched && (
                              <button
                                type="button"
                                onClick={() => keepUninstructedMatch(a)}
                                className="shrink-0 text-xs font-medium text-lime-700 underline underline-offset-2 hover:text-lime-800"
                              >
                                Keep
                              </button>
                            )}
                            <button
                              type="button"
                              onClick={() => removeUninstructedClip(a)}
                              className="shrink-0 text-xs text-[#71717a] underline underline-offset-2 hover:text-[#0c0c0e]"
                            >
                              Remove
                            </button>
                          </div>
                          <ClipNoteControl
                            note={a.user_note ?? ""}
                            onSave={(note) => saveUninstructedNote(a, note)}
                          />
                        </li>
                      );
                    })}
                  </ul>
                )}
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

          {/* Error banner — outside the instructed/uninstructed fork so the
              render-register error (and any other) shows on BOTH item types
              (dogfood: it was trapped in the uninstructed branch, so the
              first-click fix never surfaced on filming-guide items). */}
          {error && (
            <div className="mb-6 rounded border border-zinc-200 bg-white px-4 py-3 text-sm text-[#3f3f46]">
              {error}
            </div>
          )}

          {/* Conformance read — ABOVE Generate so the read informs the action,
              with the explicit generate-anyway line so proximity never reads as
              a gate. State machine: checking shimmer → tile / one-liner / nothing. */}
          {conformanceChecking ? (
            <p
              className="mb-4 text-sm text-[#71717a] motion-safe:animate-pulse"
              role="status"
              aria-live="polite"
            >
              Reading your clips against the brief…
            </p>
          ) : (
            item.conformance?.verdict && (
              <ConformanceVerdictPanel
                conformance={item.conformance}
                onTellNova={() => setAskNova("contest")}
                onDismiss={async () => {
                  try {
                    await dismissConformance(itemId);
                  } finally {
                    refetch();
                  }
                }}
              />
            )
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

          {/* Ask Nova — collapsed trigger + bounded panel BELOW Generate (the
              page's primary action keeps its page). */}
          <div className="mt-4">
            {askNova === null ? (
              <button
                type="button"
                onClick={() => setAskNova("default")}
                className="text-sm font-medium text-lime-700 underline-offset-2 hover:underline"
              >
                Not sure which clip fits? Ask Nova
              </button>
            ) : (
              <AskNovaPanel
                item={item}
                mode={askNova}
                onClose={() => setAskNova(null)}
                onItemChanged={() => {
                  conformancePolls.current = 0;
                  refetch();
                }}
              />
            )}
          </div>

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
        <div className="flex h-full items-center justify-center px-4 text-center text-sm text-[#3f3f46]">
          {variantFailureCopy(variant.error_class)}
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

// ── Conformance verdict tile ────────────────────────────────────────────────────
// Display-only: never disables or blocks Generate. Redesigned per DESIGN.md §7-D10
// after the wrong-brief incident: dashed zinc (no red walls), a READ AGAINST
// evidence line so the user can SEE what was judged, advice voice, and real
// recourse ("Tell Nova" re-reads the clip; "Hide this read" dismisses).

const VERDICT_LABEL: Record<"minor_drift" | "off_brief", string> = {
  minor_drift: "Close — one tweak",
  off_brief: "Different from the brief",
};

function ConformanceVerdictPanel({
  conformance,
  onTellNova,
  onDismiss,
}: {
  conformance: ConformanceVerdict;
  onTellNova: () => void;
  onDismiss: () => void;
}) {
  // Render gates: dismissed/suppressed verdicts and low-confidence reads show
  // nothing — silence beats a read the user can't trust.
  if (conformance.dismissed || conformance.suppressed) return null;
  if ((conformance.confidence ?? 0) < 0.6) return null;

  if (conformance.verdict === "on_track") {
    return (
      <p
        className="mb-4 text-sm text-[#3f3f46]"
        role="status"
        aria-live="polite"
        data-testid="conformance-verdict-panel"
      >
        <span className="text-lime-700">✓</span> Looks on-brief.
      </p>
    );
  }

  const label = VERDICT_LABEL[conformance.verdict] ?? VERDICT_LABEL.off_brief;
  // Label promises "one tweak" for minor drift — the advice keeps that promise.
  const adviceCap = conformance.verdict === "minor_drift" ? 1 : 2;
  const advice = (conformance.suggestions ?? []).slice(0, adviceCap);

  return (
    <div
      className="mb-6 rounded-xl border border-dashed border-zinc-300 bg-white p-4"
      role="status"
      aria-live="polite"
      data-testid="conformance-verdict-panel"
    >
      {conformance.evaluated_theme && (
        <p className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#71717a]">
          Read against: &ldquo;{conformance.evaluated_theme}&rdquo;
        </p>
      )}
      <p className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#52525b]">
        {label}
      </p>
      <p className="text-sm text-[#0c0c0e]">{conformance.summary}</p>
      {advice.length > 0 && (
        <ul className="mt-1 space-y-0.5">
          {advice.map((s, i) => (
            <li key={i} className="text-sm text-[#3f3f46]">
              {s}
            </li>
          ))}
        </ul>
      )}
      <div className="mt-3 flex gap-4">
        <button
          type="button"
          onClick={onTellNova}
          className="text-xs font-medium text-lime-700 underline-offset-2 hover:underline"
        >
          Looks wrong? Tell Nova
        </button>
        <button
          type="button"
          onClick={onDismiss}
          className="text-xs text-[#71717a] underline-offset-2 hover:underline"
        >
          Hide this read
        </button>
      </div>
      <p className="mt-2 text-xs text-[#71717a]">
        You can generate anyway — this is just a read on the brief.
      </p>
    </div>
  );
}
