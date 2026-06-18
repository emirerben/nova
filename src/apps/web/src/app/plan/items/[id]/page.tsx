"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  attachClips,
  changePlanItemStyle,
  dismissConformance,
  editPlanItemVariant,
  expandIdea,
  generatePlanItem,
  getPlanItem,
  getPlanItemJobStatus,
  NotAuthenticatedError,
  setClipNote,
  updatePlanItem,
  type ClipAssignment,
  type ConformanceVerdict,
  type IdeaExpandProposal,
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
import {
  getGenerativeStyleSets,
  type GenerativeStyleSet,
  GENERATIVE_TERMINAL_STATUSES,
} from "@/lib/generative-api";
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
import PlanVariantEditor from "../../_components/PlanVariantEditor";
import SignInPrompt from "../../_components/SignInPrompt";
import { TimelineEditor } from "../../../generative/TimelineEditor";
import { useTimelineSession } from "../../../generative/useTimelineSession";
import FeedbackButtons from "../../../library/_components/FeedbackButtons";
import {
  useVariantEditSession,
  type VariantEditSession,
} from "@/lib/variant-editor/useVariantEditSession";
import { isInstantEditEligible } from "@/lib/variant-editor/eligibility";
import { IntroTextPreview } from "@/components/variant-editor/IntroTextPreview";
import { resolveIntroParams } from "@/components/variant-editor/resolve-intro-params";
import { EditToolbar } from "@/components/variant-editor/EditToolbar";
import type { EditDraft } from "@/lib/variant-editor/useVariantEditSession";

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
  // Idea-centric: "Expand with AI" proposal state.
  const [expandProposal, setExpandProposal] = useState<IdeaExpandProposal | null>(null);
  const [expanding, setExpanding] = useState(false);
  const [acceptingExpand, setAcceptingExpand] = useState(false);
  const [tracks, setTracks] = useState<MusicTrackSummary[]>([]);
  const [styleSets, setStyleSets] = useState<GenerativeStyleSet[]>([]);
  const [focusedVariantId, setFocusedVariantId] = useState<string | null>(null);
  // Ask Nova advisor panel: closed | opened normally | opened via "Tell Nova".
  const [askNova, setAskNova] = useState<null | "default" | "contest">(null);
  const pendingEdits = useRef<Map<string, { priorFinishedAt: string | null; sawRendering: boolean }>>(new Map());
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
      // If the job-level status is already terminal (processing_failed,
      // variants_failed, etc.) treat it as done regardless of any frozen
      // per-variant render_status.  A stuck "rendering" variant after a
      // terminal job is a backend data-integrity gap — it should not keep the
      // frontend polling forever.  The failed variant renders via the existing
      // "failed" UI branch.
      const jobTerminal =
        job?.status != null && GENERATIVE_TERMINAL_STATUSES.includes(job.status);
      const baseTerminal =
        (jobTerminal || !anyRendering) &&
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
        // Server confirms the re-render is running — record that we witnessed it.
        // NOTE: mutating the ref object inside useMemo is intentional. The Map
        // lives in a useRef (not reactive state) so this doesn't trigger a new
        // render, and the mutation is idempotent (false → true only), making it
        // safe even if React replays the memo under Concurrent Mode.
        if (v.render_status === "rendering") {
          pending.sawRendering = true;
          return v;
        }
        // Decide whether this "ready" / "failed" is the result of OUR edit.
        // A fresh render is detected when:
        //   (a) we already saw the variant pass through "rendering", OR
        //   (b) the server's render_finished_at timestamp advanced past what we
        //       captured at edit-submission time.
        // Without this guard, the first poll after submission can still return
        // the PRE-edit "ready" (the Celery task hasn't fired yet) and clear the
        // pin too early — leaving controls re-enabled while the render hasn't
        // actually run.  Mirrors the commitMarkerRef pattern in useVariantEditSession.
        const isFreshRender =
          pending.sawRendering ||
          (v.render_finished_at ?? null) !== pending.priorFinishedAt;
        if ((v.render_status === "ready" || v.render_status === "failed") && isFreshRender) {
          pendingEdits.current.delete(v.variant_id);
          return v;
        }
        // Pre-edit ready race window: keep forcing "rendering" so the poll
        // continues and controls stay disabled until the real render completes.
        // Safety valve: usePolledJobStatus has a 30-minute hard ceiling after
        // which the interval stops regardless of terminal state, so a stuck
        // pending entry is bounded and cannot spin the poll indefinitely.
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
    (variantId: string, priorFinishedAt: string | null) => {
      // Preserve sawRendering from a prior in-flight edit: if the user opens
      // the clip editor a second time while the first render is still running,
      // resetting sawRendering to false could trap the pin if the first render
      // already set it (the second edit hasn't fired yet so its "rendering"
      // poll hasn't been seen). Keep the existing flag and only update the
      // timestamp anchor.
      const existing = pendingEdits.current.get(variantId);
      pendingEdits.current.set(variantId, {
        priorFinishedAt,
        sawRendering: existing?.sawRendering ?? false,
      });
      refetch();
    },
    [refetch],
  );

  const runEdit = useCallback(
    async (variantId: string, prevFinishedAt: string | null, action: () => Promise<unknown>) => {
      setError(null);
      try {
        await action();
        markVariantRendering(variantId, prevFinishedAt);
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

  // "N shots left" caption under the Generate button.
  const totalShots = item.filming_guide?.length ?? 0;
  const filledShots = item.clip_assignments?.filter((a) => a.shot_id !== null).length ?? 0;
  const shotsLeft = Math.max(0, totalShots - filledShots);

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

        {/* ── Two-pane grid: LEFT = shot checklist | RIGHT = sticky action panel ── */}
        <div className="lg:grid lg:grid-cols-[1fr_400px] lg:gap-10 lg:items-start">

          {/* LEFT: back link + editorial header + uploader + progress */}
          <div>
            <Link
              href="/plan"
              className="text-sm text-[#71717a] underline-offset-2 transition-colors hover:text-[#0c0c0e]"
            >
              ← back to plan
            </Link>
            {item.day_index != null && (
              <div className="mb-1 mt-4 flex items-center gap-3">
                <span className="rounded bg-zinc-100 px-2 py-0.5 text-xs text-[#71717a]">
                  Day {item.day_index}
                </span>
              </div>
            )}
            <h1 className="font-display mt-4 text-3xl text-[#0c0c0e]">
              {item.theme ?? item.idea}
            </h1>
            {item.theme && <p className="mb-2 mt-2 text-[#3f3f46]">{item.idea}</p>}

            {/* Notes textarea — editable, saves on blur */}
            <textarea
              defaultValue={item.notes ?? ""}
              onBlur={async (e) => {
                const val = e.currentTarget.value.trim() || null;
                if (val !== (item.notes ?? null)) {
                  await updatePlanItem(item.id, { notes: val ?? undefined }).catch(() => null);
                  refetch();
                }
              }}
              placeholder="Add notes…"
              rows={2}
              className="mb-4 mt-2 w-full resize-none rounded-lg border border-zinc-200 bg-transparent px-3 py-2 text-sm text-[#3f3f46] placeholder-zinc-400 focus:border-zinc-400 focus:outline-none"
            />

            {/* Expand with AI — only for un-expanded ideas (no theme yet, no clips) */}
            {!item.theme && item.clip_gcs_paths.length === 0 && !expandProposal && (
              <div className="mb-4">
                <button
                  type="button"
                  disabled={expanding}
                  onClick={async () => {
                    setExpanding(true);
                    try {
                      const proposal = await expandIdea(item.id);
                      setExpandProposal(proposal);
                    } catch {
                      /* swallow — user can retry */
                    } finally {
                      setExpanding(false);
                    }
                  }}
                  className="flex items-center gap-1.5 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#71717a] transition-colors hover:border-lime-400 hover:text-lime-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <span aria-hidden>✦</span>
                  {expanding ? "Thinking…" : "Expand with AI"}
                </button>
              </div>
            )}

            {/* Expand proposal card */}
            {expandProposal && (
              <div className="mb-4 rounded-xl border border-lime-200 bg-lime-50 p-4">
                <p className="text-[11px] font-semibold uppercase tracking-[.15em] text-lime-700">
                  AI suggestion
                </p>
                <p className="mt-1 font-display text-lg font-medium text-[#0c0c0e]">
                  {expandProposal.theme}
                </p>
                {expandProposal.filming_suggestion && (
                  <p className="mt-1 text-sm text-[#3f3f46]">{expandProposal.filming_suggestion}</p>
                )}
                {expandProposal.rationale && (
                  <p className="mt-2 text-xs text-[#71717a]">{expandProposal.rationale}</p>
                )}
                <div className="mt-3 flex gap-2">
                  <button
                    type="button"
                    disabled={acceptingExpand}
                    onClick={async () => {
                      setAcceptingExpand(true);
                      try {
                        await updatePlanItem(item.id, {
                          theme: expandProposal.theme,
                          filming_suggestion: expandProposal.filming_suggestion,
                        });
                        setExpandProposal(null);
                        refetch();
                      } catch {
                        /* swallow */
                      } finally {
                        setAcceptingExpand(false);
                      }
                    }}
                    className="rounded-lg bg-lime-600 px-4 py-1.5 text-[12px] font-semibold text-white hover:bg-lime-700 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {acceptingExpand ? "Saving…" : "Accept"}
                  </button>
                  <button
                    type="button"
                    onClick={() => setExpandProposal(null)}
                    className="rounded-lg border border-zinc-200 bg-white px-4 py-1.5 text-[12px] text-[#71717a] hover:border-zinc-400"
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            )}

            {/* Uploader — instructed: shot-slot guide; uninstructed: pool card */}
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
                {item.filming_suggestion ? (
                  <p className="mb-4 text-sm text-[#71717a]">{item.filming_suggestion}</p>
                ) : null}
                <PoolUploadCard
                  clips={item.clip_assignments ?? []}
                  uploading={uploading}
                  onFiles={handleFiles}
                  onKeep={keepUninstructedMatch}
                  onRemove={removeUninstructedClip}
                  onNoteChange={saveUninstructedNote}
                />
              </>
            )}

            {/* Error banner — outside the fork so it shows on both item types */}
            {error && (
              <div className="mb-6 rounded border border-zinc-200 bg-white px-4 py-3 text-sm text-[#3f3f46]">
                {error}
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

          {/* RIGHT: sticky action panel — preview + Nova helper + Generate */}
          <div className="mt-8 space-y-4 lg:sticky lg:top-6 lg:mt-0">
            {/* Small preview — shows the latest result or skeleton before generation */}
            <div className="mx-auto max-w-[200px]">
              <Hero variant={focused} generating={isGenerating} />
            </div>
            {item.edit_format && (
              <div className="mb-2">
                <span className="inline-flex items-center gap-1 rounded-full bg-zinc-800 px-2 py-0.5 text-xs text-zinc-400">
                  {item.edit_format}
                </span>
              </div>
            )}


            {/* Nova helper — one quiet line; expands to AskNovaPanel on request.
                Collapses conformance critic + Ask Nova into a single surface. */}
            <NovaHelper
              item={item}
              conformanceChecking={conformanceChecking}
              askNova={askNova}
              onOpen={() => setAskNova("default")}
              onContest={() => setAskNova("contest")}
              onClose={() => setAskNova(null)}
              onDismissConformance={async () => {
                try {
                  await dismissConformance(itemId);
                } finally {
                  refetch();
                }
              }}
              onItemChanged={() => {
                conformancePolls.current = 0;
                refetch();
              }}
            />

            {/* Generate + "N shots left" caption */}
            <div className="space-y-2">
              <InkButton
                onClick={handleGenerate}
                disabled={generating || clipCount === 0 || isGenerating || uploaderBusy}
              >
                {isGenerating
                  ? "Generating…"
                  : generating
                    ? "Starting…"
                    : uploaderBusy
                      ? "Finishing upload…"
                      : "Generate videos"}
              </InkButton>
              <p className="text-center text-sm text-[#a1a1aa]">
                {uploaderBusy
                  ? "Finishing upload…"
                  : clipCount === 0
                    ? "Add clips to generate"
                    : isInstructed && shotsLeft > 0
                      ? `${shotsLeft} shot${shotsLeft !== 1 ? "s" : ""} left`
                      : null}
              </p>
            </div>
          </div>
        </div>

        {/* ── Results: Hero + rail layout ── */}
        {/* FocusedResults owns the edit session and renders the hero+rail layout.
            The hero shows the active variant; the rail shows alternates + rationale
            + editor row. Keyed by variant_id so switching the focused variant
            remounts → fresh session (no stale draft over the new video). */}
        {showResults && (
          <FocusedResults
            key={focused?.variant_id ?? "pending"}
            itemId={itemId}
            item={item}
            variant={focused}
            variants={variants}
            focusedVariantId={focusedVariantId}
            onFocus={setFocusedVariantId}
            tracks={tracks}
            styleSets={styleSets}
            isGenerating={isGenerating}
            refetch={refetch}
            markVariantRendering={markVariantRendering}
            onSwap={
              focused
                ? (trackId) =>
                    runEdit(focused.variant_id, focused.render_finished_at ?? null, () =>
                      swapPlanItemSong(itemId, focused.variant_id, trackId),
                    )
                : async () => {}
            }
            onRetext={
              focused
                ? (text) =>
                    runEdit(focused.variant_id, focused.render_finished_at ?? null, () =>
                      retextPlanItem(itemId, focused.variant_id, { text }),
                    )
                : async () => {}
            }
            onRemoveText={
              focused
                ? () =>
                    runEdit(focused.variant_id, focused.render_finished_at ?? null, () =>
                      retextPlanItem(itemId, focused.variant_id, { remove: true }),
                    )
                : async () => {}
            }
            onChangeStyle={
              focused
                ? (styleSetId) =>
                    runEdit(focused.variant_id, focused.render_finished_at ?? null, () =>
                      changePlanItemStyle(itemId, focused.variant_id, styleSetId),
                    )
                : async () => {}
            }
            onResize={
              focused
                ? (px) =>
                    runEdit(focused.variant_id, focused.render_finished_at ?? null, () =>
                      setPlanItemIntroSize(itemId, focused.variant_id, px),
                    )
                : async () => {}
            }
            onChangeLayout={
              focused
                ? (layout) =>
                    runEdit(focused.variant_id, focused.render_finished_at ?? null, () =>
                      editPlanItemVariant(itemId, focused.variant_id, {
                        intro_layout: layout,
                      }),
                    )
                : async () => {}
            }
          />
        )}
      </div>
    </LightShell>
  );
}

// ── Variant rationale (client-only, no LLM) ─────────────────────────────────
// Maps text_mode + track_title to a 1-2 sentence blurb shown below the hero.
function deriveRationale(variant: PlanItemVariant, totalVariants: number): string {
  const track = variant.track_title ?? null;
  if (variant.text_mode === "lyrics" && track) return `Beat-synced to ${track}.`;
  if (variant.text_mode === "lyrics") return "Beat-synced lyrics overlay.";
  if (variant.text_mode === "agent_text" && track) return `Styled text over ${track}.`;
  if (variant.text_mode === "agent_text") return "Nova-written intro, your original audio.";
  if (variant.text_mode === "none") return "Your original audio, kept.";
  return `Nova generated ${totalVariants} edit${totalVariants !== 1 ? "s" : ""}.`;
}

// ── Editor panel tabs ────────────────────────────────────────────────────────
type EditorTab = "text" | "font" | "song" | "clips";

const EDITOR_TABS: { id: EditorTab; icon: string; label: string }[] = [
  { id: "text", icon: "T", label: "Text" },
  { id: "font", icon: "Aa", label: "Font" },
  { id: "song", icon: "♫", label: "Song" },
  { id: "clips", icon: "✂", label: "Clips" },
];

/**
 * Owns the focused variant's edit session and renders the Hero + rail layout.
 *
 * Layout:
 *   HERO — large 9/16 video player (active variant). "Nova's pick" lime badge
 *   on variants[0]; text_mode label pill below the video.
 *
 *   RIGHT (desktop) / BELOW (mobile):
 *     Rationale blurb (1-2 sentences derived from text_mode + track_title)
 *     Alternates row — small thumbnails for the other ready variants
 *     Editor row — 4 icon+label buttons that reveal PlanVariantEditor inline
 *     Download button + feedback
 *
 * DEFERRED-BURN model: for an instant-edit-eligible variant the session is the
 * draft store. Caption / Text size / Layout / Style controls mutate that draft
 * with ZERO network; the hero is the text-free base video + a live
 * IntroTextPreview overlay. The single FFmpeg bake fires only on Download.
 *
 * INELIGIBLE variants keep the legacy behavior: burned output_url in the hero +
 * PlanVariantEditor controls that re-render server-side per field.
 *
 * Keyed by variant_id in the parent so the edit session resets when the user
 * focuses a different variant — never showing variant A's draft over variant B.
 */
function FocusedResults({
  itemId,
  item,
  variant,
  variants,
  focusedVariantId,
  onFocus,
  tracks,
  styleSets,
  isGenerating,
  refetch,
  markVariantRendering,
  onSwap,
  onRetext,
  onRemoveText,
  onChangeStyle,
  onResize,
  onChangeLayout,
}: {
  itemId: string;
  item: PlanItem;
  variant: PlanItemVariant | null;
  variants: PlanItemVariant[];
  focusedVariantId: string | null;
  onFocus: (id: string) => void;
  tracks: MusicTrackSummary[];
  styleSets: GenerativeStyleSet[];
  isGenerating: boolean;
  refetch: () => void;
  markVariantRendering: (variantId: string, priorFinishedAt: string | null) => void;
  onSwap: (trackId: string) => Promise<void>;
  onRetext: (text: string) => Promise<void>;
  onRemoveText: () => Promise<void>;
  onChangeStyle: (styleSetId: string) => Promise<void>;
  onResize: (textSizePx: number) => Promise<void>;
  onChangeLayout: (layout: "linear" | "cluster") => Promise<void>;
}) {
  const [activeTab, setActiveTab] = useState<EditorTab | null>(null);

  // ── Deferred-burn session — eligible variants only ──────────────────────────
  // Use a stable no-op variant when nothing is focused yet (pre-first-render).
  const stableVariant: PlanItemVariant = variant ?? {
    variant_id: "__pending__",
    output_url: null,
    render_status: null,
    text_mode: "none",
    style_set_id: null,
    intro_text_size_px: null,
  };

  const editSession = useVariantEditSession(stableVariant, async (payload) => {
    if (!variant) return;
    await editPlanItemVariant(itemId, variant.variant_id, payload);
    refetch();
  });
  const instantEligible = variant ? isInstantEditEligible(variant) : false;

  useEffect(() => {
    if (instantEligible && !editSession.isEditing) editSession.enterEdit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instantEligible]);

  useEffect(() => {
    if (!editSession.isSaving) return;
    const t = setInterval(refetch, 2000);
    return () => clearInterval(t);
  }, [editSession.isSaving, refetch]);

  const pendingDownloadRef = useRef(false);
  const downloadName = `nova-${slugify(item.theme ?? "") || itemId.slice(0, 8)}.mp4`;

  useEffect(() => {
    if (!pendingDownloadRef.current) return;
    if (editSession.isSaving) return;
    if (variant?.render_status === "ready" && variant.output_url) {
      pendingDownloadRef.current = false;
      downloadVideo(variant.output_url, downloadName);
    } else if (variant?.render_status === "failed") {
      pendingDownloadRef.current = false;
    }
  }, [editSession.isSaving, variant?.render_status, variant?.output_url, downloadName]);

  const baking = instantEligible && (editSession.isSaving || pendingDownloadRef.current);

  const handleDownload = useCallback(() => {
    if (!variant) return;
    if (!variant.output_url && !editSession.isDirty) return;
    if (instantEligible && editSession.isDirty) {
      pendingDownloadRef.current = true;
      void editSession.commit();
      return;
    }
    if (variant.output_url) downloadVideo(variant.output_url, downloadName);
  }, [variant, editSession, instantEligible, downloadName]);

  // Alternates: the non-focused ready variants (up to 3 shown as small thumbs)
  const alternates = variants.filter((v) => v.variant_id !== focusedVariantId);
  // "Nova's pick" is always the first variant (index 0 in the variants array)
  const isNovaPick = variant != null && variants.length > 0 && variants[0].variant_id === variant.variant_id;

  // Text-mode label for the pill below the hero
  const TEXT_MODE_PILL: Record<string, string> = {
    lyrics: "With lyrics",
    agent_text: "Original audio",
    none: "Original audio",
  };
  const modePill = variant ? (TEXT_MODE_PILL[variant.text_mode] ?? "Original audio") : null;

  // The editor panel reveals PlanVariantEditor filtered to the active tab.
  // We keep one PlanVariantEditor instance and use the tab to scroll/focus.
  const focusedEditable = variant && (!!variant.output_url || variant.render_status === "failed");

  return (
    <div className="mt-8">
      {/* Hero + rail: on desktop they are side-by-side */}
      <div className="flex flex-col gap-6 lg:flex-row lg:items-start">

        {/* ── HERO: large video player ── */}
        <div className="w-full shrink-0 sm:max-w-xs lg:w-[300px]">
          <div className="relative">
            {/* "Nova's pick" badge */}
            {isNovaPick && variant?.output_url && (
              <span className="absolute left-3 top-3 z-10 rounded-full border border-lime-300 bg-lime-50 px-2.5 py-0.5 text-[11px] font-semibold text-lime-800">
                Nova&apos;s pick
              </span>
            )}
            {instantEligible && variant ? (
              <LiveEditPreview
                variant={variant}
                styleSets={styleSets}
                session={editSession}
                playToken={editSession.playToken}
              />
            ) : (
              <Hero variant={variant} generating={isGenerating} />
            )}
          </div>
          {/* Text-mode pill below video */}
          {modePill && !isGenerating && (
            <div className="mt-2 flex justify-center">
              <span className="rounded-full border border-zinc-200 bg-white px-3 py-0.5 text-xs text-[#71717a]">
                {modePill}
              </span>
            </div>
          )}
        </div>

        {/* ── RAIL: rationale + alternates + editor ── */}
        <div className="min-w-0 flex-1 space-y-5">

          {/* Rationale blurb */}
          {variant && !isGenerating && (
            <p className="text-sm text-[#3f3f46]">
              {deriveRationale(variant, variants.length)}
            </p>
          )}
          {isGenerating && (
            <p className="text-sm text-[#71717a]">
              Edit controls unlock as soon as a variant finishes rendering.
            </p>
          )}

          {/* Alternates row — small thumbnails, click to swap hero */}
          {alternates.length > 0 && (
            <div>
              <p className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-[#a1a1aa]">
                Other takes
              </p>
              <div className="flex gap-2">
                {alternates.slice(0, 3).map((v) => {
                  const altLabel: Record<string, string> = {
                    lyrics: "Lyrics",
                    agent_text: "AI text",
                    none: "Original",
                  };
                  const label = altLabel[v.text_mode] ?? "Edit";
                  const rendering = v.render_status === "rendering";
                  const failed = v.render_status === "failed";
                  return (
                    <button
                      key={v.variant_id}
                      type="button"
                      aria-label={`Switch to ${label} — ${v.track_title ?? "original audio"}`}
                      onClick={() => onFocus(v.variant_id)}
                      className="group relative aspect-[9/16] w-14 shrink-0 overflow-hidden rounded-lg border border-zinc-200 bg-zinc-100 transition-colors hover:border-zinc-400"
                    >
                      {v.output_url ? (
                        <video
                          src={v.output_url}
                          muted
                          preload="metadata"
                          className="h-full w-full object-cover"
                        />
                      ) : (
                        <div className="h-full w-full bg-zinc-200" />
                      )}
                      <span className="absolute inset-x-0 bottom-0 truncate bg-black/40 px-1 py-0.5 text-[8px] text-white">
                        {label}
                      </span>
                      {rendering && (
                        <span className="absolute inset-0 flex items-center justify-center bg-white/60 text-[10px] text-lime-700">
                          …
                        </span>
                      )}
                      {failed && (
                        <span className="absolute right-0.5 top-0.5 text-[10px]">⚠</span>
                      )}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* ── Editor row: 4 icon+label buttons ── */}
          {focusedEditable && (
            <div>
              <div className="flex gap-2">
                {EDITOR_TABS.map((tab) => {
                  // Hide Song tab when no song is swappable
                  if (tab.id === "song" && (tracks.length === 0 || !variant?.music_track_id)) return null;
                  // Hide Font tab for lyrics (font is locked to lyrics renderer)
                  if (tab.id === "font" && variant?.text_mode === "lyrics") return null;
                  const isActive = activeTab === tab.id;
                  return (
                    <button
                      key={tab.id}
                      type="button"
                      aria-pressed={isActive}
                      onClick={() => setActiveTab(isActive ? null : tab.id)}
                      className={`flex flex-col items-center gap-0.5 rounded-xl border px-3 py-2 text-center transition-colors ${
                        isActive
                          ? "border-lime-600 bg-lime-50 text-lime-800"
                          : "border-zinc-200 bg-white text-[#3f3f46] hover:border-zinc-400"
                      }`}
                    >
                      <span className="text-sm font-semibold leading-none">{tab.icon}</span>
                      <span className="text-[10px] leading-tight">{tab.label}</span>
                    </button>
                  );
                })}
              </div>

              {/* Inline editor panel — slides open below the tab row */}
              {activeTab !== null && variant && (
                <div className="mt-3">
                  <FocusedVariantControls
                    itemId={itemId}
                    variant={variant}
                    tracks={tracks}
                    styleSets={styleSets}
                    session={editSession}
                    instantEligible={instantEligible}
                    baking={baking}
                    activeTab={activeTab}
                    refetch={refetch}
                    markVariantRendering={markVariantRendering}
                    onSwap={onSwap}
                    onRetext={onRetext}
                    onRemoveText={onRemoveText}
                    onChangeStyle={onChangeStyle}
                    onResize={onResize}
                    onChangeLayout={onChangeLayout}
                  />
                </div>
              )}
            </div>
          )}

          {/* Download button */}
          {variant && (instantEligible ? variant.base_video_url : variant.output_url) && (
            <>
              <button
                type="button"
                onClick={handleDownload}
                disabled={baking}
                className="inline-flex min-h-11 w-full items-center justify-center rounded-full bg-[#0c0c0e] px-5 py-2 text-sm font-semibold text-white transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {baking ? "Preparing your video…" : "Download"}
              </button>
              {instantEligible && editSession.isDirty && !baking && (
                <p className="mt-1 text-center text-xs text-[#a1a1aa]">
                  Unsaved — downloads will include your changes
                </p>
              )}
            </>
          )}

          {/* Feedback */}
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
    </div>
  );
}

/**
 * Controls-only column for the focused variant. Receives the edit session as a
 * prop (the parent owns it, keyed by variant_id) — it does NOT create one.
 *
 * `activeTab` controls which section of PlanVariantEditor is surfaced. The
 * "text" tab shows caption/size/layout/style; "font" shows the EditToolbar font
 * controls (instant-edit variants only); "song" shows the song-swap picker;
 * "clips" opens the timeline editor sheet.
 *
 * For an ELIGIBLE variant the Caption / Text size / Layout / Style controls are
 * re-pointed at the session draft (no render). Song + Clips keep their server
 * paths. An INELIGIBLE variant gets the original server handlers (per-field
 * re-render, legacy behavior).
 */
function FocusedVariantControls({
  itemId,
  variant,
  tracks,
  styleSets,
  session,
  instantEligible,
  baking,
  activeTab,
  refetch,
  markVariantRendering,
  onSwap,
  onRetext,
  onRemoveText,
  onChangeStyle,
  onResize,
  onChangeLayout,
}: {
  itemId: string;
  variant: PlanItemVariant;
  tracks: MusicTrackSummary[];
  styleSets: GenerativeStyleSet[];
  session: VariantEditSession;
  instantEligible: boolean;
  baking: boolean;
  activeTab: EditorTab;
  refetch: () => void;
  markVariantRendering: (variantId: string, priorFinishedAt: string | null) => void;
  onSwap: (trackId: string) => Promise<void>;
  onRetext: (text: string) => Promise<void>;
  onRemoveText: () => Promise<void>;
  onChangeStyle: (styleSetId: string) => Promise<void>;
  onResize: (textSizePx: number) => Promise<void>;
  onChangeLayout: (layout: "linear" | "cluster") => Promise<void>;
}) {
  const timeline = useTimelineSession(itemId, variant, refetch, "plan-item");

  // For an eligible variant, re-point the text/size/layout/style handlers at the
  // session draft (synchronous → resolved promise so PlanVariantEditor's `run()`
  // busy-wrapper completes immediately). Song + Clips stay on the server paths.
  const editorVariant =
    instantEligible && session.isEditing ? variantWithDraft(variant, session.draft) : variant;
  const draftHandlers = instantEligible
    ? {
        onRetext: async (text: string) => {
          session.setText(text);
        },
        onRemoveText: async () => {
          session.setRemoved(true);
        },
        onChangeStyle: async (styleSetId: string) => {
          session.setStyle(styleSetId);
        },
        onResize: async (px: number) => {
          session.setSize(px);
        },
        onChangeLayout: async (layout: "linear" | "cluster") => {
          session.setLayout(layout);
        },
      }
    : { onRetext, onRemoveText, onChangeStyle, onResize, onChangeLayout };

  // "clips" tab: open the timeline editor inline. The TimelineEditor is always
  // rendered (it's a sheet) but we auto-open it when the tab activates.
  // biome-ignore lint/correctness/useExhaustiveDependencies: intentional — open on tab change, not on every render
  useEffect(() => {
    if (activeTab === "clips" && timeline.entryVisible && !timeline.isEditorOpen) {
      timeline.openEditor();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

  const showTextSection = activeTab === "text";
  const showFontSection = activeTab === "font" && instantEligible;
  const showSongSection = activeTab === "song";
  // Clips: the TimelineEditor sheet handles itself; we just need the render.

  return (
    <>
      {/* Text tab: Caption + size + layout + style (no Song / no Clips) */}
      {showTextSection && (
        <PlanVariantEditor
          variant={baking ? { ...editorVariant, render_status: "rendering" } : editorVariant}
          tracks={[]}
          styleSets={instantEligible ? [] : styleSets}
          onSwap={onSwap}
          onRetext={draftHandlers.onRetext}
          onRemoveText={draftHandlers.onRemoveText}
          onChangeStyle={draftHandlers.onChangeStyle}
          onResize={instantEligible ? undefined : draftHandlers.onResize}
          onChangeLayout={draftHandlers.onChangeLayout}
          onEditClips={undefined}
          showClipEditor={false}
          clipSlotCount={null}
          hasClipEdits={false}
        />
      )}

      {/* Font tab: EditToolbar (instant-edit eligible variants only) */}
      {showFontSection && (
        <EditToolbar
          session={session}
          styleSets={[]}
          fallbackSizePx={variant.intro_text_size_px}
          resolvedParams={resolveIntroParams(variant, styleSets, session.draft)}
        />
      )}

      {/* Song tab: song picker only — a standalone SongPicker section */}
      {showSongSection && (
        <PlanVariantEditor
          variant={baking ? { ...editorVariant, render_status: "rendering" } : editorVariant}
          tracks={tracks}
          styleSets={[]}
          onSwap={onSwap}
          onRetext={async () => {}}
          onRemoveText={async () => {}}
          onChangeStyle={async () => {}}
          onResize={undefined}
          onChangeLayout={undefined}
          onEditClips={undefined}
          showClipEditor={false}
          clipSlotCount={null}
          hasClipEdits={timeline.hasUserEdits}
          hideSections={["caption", "size", "layout", "style", "clips"]}
        />
      )}

      {/* Timeline editor sheet — always rendered, opened when clips tab is active */}
      {timeline.isEditorOpen && (
        <TimelineEditor
          ownerId={itemId}
          variantId={variant.variant_id}
          base="plan-item"
          onClose={timeline.closeEditor}
          onRenderEnqueued={() => {
            timeline.onRenderEnqueued();
            markVariantRendering(variant.variant_id, variant.render_finished_at ?? null);
          }}
        />
      )}
    </>
  );
}

/**
 * Overlay the live edit draft onto the variant so PlanVariantEditor's controls
 * reflect the in-progress selection (the user's chosen caption / size / layout /
 * style) rather than the last-baked server values. Only the fields the editor
 * reads are touched; everything else (song, clips, render_status) passes through.
 */
function variantWithDraft(variant: PlanItemVariant, draft: EditDraft): PlanItemVariant {
  return {
    ...variant,
    intro_text: draft.removed ? "" : draft.text,
    text_mode: draft.removed ? "none" : variant.text_mode === "none" ? "agent_text" : variant.text_mode,
    style_set_id: draft.styleSetId ?? variant.style_set_id,
    intro_text_size_px: draft.sizePx ?? variant.intro_text_size_px,
    // A user-driven size shows as the explicit value (no "· auto" suffix).
    intro_size_source: draft.sizePx != null ? "user" : variant.intro_size_source,
    intro_layout: draft.layout ?? variant.intro_layout,
  };
}

/**
 * The LEFT-hero live preview for an eligible plan-item variant: the text-free
 * base video plays under a live DOM intro overlay; every control change (from
 * the RIGHT column) updates this preview at 0 network via the session draft.
 * Occupies the exact hero frame the burned-output Hero does. Light editorial
 * canvas (lime accent, cream/white tiles — never amber). The overlay is
 * non-editable: the user edits the caption via the RIGHT Caption control, not by
 * typing on the video.
 */
function LiveEditPreview({
  variant,
  styleSets,
  session,
  playToken,
}: {
  variant: PlanItemVariant;
  styleSets: GenerativeStyleSet[];
  session: VariantEditSession;
  playToken?: number;
}) {
  const introParams = resolveIntroParams(variant, styleSets, session.draft);

  // Pin the base-video src for the session: every poll re-signs the URL (new
  // query string), and swapping <video src> restarts playback. Fall forward to
  // the freshest signed URL only on a media error (expired sig mid-session).
  const baseSrcRef = useRef<string | null>(null);
  const [baseSrcNonce, setBaseSrcNonce] = useState(0);
  if (baseSrcRef.current === null && variant.base_video_url) {
    baseSrcRef.current = variant.base_video_url;
  }
  void baseSrcNonce; // re-render trigger only

  // Live layout follows the draft (so toggling Classic/Editorial re-lays the
  // overlay instantly), falling back to the variant's persisted layout.
  const previewLayout =
    (session.draft.layout ?? variant.intro_layout) === "cluster" ? "cluster" : "linear";

  return (
    <div className="relative aspect-[9/16] w-full overflow-hidden rounded-xl border border-zinc-200 bg-zinc-100">
      {baseSrcRef.current ? (
        <video
          src={baseSrcRef.current}
          controls
          loop
          autoPlay
          muted
          playsInline
          className="h-full w-full object-contain"
          onError={() => {
            if (
              variant.base_video_url &&
              baseSrcRef.current !== variant.base_video_url
            ) {
              baseSrcRef.current = variant.base_video_url;
              setBaseSrcNonce((n) => n + 1);
            }
          }}
        />
      ) : (
        <div className="flex h-full items-center justify-center text-sm text-[#71717a]">
          No preview
        </div>
      )}
      <IntroTextPreview params={introParams} editable={false} layout={previewLayout} playToken={playToken} />
    </div>
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
  // Pin the video src for the session lifetime.  Every 2s poll re-signs the GCS
  // URL with a fresh query string; swapping <video src> restarts playback.
  // Only advance on media error (expired sig in a very long session).
  const pinnedSrcRef = useRef<string | null>(null);
  if (variant?.output_url && pinnedSrcRef.current === null) {
    pinnedSrcRef.current = variant.output_url;
  }
  // Reset pin when switching to a different variant (different video entirely).
  const prevVariantIdRef = useRef<string | null>(null);
  if (variant?.variant_id !== prevVariantIdRef.current) {
    prevVariantIdRef.current = variant?.variant_id ?? null;
    pinnedSrcRef.current = variant?.output_url ?? null;
  }
  const videoSrc = pinnedSrcRef.current ?? variant?.output_url ?? null;

  if (!variant) return <SkeletonTile />;
  const rendering = variant.render_status === "rendering";
  const failed = variant.render_status === "failed";
  return (
    <div className="relative aspect-[9/16] w-full overflow-hidden rounded-xl border border-zinc-200 bg-zinc-100">
      {videoSrc ? (
        <video
          src={videoSrc}
          controls
          className="h-full w-full object-contain"
          onError={() => {
            // Expired signature — fall forward to the freshest signed URL.
            if (variant?.output_url && variant.output_url !== pinnedSrcRef.current) {
              pinnedSrcRef.current = variant.output_url;
            }
          }}
        />
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

// ── Nova helper ─────────────────────────────────────────────────────────────────
// One quiet line in the right action panel. Collapses the two pre-generate AI
// surfaces (conformance critic + Ask Nova) into a single lime-dot row.
// States: checking (pulse) → on-track → off-brief one-liner → default prompt.
// Expanding → AskNovaPanel (full advisor chat) replaces this row entirely.

function NovaHelper({
  item,
  conformanceChecking,
  askNova,
  onOpen,
  onContest,
  onClose,
  onDismissConformance,
  onItemChanged,
}: {
  item: PlanItem;
  conformanceChecking: boolean;
  askNova: null | "default" | "contest";
  onOpen: () => void;
  onContest: () => void;
  onClose: () => void;
  onDismissConformance: () => void;
  onItemChanged: () => void;
}) {
  // AskNovaPanel is the full-expanded state — it takes over the row entirely.
  if (askNova !== null) {
    return (
      <AskNovaPanel
        item={item}
        mode={askNova}
        onClose={onClose}
        onItemChanged={onItemChanged}
      />
    );
  }

  const c = item.conformance;
  // Reuse the same render gates as ConformanceVerdictPanel: dismissed,
  // suppressed, and low-confidence reads are silent.
  const hasVerdict =
    !!c?.verdict &&
    !c.dismissed &&
    !c.suppressed &&
    (c.confidence ?? 0) >= 0.6;

  return (
    <div role="status" aria-live="polite" className="space-y-1.5" data-testid="nova-helper">
      {conformanceChecking ? (
        <p className="flex items-start gap-2 text-sm text-[#71717a] motion-safe:animate-pulse">
          <span
            className="mt-1.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-lime-600"
            aria-hidden="true"
          />
          Reading your clips against the brief…
        </p>
      ) : hasVerdict && c!.verdict === "on_track" ? (
        <p className="flex items-start gap-2 text-sm text-[#3f3f46]">
          <span
            className="mt-1.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-lime-600"
            aria-hidden="true"
          />
          Looks on-brief.{" "}
          <button
            type="button"
            onClick={onOpen}
            className="font-medium text-lime-700 underline-offset-2 hover:underline"
          >
            Ask Nova ↗
          </button>
        </p>
      ) : hasVerdict ? (
        <div className="space-y-1">
          <p className="flex items-start gap-2 text-sm text-[#3f3f46]">
            <span
              className="mt-1.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-lime-600"
              aria-hidden="true"
            />
            <span>{c!.summary}</span>
          </p>
          <div className="flex gap-3 pl-3.5">
            <button
              type="button"
              onClick={onContest}
              className="text-xs font-medium text-lime-700 underline-offset-2 hover:underline"
            >
              Tell Nova
            </button>
            <button
              type="button"
              onClick={onDismissConformance}
              className="text-xs text-[#71717a] underline-offset-2 hover:underline"
            >
              Hide
            </button>
            <button
              type="button"
              onClick={onOpen}
              className="text-xs text-[#71717a] underline-offset-2 hover:underline"
            >
              Ask Nova ↗
            </button>
          </div>
        </div>
      ) : (
        <p className="flex items-start gap-2 text-sm text-[#71717a]">
          <span
            className="mt-1.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-lime-600"
            aria-hidden="true"
          />
          Not sure which clip fits?{" "}
          <button
            type="button"
            onClick={onOpen}
            className="font-medium text-lime-700 underline-offset-2 hover:underline"
          >
            Ask Nova ↗
          </button>
        </p>
      )}
    </div>
  );
}

// ── Pool upload card (uninstructed items) ────────────────────────────────────────
// Replaces the legacy inline <section> for items without a filming guide.
// Visually matches the shot-slot card: rounded-2xl, border-zinc-200, bg-white.
// Logic is identical to the old section — only the markup has been trimmed.

function PoolUploadCard({
  clips,
  uploading,
  onFiles,
  onKeep,
  onRemove,
  onNoteChange,
}: {
  clips: ClipAssignment[];
  uploading: boolean;
  onFiles: (files: FileList | null) => void;
  onKeep: (a: ClipAssignment) => void;
  onRemove: (a: ClipAssignment) => void;
  onNoteChange: (a: ClipAssignment, note: string) => Promise<void>;
}) {
  return (
    <div className="mb-8 rounded-2xl border border-zinc-200 bg-white p-5">
      {clips.length > 0 && (
        <ul className="mb-4 space-y-3">
          {clips.map((a) => {
            const raw = a.gcs_path.split("/").pop() ?? a.gcs_path;
            const name = raw.includes("-") ? raw.slice(raw.indexOf("-") + 1) : raw;
            return (
              <li
                key={a.gcs_path}
                className="border-b border-zinc-100 pb-3 last:border-0 last:pb-0"
              >
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
                      onClick={() => onKeep(a)}
                      className="shrink-0 text-xs font-medium text-lime-700 underline underline-offset-2 hover:text-lime-800"
                    >
                      Keep
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => onRemove(a)}
                    className="shrink-0 text-xs text-[#71717a] underline underline-offset-2 hover:text-[#0c0c0e]"
                  >
                    Remove
                  </button>
                </div>
                <ClipNoteControl
                  note={a.user_note ?? ""}
                  onSave={(note) => onNoteChange(a, note)}
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
          onChange={(e) => onFiles(e.target.files)}
          className="block w-full text-sm text-[#71717a] file:mr-3 file:rounded-full file:border-0 file:bg-[#0c0c0e] file:px-4 file:py-2 file:text-sm file:font-medium file:text-white hover:file:opacity-80"
        />
      </label>
      {uploading && <p className="mt-3 text-sm text-lime-700">Uploading…</p>}
    </div>
  );
}
