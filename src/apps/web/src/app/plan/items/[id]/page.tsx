"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { type Dispatch, type SetStateAction, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  attachClips,
  changePlanItemStyle,
  dismissConformance,
  editPlanItemVariant,
  expandIdea,
  generatePlanItem,
  generatePlanItemGuide,
  getPlanItem,
  getPlanItemJobStatus,
  NotAuthenticatedError,
  setClipNote,
  setItemVoiceover,
  setItemVoiceoverBedLevel,
  setItemVoiceoverCaptionStyle,
  type VoiceoverCaptionStyle,
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
  requestOverlayUploadUrls,
  setVariantMediaOverlays,
  type MediaOverlay,
  requestSfxUploadUrls,
  setVariantSoundEffects,
  renderVariantSfx,
  getSfxAudioUrl,
  putTextElements,
  type SoundEffectPlacement,
  type TextElement,
  type CaptionCue,
  setPlanItemCaptions,
  applyPlanItemCaptions,
  setPlanItemIntroTiming,
  patchPlanItemSceneTiming,
  type SceneTimingPatch,
} from "@/lib/plan-api";
import { useSfxPreview } from "../../_components/useSfxPreview";
import { VoiceRecorder } from "../../../generative/VoiceRecorder";
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
import { variantFailureCopy, unplacedShotCopy } from "@/lib/variant-failure-copy";
import { stripRationalePrefix } from "@/lib/plan-text";
import { GENERATIVE_PHASE_ORDER, GENERATIVE_PHASE_LABEL } from "@/lib/job-phases";
import { ProgressTheater, ShimmerSweep } from "@/components/progress";
import { StableVideo } from "@/components/StableVideo";
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";
import { LightShell } from "@/components/ui/LightShell";
import { InkButton } from "@/components/ui/InkButton";
import { SeedProvenanceBadge } from "../../_components/ui/SeedProvenanceBadge";
import CaptionEditor from "../../_components/CaptionEditor";
import PlanVariantEditor from "../../_components/PlanVariantEditor";
import SignInPrompt from "../../_components/SignInPrompt";
import UnifiedTimeline from "../../_components/UnifiedTimeline";
import { InlineClipsEditor } from "../../_components/InlineClipsEditor";
import { useClipTimeline } from "../../_components/useClipTimeline";
import { getSoundEffects, type SoundEffectSummary } from "@/lib/sfx-api";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import FeedbackButtons from "../../../library/_components/FeedbackButtons";
import {
  useVariantEditSession,
  type VariantEditSession,
} from "@/lib/variant-editor/useVariantEditSession";
import { isInstantEditEligible } from "@/lib/variant-editor/eligibility";
import { IntroTextPreview } from "@/components/variant-editor/IntroTextPreview";
import { resolveIntroParams } from "@/components/variant-editor/resolve-intro-params";
import { EditToolbar } from "@/components/variant-editor/EditToolbar";
import { resolveTextElementsLayout } from "@/lib/overlay-layout";
import type { EditDraft } from "@/lib/variant-editor/useVariantEditSession";

// How long a dispatched render may take to register its Job before we admit
// failure. Celery pickup on a busy local worker regularly exceeds 10s; prod
// queue waits can too. Keep this comfortably above both.
const RENDER_REGISTER_TIMEOUT_MS = 45_000;

// Kill-switch: overlays tab only appears when NEXT_PUBLIC_MEDIA_OVERLAYS_ENABLED=true.
// Normalise: accept "true", "True", "TRUE", "1" and trim whitespace so a
// near-miss Vercel value ("True", trailing space) doesn't silently hide the tab.
const _mediaOverlaysRaw = (process.env.NEXT_PUBLIC_MEDIA_OVERLAYS_ENABLED ?? "").trim();
const MEDIA_OVERLAYS_ENABLED =
  _mediaOverlaysRaw.toLowerCase() === "true" || _mediaOverlaysRaw === "1";
const SOUND_EFFECTS_ENABLED =
  process.env.NEXT_PUBLIC_SOUND_EFFECTS_ENABLED === "true";
const RENDER_REGISTER_ERROR = "The render didn't register — give it another go.";

// Shared by the interactive Fit/Fill toggle (pre-render) and the read-only
// applied-fit display (post-render).
const LANDSCAPE_FIT_OPTIONS: { value: "fit" | "fill"; label: string; desc: string }[] = [
  { value: "fit",  label: "Fit",  desc: "Keep horizontal, black bars top & bottom" },
  { value: "fill", label: "Fill", desc: "Crop to fill the vertical frame" },
];

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
  const [generatingGuide, setGeneratingGuide] = useState(false);
  const pendingEdits = useRef<Map<string, { priorFinishedAt: string | null; sawRendering: boolean }>>(new Map());
  // Incremented whenever pendingEdits is mutated so the variants memo re-runs
  // immediately (useMemo only tracks reactive dependencies; the ref itself is not reactive).
  const [editGeneration, setEditGeneration] = useState(0);
  // Tracks what kind of edit is in-flight for the focused variant so the Hero
  // overlay can show a meaningful label ("Applying your new song…" vs "Updating text…").
  const renderingAction = useRef<{ type: "song" | "text" | "style" | "other"; label: string } | null>(null);
  // Transient "✓ Updated" cue: set to the variantId for 4s when render_finished_at advances.
  const [updatedVariantId, setUpdatedVariantId] = useState<string | null>(null);
  // Narrated-walkthrough: local shadow of voiceover_gcs_path — updated optimistically
  // when VoiceRecorder fires onVoiceover; reset from item on refetch.
  const [voiceoverGcsPath, setVoiceoverGcsPath] = useState<string | null>(null);
  const [voiceoverSaving, setVoiceoverSaving] = useState(false);
  // Narrated-walkthrough: original-audio bed level (0 = voice only, 1 = loudest).
  // null = Nova's default. Optimistic local shadow; reset from item on refetch.
  const [bedLevel, setBedLevel] = useState<number | null>(null);
  const [bedSaving, setBedSaving] = useState(false);
  // Narrated-walkthrough: caption style ("sentence" | "word"). null → "sentence"
  // (today's sentence-block captions). Optimistic local shadow; reset from item.
  const [captionStyle, setCaptionStyle] = useState<VoiceoverCaptionStyle | null>(null);
  const [captionSaving, setCaptionSaving] = useState(false);
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

  // Sync voiceover path from item whenever it changes (after refetch / on load).
  useEffect(() => {
    if (item?.voiceover_gcs_path !== undefined) {
      setVoiceoverGcsPath(item.voiceover_gcs_path ?? null);
    }
  }, [item?.voiceover_gcs_path]);

  // Sync the original-audio bed level from the item (after refetch / on load).
  useEffect(() => {
    if (item?.voiceover_bed_level !== undefined) {
      setBedLevel(item.voiceover_bed_level ?? null);
    }
  }, [item?.voiceover_bed_level]);

  // Sync the caption style from the item (after refetch / on load).
  useEffect(() => {
    if (item?.voiceover_caption_style !== undefined) {
      setCaptionStyle(item.voiceover_caption_style === "word" ? "word" : "sentence");
    }
  }, [item?.voiceover_caption_style]);

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
    // editGeneration forces a re-run when pendingEdits is mutated (refs are not
    // reactive; without this, the optimistic pin only takes effect on the next data update).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [data, editGeneration],
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

  // "✓ Updated" cue: detect when the focused variant's render_finished_at advances
  // (the exact moment StableVideo swaps in fresh bytes) and flash a transient badge.
  const prevFocusedFinishedAtRef = useRef<string | null>(undefined as unknown as null);
  useEffect(() => {
    const focused = variants.find((v) => v.variant_id === focusedVariantId);
    const cur = focused?.render_finished_at ?? null;
    const prev = prevFocusedFinishedAtRef.current;
    if (prev !== undefined && prev !== null && cur !== null && cur !== prev && focused?.render_status === "ready") {
      renderingAction.current = null; // clear the in-flight label now that it's done
      setUpdatedVariantId(focusedVariantId);
      const timer = setTimeout(() => setUpdatedVariantId(null), 4000);
      return () => clearTimeout(timer);
    }
    prevFocusedFinishedAtRef.current = cur;
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
    async (
      variantId: string,
      prevFinishedAt: string | null,
      action: () => Promise<unknown>,
      actionMeta?: { type: "song" | "text" | "style" | "other"; label: string },
    ) => {
      setError(null);
      // Optimistic pin: mark rendering immediately so the variants memo (which reads
      // pendingEdits.current) fires on the SAME React tick as the click — not after
      // the HTTP round-trip + next poll. setEditGeneration triggers the parent re-render
      // that re-runs the memo; pendingEdits.current is already mutated by then.
      pendingEdits.current.set(variantId, { priorFinishedAt: prevFinishedAt, sawRendering: false });
      if (actionMeta) renderingAction.current = actionMeta;
      setEditGeneration((g) => g + 1);
      try {
        await action();
        // Re-anchor the pin now that the dispatch succeeded; keeps it alive until the
        // poll catches the variant mid-rendering or render_finished_at advances.
        markVariantRendering(variantId, prevFinishedAt);
      } catch (err) {
        // Clear the optimistic pin on any error so controls re-enable.
        pendingEdits.current.delete(variantId);
        renderingAction.current = null;
        setEditGeneration((g) => g + 1);
        const msg = err instanceof Error ? err.message : "Failed to update variant";
        // 409 = variant is being rendered by a prior edit — don't treat as a scary error.
        if (msg.toLowerCase().includes("re-rendering") || msg.includes("409")) {
          setError("Still applying your last change — wait for it to finish, then try again.");
        } else {
          setError(msg);
        }
        refetch();
      }
    },
    [markVariantRendering, refetch],
  );

  // Instructed items (WS2): create-new/mixed items with a filmed shot guide use
  // ShotSlotUploader. existing_footage items keep the legacy pool upload.
  // instruction_level no longer gates the upload UI — it only affects copy/tone.
  const contentMode = item?.content_mode ?? "create_new";
  const isFilmThis = contentMode !== "existing_footage";
  const hasGuide = (item?.filming_guide?.length ?? 0) > 0;
  const isInstructed = isFilmThis && hasGuide;

  // Narrated sub-modes:
  //   "narrated" | "narrated_planned" → step-guided flow (plan first, then film)
  //   "narrated_ready"               → have-videos flow (audio first, pool clips)
  const isNarrated =
    item?.edit_format === "narrated" ||
    item?.edit_format === "narrated_planned" ||
    item?.edit_format === "narrated_ready";
  const isNarratedReady = item?.edit_format === "narrated_ready";

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

  async function handleVoiceover(gcsPath: string | null) {
    setVoiceoverGcsPath(gcsPath);
    setVoiceoverSaving(true);
    try {
      await setItemVoiceover(itemId, gcsPath);
      refetch();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save voiceover");
    } finally {
      setVoiceoverSaving(false);
    }
  }

  async function handleBedLevelChange(level: number | null) {
    setBedLevel(level);
    setBedSaving(true);
    try {
      await setItemVoiceoverBedLevel(itemId, level);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save background sound");
    } finally {
      setBedSaving(false);
    }
  }

  async function handleCaptionStyleChange(style: VoiceoverCaptionStyle) {
    setCaptionStyle(style);
    setCaptionSaving(true);
    try {
      await setItemVoiceoverCaptionStyle(itemId, style);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save caption style");
    } finally {
      setCaptionSaving(false);
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

        {/* ── Single-column layout: back link + header + shot plan + generate + progress ── */}
        <div>

          {/* Content: back link + editorial header + uploader + generate + progress */}
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
            <SeedProvenanceBadge item={item} />

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

            {/* Format picker — shown when item hasn't started generating */}
            {item.status !== "generating" && item.status !== "ready" && variants.length === 0 && (
              <div className="mb-4">
                <p className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-400">
                  Edit style
                </p>
                <div className="flex gap-2">
                  {(
                    [
                      { value: "montage", label: "Montage", desc: "Cuts and transitions from your clips" },
                      { value: "narrated_planned", label: "Narrated walkthrough", desc: "Record your voice, clips follow along" },
                    ] as { value: string; label: string; desc: string }[]
                  ).map(({ value, label, desc }) => {
                    const active = value === "narrated_planned" ? isNarrated : (item.edit_format ?? "montage") === value;
                    return (
                      <button
                        key={label}
                        type="button"
                        onClick={async () => {
                          if (active) return;
                          await updatePlanItem(item.id, { edit_format: value }).catch(() => null);
                          refetch();
                        }}
                        className={`flex flex-1 flex-col rounded-xl border px-3 py-2.5 text-left transition-colors ${
                          active
                            ? "border-lime-400 bg-lime-50"
                            : "border-zinc-200 bg-white hover:border-zinc-300"
                        }`}
                      >
                        <span className={`text-sm font-medium ${active ? "text-lime-800" : "text-[#0c0c0e]"}`}>
                          {label}
                        </span>
                        <span className="mt-0.5 text-xs text-zinc-400">{desc}</span>
                      </button>
                    );
                  })}
                </div>

                {/* Narrated sub-mode picker */}
                {isNarrated && (
                  <div className="mt-3 flex gap-2">
                    {(
                      [
                        { value: "narrated_planned", label: "Planning to film", desc: "Get a step guide, film each shot" },
                        { value: "narrated_ready",   label: "I have the videos", desc: "Upload audio + clips, we match them" },
                      ] as { value: string; label: string; desc: string }[]
                    ).map(({ value, label, desc }) => {
                      const active = isNarratedReady
                        ? value === "narrated_ready"
                        : value === "narrated_planned";
                      return (
                        <button
                          key={value}
                          type="button"
                          onClick={async () => {
                            if (active) return;
                            await updatePlanItem(item.id, { edit_format: value }).catch(() => null);
                            refetch();
                          }}
                          className={`flex flex-1 flex-col rounded-xl border px-3 py-2 text-left transition-colors ${
                            active
                              ? "border-zinc-900 bg-zinc-900"
                              : "border-zinc-200 bg-white hover:border-zinc-300"
                          }`}
                        >
                          <span className={`text-xs font-semibold ${active ? "text-white" : "text-[#0c0c0e]"}`}>
                            {label}
                          </span>
                          <span className={`mt-0.5 text-[11px] ${active ? "text-zinc-400" : "text-zinc-400"}`}>{desc}</span>
                        </button>
                      );
                    })}
                  </div>
                )}

                {/* Montage sub-mode picker — "Planning to film" vs "I already have footage".
                    Flips the per-item content_mode override so the user can skip shot-plan
                    generation and go straight to the pool uploader. Only shown when Montage
                    is the active style (narrated has its own equivalent picker above). */}
                {!isNarrated && (
                  <div className="mt-3 flex gap-2">
                    {(
                      [
                        { value: "create_new",       label: "Planning to film",        desc: "Get a shot plan, film each shot" },
                        { value: "existing_footage", label: "I already have footage",  desc: "Skip the plan — just upload your footage" },
                      ] as { value: "create_new" | "existing_footage"; label: string; desc: string }[]
                    ).map(({ value, label, desc }) => {
                      // "I already have footage" is active when content_mode is explicitly
                      // existing_footage; otherwise "Planning to film" is the default.
                      const active = value === "existing_footage"
                        ? contentMode === "existing_footage"
                        : contentMode !== "existing_footage";
                      return (
                        <button
                          key={value}
                          type="button"
                          onClick={async () => {
                            if (active) return;
                            await updatePlanItem(item.id, { content_mode: value }).catch(() => null);
                            refetch();
                          }}
                          className={`flex flex-1 flex-col rounded-xl border px-3 py-2 text-left transition-colors ${
                            active
                              ? "border-zinc-900 bg-zinc-900"
                              : "border-zinc-200 bg-white hover:border-zinc-300"
                          }`}
                        >
                          <span className={`text-xs font-semibold ${active ? "text-white" : "text-[#0c0c0e]"}`}>
                            {label}
                          </span>
                          <span className={`mt-0.5 text-[11px] ${active ? "text-zinc-400" : "text-zinc-400"}`}>{desc}</span>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            )}

            {/* Landscape-clip fit picker — shown alongside Edit style */}
            {item.status !== "generating" && item.status !== "ready" && variants.length === 0 && (
              <div className="mb-4">
                <p className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-400">
                  Landscape clips
                </p>
                <div className="flex gap-2">
                  {LANDSCAPE_FIT_OPTIONS.map(({ value, label, desc }) => {
                    const active = (item.landscape_fit ?? "fit") === value;
                    return (
                      <button
                        key={value}
                        type="button"
                        onClick={async () => {
                          if (active) return;
                          await updatePlanItem(item.id, { landscape_fit: value }).catch(() => null);
                          refetch();
                        }}
                        className={`flex flex-1 flex-col rounded-xl border px-3 py-2.5 text-left transition-colors ${
                          active
                            ? "border-lime-400 bg-lime-50"
                            : "border-zinc-200 bg-white hover:border-zinc-300"
                        }`}
                      >
                        <span className={`text-sm font-medium ${active ? "text-lime-800" : "text-[#0c0c0e]"}`}>
                          {label}
                        </span>
                        <span className="mt-0.5 text-xs text-zinc-400">{desc}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Landscape-clip fit — read-only status display post-render */}
            {variants.length > 0 && (() => {
              const applied = LANDSCAPE_FIT_OPTIONS.find(
                (o) => o.value === (item.landscape_fit ?? "fit")
              );
              if (!applied) return null;
              return (
                <div className="mb-4">
                  <p className="mb-1 text-xs font-medium uppercase tracking-wide text-zinc-400">
                    Landscape clips
                  </p>
                  <p className="text-sm font-medium text-lime-800">
                    {applied.label}
                    <span className="ml-1 font-normal text-zinc-400">· {applied.desc}</span>
                  </p>
                </div>
              );
            })()}

            {/* Expand with AI — only for planned mode; hide in ready (have-videos) mode */}
            {!isNarratedReady && item.clip_gcs_paths.length === 0 && !expandProposal && item.status !== "generating" && item.status !== "ready" && variants.length === 0 && (
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
                          filming_guide: expandProposal.filming_guide,
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

            {/* Narrated walkthrough: sticky voice recorder bar — shown for both narrated sub-modes */}
            {isNarrated && (
              <div className="sticky top-0 z-10 -mx-6 mb-6 border-b border-zinc-100 bg-[#fafaf8] px-6 py-3">
                <p className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-400">
                  Voice recording
                </p>
                <VoiceRecorder onVoiceover={handleVoiceover} />
                {voiceoverSaving && (
                  <p className="mt-1 text-xs text-zinc-400">Saving…</p>
                )}
                {voiceoverGcsPath && !voiceoverSaving && (
                  <p className="mt-1 text-xs text-lime-700">
                    Voice recorded — clips will be timed to match your narration.
                  </p>
                )}
              </div>
            )}

            {/* Narrated walkthrough: original-audio bed control — sits next to the
                clips so the creator can dial how much of their clip sound plays
                under the voice. Nova ducks it automatically while they speak. */}
            {isNarrated && (
              <div className="mb-6">
                <p className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-400">
                  Background sound
                </p>
                <p className="mb-3 text-sm text-[#71717a]">
                  How loud your original clip audio plays under your voice. Nova ducks it
                  automatically while you&apos;re talking.
                </p>
                <div className="flex items-center gap-3">
                  <span className="text-xs text-zinc-400">Off</span>
                  <input
                    type="range"
                    min={0}
                    max={1}
                    step={0.05}
                    value={bedLevel ?? 0.25}
                    onChange={(e) => handleBedLevelChange(Number(e.target.value))}
                    className="h-1 flex-1 cursor-pointer accent-lime-600"
                    aria-label="Original video background sound level"
                  />
                  <span className="text-xs text-zinc-400">Loud</span>
                </div>
                <div className="mt-1 flex items-center justify-between">
                  <p className="text-xs text-lime-700">
                    {bedSaving
                      ? "Saving…"
                      : bedLevel === null
                        ? "Nova decides the best level."
                        : bedLevel === 0
                          ? "Voice only — no original audio."
                          : `Original audio at ${Math.round(bedLevel * 100)}%.`}
                  </p>
                  {bedLevel !== null && (
                    <button
                      type="button"
                      onClick={() => handleBedLevelChange(null)}
                      className="text-xs text-zinc-400 underline-offset-2 hover:text-zinc-600 hover:underline"
                    >
                      Reset to Nova
                    </button>
                  )}
                </div>
              </div>
            )}

            {/* Narrated walkthrough: caption style — sentence blocks (default) vs
                word-by-word (one big word at a time, the qbuilder look). Consumed at
                generate time; editable per-word afterward in the on-video editor. */}
            {isNarrated && (
              <div className="mb-6">
                <p className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-400">
                  Captions
                </p>
                <p className="mb-3 text-sm text-[#71717a]">
                  How your voiceover appears as on-screen text.
                </p>
                <div className="grid grid-cols-2 gap-2">
                  {(
                    [
                      {
                        value: "sentence" as const,
                        label: "Sentence",
                        hint: "Full lines, like subtitles",
                      },
                      {
                        value: "word" as const,
                        label: "Word-by-word",
                        hint: "One big word at a time",
                      },
                    ]
                  ).map((opt) => {
                    const active = (captionStyle ?? "sentence") === opt.value;
                    return (
                      <button
                        key={opt.value}
                        type="button"
                        aria-pressed={active}
                        disabled={captionSaving}
                        onClick={() => handleCaptionStyleChange(opt.value)}
                        className={`rounded-xl border px-3 py-2 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
                          active
                            ? "border-lime-600 bg-lime-50 text-lime-900"
                            : "border-zinc-200 bg-white text-[#3f3f46] hover:border-zinc-400"
                        }`}
                      >
                        <span className="block text-sm font-semibold">{opt.label}</span>
                        <span className="block text-xs text-[#71717a]">{opt.hint}</span>
                      </button>
                    );
                  })}
                </div>
                {captionSaving && <p className="mt-1 text-xs text-zinc-400">Saving…</p>}
              </div>
            )}

            {/* Uploader — four branches:
                1. narrated_ready: audio-first flow, pool upload, no step spine
                2. isInstructed (create_new/mixed + guide present) → ShotSlotUploader
                3. isFilmThis but no guide yet → "Generate shot list" CTA
                4. existing_footage → PoolUploadCard (use footage you already have) */}
            {isNarratedReady ? (
              <div>
                <p className="mb-3 text-xs font-medium uppercase tracking-wide text-zinc-400">
                  Your clips
                </p>
                <p className="mb-4 text-sm text-[#71717a]">
                  Upload all the clips you filmed. We&apos;ll listen to your recording and match each moment to the right clip automatically.
                </p>
                <PoolUploadCard
                  clips={item.clip_assignments ?? []}
                  uploading={uploading}
                  onFiles={handleFiles}
                  onKeep={keepUninstructedMatch}
                  onRemove={removeUninstructedClip}
                  onNoteChange={saveUninstructedNote}
                />
              </div>
            ) : isInstructed ? (
              <ShotSlotUploader
                item={item}
                onAttached={(updated) => {
                  conformancePolls.current = 0;
                  refetch();
                }}
                onBusyChange={setUploaderBusy}
              />
            ) : isFilmThis ? (
              /* create_new/mixed with empty filming guide — offer to generate one */
              <div className="mb-6 rounded-2xl border border-dashed border-zinc-200 bg-white p-5 text-center">
                <p className="text-sm text-[#71717a]">
                  {item.filming_suggestion ?? "No shot plan yet."}
                </p>
                <button
                  type="button"
                  disabled={generatingGuide}
                  onClick={async () => {
                    setGeneratingGuide(true);
                    setError(null);
                    try {
                      await generatePlanItemGuide(item.id);
                      refetch();
                    } catch {
                      setError("Couldn't generate a shot plan. Please try again.");
                    } finally {
                      setGeneratingGuide(false);
                    }
                  }}
                  className="mt-3 inline-flex items-center gap-1.5 rounded-lg border border-zinc-200 bg-white px-4 py-2 text-sm text-[#3f3f46] transition-colors hover:border-lime-400 hover:text-lime-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <span aria-hidden>✦</span>
                  {generatingGuide ? "Generating shot plan…" : "Generate shot plan"}
                </button>
              </div>
            ) : (
              /* existing_footage — pool upload (find the footage you already have) */
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

            {/* Generate + "N shots left" caption — below the shot sections (WS1) */}
            {!isGenerating && (
              <div className="mt-4 space-y-2">
                <InkButton
                  onClick={handleGenerate}
                  disabled={
                    generating ||
                    clipCount === 0 ||
                    isGenerating ||
                    uploaderBusy ||
                    (isNarrated && !voiceoverGcsPath)
                  }
                >
                  {generating
                    ? "Starting…"
                    : uploaderBusy
                      ? "Finishing upload…"
                      : "Generate videos"}
                </InkButton>
                <p className="text-center text-sm text-[#a1a1aa]">
                  {uploaderBusy
                    ? "Finishing upload…"
                    : isNarrated && !voiceoverGcsPath
                      ? "Record your voiceover first — narration drives the edit"
                      : clipCount === 0
                        ? "Add clips to generate"
                        : isInstructed && shotsLeft > 0
                          ? `${shotsLeft} shot${shotsLeft !== 1 ? "s" : ""} left`
                          : null}
                </p>
              </div>
            )}

            {/* Nova helper — inline, below Generate (WS1: moved from right rail) */}
            <div className="mt-4">
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
            </div>

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
            onError={setError}
            onSwap={
              focused
                ? (trackId) => {
                    const trackName = tracks.find((t) => t.id === trackId)?.title ?? "new song";
                    return runEdit(
                      focused.variant_id,
                      focused.render_finished_at ?? null,
                      () => swapPlanItemSong(itemId, focused.variant_id, trackId),
                      { type: "song", label: trackName },
                    );
                  }
                : async () => {}
            }
            onRetext={
              focused
                ? (text) =>
                    runEdit(
                      focused.variant_id,
                      focused.render_finished_at ?? null,
                      () => retextPlanItem(itemId, focused.variant_id, { text }),
                      { type: "text", label: "Updating text…" },
                    )
                : async () => {}
            }
            onRemoveText={
              focused
                ? () =>
                    runEdit(
                      focused.variant_id,
                      focused.render_finished_at ?? null,
                      () => retextPlanItem(itemId, focused.variant_id, { remove: true }),
                      { type: "text", label: "Removing text…" },
                    )
                : async () => {}
            }
            onChangeStyle={
              focused
                ? (styleSetId) =>
                    runEdit(
                      focused.variant_id,
                      focused.render_finished_at ?? null,
                      () => changePlanItemStyle(itemId, focused.variant_id, styleSetId),
                      { type: "style", label: "Applying style…" },
                    )
                : async () => {}
            }
            onResize={
              focused
                ? (px) =>
                    runEdit(
                      focused.variant_id,
                      focused.render_finished_at ?? null,
                      () => setPlanItemIntroSize(itemId, focused.variant_id, px),
                      { type: "style", label: "Updating text size…" },
                    )
                : async () => {}
            }
            onChangeLayout={
              focused
                ? (layout) =>
                    runEdit(
                      focused.variant_id,
                      focused.render_finished_at ?? null,
                      () => editPlanItemVariant(itemId, focused.variant_id, { intro_layout: layout }),
                      { type: "style", label: "Updating layout…" },
                    )
                : async () => {}
            }
            renderingAction={renderingAction.current}
            updatedVariantId={updatedVariantId}
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
// Clips tab removed in PR-5: editing moved inline to the Timeline Clips lane.
// Text + Font tabs removed in PR-4: editing moved inline to the Timeline Text lane.
// Overlays tab removed in PR-3: editing moved inline to the Timeline Overlays lane.
type EditorTab = "song" | "captions" | "timeline";

const EDITOR_TABS: { id: EditorTab; icon: string; label: string }[] = [
  { id: "captions", icon: "CC", label: "Captions" },
  { id: "song", icon: "♫", label: "Song" },
  { id: "timeline", icon: "▭", label: "Timeline" },
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
  onError,
  onSwap,
  onRetext,
  onRemoveText,
  onChangeStyle,
  onResize,
  onChangeLayout,
  renderingAction,
  updatedVariantId,
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
  /** Surface a user-facing error in the page-level banner (e.g. SFX save/render failures). */
  onError: (msg: string) => void;
  onSwap: (trackId: string) => Promise<void>;
  onRetext: (text: string) => Promise<void>;
  onRemoveText: () => Promise<void>;
  onChangeStyle: (styleSetId: string) => Promise<void>;
  onResize: (textSizePx: number) => Promise<void>;
  onChangeLayout: (layout: "linear" | "cluster") => Promise<void>;
  renderingAction: { type: "song" | "text" | "style" | "other"; label: string } | null;
  updatedVariantId: string | null;
}) {
  const [activeTab, setActiveTab] = useState<EditorTab | null>(null);
  // T5: textLaneOpen is derived (not state) — true when the timeline tab is open and the variant
  // has text. Text controls are now always visible below the timeline (not in a collapsible panel),
  // so we show LiveEditPreview whenever the user can interact with them.
  // Previously this was state set via onTextPanelChange from UnifiedTimeline; that callback was
  // removed in T5 when the expandable textPanel slot was replaced by the interactive bar lane.
  const textLaneOpen = activeTab === "timeline" && !!variant && variant.text_mode !== "none";

  // ── Overlay-card state (lifted here so Hero can render the instant preview) ─
  const [overlayCards, setOverlayCards] = useState<MediaOverlay[]>(
    variant?.media_overlays ?? [],
  );
  // Seed from preview_url on load so existing applied cards show in the CSS overlay
  // immediately without re-uploading (preview_url is a fresh-signed read URL from the API).
  // localPreviewUrls: blob: URLs from freshly-uploaded card files. NOT initialised from
  // preview_url — the burned output_url already shows those cards, so using preview_url
  // here would double the overlay on page load. Cleared when a burn completes (render_finished_at
  // effect below), so the burned output takes over without doubling.
  const [localPreviewUrls, setLocalPreviewUrls] = useState<Record<string, string>>({});
  // SFX placements — lifted alongside overlayCards so both stay in sync with the active variant.
  const [sfxPlacements, setSfxPlacements] = useState<SoundEffectPlacement[]>(
    variant?.sound_effects ?? [],
  );
  // sfxAudioUrls: map from src_gcs_path → playable URL (signed GCS or blob URL) for instant preview.
  const [sfxAudioUrls, setSfxAudioUrls] = useState<Record<string, string>>({});
  // Current video time lifted from the hero player so "Add at playhead" works.
  const [currentTimeS, setCurrentTimeS] = useState(0);
  useEffect(() => {
    const nextCards = variant?.media_overlays ?? [];
    setOverlayCards(nextCards);
    setSfxPlacements(variant?.sound_effects ?? []);
    setSfxAudioUrls({});
    // Revoke any blob URLs from the previous variant and reset to empty.
    // Do NOT repopulate from preview_url — the burned output_url already shows the cards.
    setLocalPreviewUrls((prev) => {
      Object.values(prev).forEach((url) => URL.revokeObjectURL(url));
      return {};
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [variant?.variant_id]);
  // Declared here (before the render_finished_at effect) so the effect can read it.
  // The full definition lives further down alongside handleDownload.
  const pendingDownloadRef = useRef(false);

  // When a download-triggered burn completes (render_finished_at advances), clear the CSS
  // preview layer — the burned output_url now has the cards composited in. Only fires when
  // pendingDownloadRef is true so stale/concurrent renders (e.g. completing text edits, or
  // lingering renders from a previous session) don't wipe newly uploaded card previews.
  const prevFinishedAtRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const cur = variant?.render_finished_at ?? null;
    if (prevFinishedAtRef.current !== undefined && cur !== prevFinishedAtRef.current) {
      if (pendingDownloadRef.current) {
        setLocalPreviewUrls((prev) => {
          Object.values(prev).forEach((url) => URL.revokeObjectURL(url));
          return {};
        });
      }
    }
    prevFinishedAtRef.current = cur;
  }, [variant?.render_finished_at]);

  // Revoke all blob URLs when the component unmounts (FocusedResults is re-keyed
  // on variant switch, so unmount fires when the user focuses a different variant).
  useEffect(() => {
    return () => {
      setLocalPreviewUrls((prev) => {
        Object.values(prev).forEach((url) => URL.revokeObjectURL(url));
        return {};
      });
    };
  }, []);

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

  const baking = (instantEligible && editSession.isSaving) || pendingDownloadRef.current;

  const handleDownload = useCallback(async () => {
    if (!variant) return;

    // If SFX placements exist but the FFmpeg mix-pass hasn't run yet, trigger it first.
    if (sfxPlacements.length > 0 && !variant.pre_sfx_video_path) {
      pendingDownloadRef.current = true;
      try {
        await renderVariantSfx(itemId, variant.variant_id);
        markVariantRendering(variant.variant_id, variant.render_finished_at ?? null);
      } catch (err) {
        pendingDownloadRef.current = false;
        onError(
          err instanceof Error
            ? err.message
            : "Couldn't add your sound effects to the video. Try again.",
        );
      }
      return;
    }

    // If overlay cards exist, composite them into the video on-demand (render=true).
    // No background render was triggered on card changes — this is the only FFmpeg pass.
    if (overlayCards.length > 0) {
      pendingDownloadRef.current = true;
      try {
        await setVariantMediaOverlays(itemId, variant.variant_id, overlayCards, { render: true });
        markVariantRendering(variant.variant_id, variant.render_finished_at ?? null);
      } catch (err) {
        pendingDownloadRef.current = false;
        onError(
          err instanceof Error
            ? err.message
            : "Couldn't add your overlays to the video. Try again.",
        );
      }
      return;
    }

    if (!variant.output_url && !editSession.isDirty) return;
    if (instantEligible && editSession.isDirty) {
      pendingDownloadRef.current = true;
      void editSession.commit();
      return;
    }
    if (variant.output_url) downloadVideo(variant.output_url, downloadName);
  }, [variant, editSession, instantEligible, sfxPlacements, overlayCards, itemId, downloadName, markVariantRendering, onError]);

  // Alternates: the non-focused ready variants (up to 3 shown as small thumbs)
  const alternates = variants.filter((v) => v.variant_id !== focusedVariantId);
  // "Nova's pick" is always the first variant (index 0 in the variants array)
  const isNovaPick = variant != null && variants.length > 0 && variants[0].variant_id === variant.variant_id;

  // Text-mode label for the pill below the hero. Narrated variants carry the
  // creator's recorded voiceover (not the clips' original audio), so they get
  // their own label regardless of text_mode ("none").
  const TEXT_MODE_PILL: Record<string, string> = {
    lyrics: "With lyrics",
    agent_text: "Original audio",
    none: "Original audio",
  };
  const modePill = variant
    ? variant.resolved_archetype === "narrated"
      ? "Voiceover"
      : (TEXT_MODE_PILL[variant.text_mode] ?? "Original audio")
    : null;

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
            {instantEligible && variant && (activeTab !== "timeline" || textLaneOpen) ? (
              <LiveEditPreview
                variant={variant}
                styleSets={styleSets}
                session={editSession}
                playToken={editSession.playToken}
                textElements={variant.text_elements ?? undefined}
              />
            ) : (
              <Hero
                variant={variant}
                generating={isGenerating}
                overlayCards={overlayCards}
                localPreviewUrls={localPreviewUrls}
                sfxPlacements={sfxPlacements}
                sfxAudioUrls={sfxAudioUrls}
                renderingAction={renderingAction}
                showUpdatedCue={updatedVariantId === variant?.variant_id}
              />
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
                        <StableVideo
                          src={v.output_url}
                          identity={v.render_finished_at ?? undefined}
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

          {/* ── Unplaced shots info card ── */}
          {variant && (variant.unplaced_shots?.length ?? 0) > 0 && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-3">
              <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.12em] text-amber-700">
                Not in this take
              </p>
              <ul className="space-y-0.5">
                {variant.unplaced_shots!.map((shot) => (
                  <li key={shot.clip_id} className="text-xs text-amber-800">
                    <span className="font-medium">Shot {shot.shot_index}</span>
                    {" – "}
                    {unplacedShotCopy(shot.reason)}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* ── Editor row: 4 icon+label buttons ── */}
          {focusedEditable && (
            <div>
              <div className="flex gap-2">
                {EDITOR_TABS.map((tab) => {
                  const hasCaptions = !!variant?.caption_cues?.length && !!variant?.base_video_url;
                  // Captions tab only for narrated variants that carry editable cues.
                  if (tab.id === "captions" && !hasCaptions) return null;
                  // Narrated has no song to edit — only Captions + Clips.
                  if (hasCaptions && tab.id === "song") return null;
                  // Hide Song tab when no song is swappable
                  if (tab.id === "song" && (tracks.length === 0 || !variant?.music_track_id)) return null;
                  // Timeline: only when SFX is enabled.
                  if (tab.id === "timeline" && !SOUND_EFFECTS_ENABLED) return null;
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
                  {activeTab === "captions" &&
                  variant.base_video_url &&
                  variant.caption_cues ? (
                    <CaptionEditor
                      itemId={itemId}
                      variantId={variant.variant_id}
                      baseVideoUrl={variant.base_video_url}
                      initialCues={variant.caption_cues}
                      initialFont={variant.voiceover_caption_font}
                      rendering={variant.render_status === "rendering"}
                      onApplied={() => {
                        markVariantRendering(
                          variant.variant_id,
                          variant.render_finished_at ?? null,
                        );
                        refetch();
                      }}
                    />
                  ) : (
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
                      overlayCards={overlayCards}
                      setOverlayCards={setOverlayCards}
                      localPreviewUrls={localPreviewUrls}
                      setLocalPreviewUrls={setLocalPreviewUrls}
                      sfxPlacements={sfxPlacements}
                      setSfxPlacements={setSfxPlacements}
                      sfxAudioUrls={sfxAudioUrls}
                      setSfxAudioUrls={setSfxAudioUrls}
                      currentTimeS={currentTimeS}
                      onError={onError}
                    />
                  )}
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

// ── T6: TextElement ↔ TextElementBar conversion helpers ──────────────────────

/**
 * Convert API TextElement[] → TextElementBar[] for the reducer initial state.
 * Only the fields that TextElementBar carries are mapped; position / x_frac /
 * y_frac / highlight_color / stroke_width / fade_out_ms / reveal_s / z /
 * word_timings are API-only and will be re-applied by the compiler at render time
 * (they're preserved in source_params).
 */
function convertApiTextElements(
  apiElements: TextElement[] | null | undefined,
): import("@/lib/timeline/text-timeline-reducer").TextElementBar[] {
  return (apiElements ?? []).map((el) => ({
    id: el.id,
    text: el.text,
    start_s: el.start_s,
    end_s: el.end_s,
    role: el.role,
    font_family: el.font_family ?? undefined,
    size_px: el.size_px ?? undefined,
    size_class: el.size_class ?? undefined,
    color: el.color ?? undefined,
    effect: el.effect ?? undefined,
    alignment: el.alignment ?? undefined,
    source_params: el.source_params ?? undefined,
  }));
}

/**
 * PR-B: Convert narrated CaptionCue[] → TextElementBar[] for the Text lane.
 * Uses stable index-based IDs ("caption-0", "caption-1", …) so re-syncs from
 * the server don't thrash the reducer's undo/redo identity tracking.
 */
function convertCaptionCues(
  cues: CaptionCue[] | null | undefined,
): import("@/lib/timeline/text-timeline-reducer").TextElementBar[] {
  return (cues ?? []).map((c, i) => ({
    id: `caption-${i}`,
    text: c.text,
    start_s: c.start_s,
    end_s: c.end_s,
    role: "narrated_caption" as const,
  }));
}

/**
 * PR-E: Convert scene_timings[] → TextElementBar[] for the Text lane.
 * Uses stable index-based IDs ("scene-0", "scene-1", …). Only scenes with
 * non-null start_s and end_s are included (scenes lacking timing data are skipped).
 */
function convertSceneTimings(
  scenes: Array<{ text: string; start_s: number | null; end_s: number | null }>,
): import("@/lib/timeline/text-timeline-reducer").TextElementBar[] {
  return scenes
    .filter((s) => s.start_s != null && s.end_s != null)
    .map((s, i) => ({
      id: `scene-${i}`,
      text: s.text,
      start_s: s.start_s as number,
      end_s: s.end_s as number,
      role: "generative_sequence" as const,
    }));
}

/**
 * Controls-only column for the focused variant. Receives the edit session as a
 * prop (the parent owns it, keyed by variant_id) — it does NOT create one.
 *
 * `activeTab` controls which section of PlanVariantEditor is surfaced. The
 * "song" tab shows the song-swap picker; "clips" opens the timeline editor sheet.
 * Text/font editing is now inline in the UnifiedTimeline Text lane (PR-4).
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
  overlayCards,
  setOverlayCards,
  localPreviewUrls,
  setLocalPreviewUrls,
  sfxPlacements,
  setSfxPlacements,
  sfxAudioUrls,
  setSfxAudioUrls,
  currentTimeS,
  onError,
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
  overlayCards: MediaOverlay[];
  setOverlayCards: Dispatch<SetStateAction<MediaOverlay[]>>;
  localPreviewUrls: Record<string, string>;
  setLocalPreviewUrls: Dispatch<SetStateAction<Record<string, string>>>;
  sfxPlacements: SoundEffectPlacement[];
  setSfxPlacements: Dispatch<SetStateAction<SoundEffectPlacement[]>>;
  sfxAudioUrls: Record<string, string>;
  setSfxAudioUrls: Dispatch<SetStateAction<Record<string, string>>>;
  currentTimeS: number;
  /** Surface a user-facing error in the page-level banner (e.g. SFX save failures). */
  onError: (msg: string) => void;
}) {
  const [overlayUploading, setOverlayUploading] = useState(false);
  // True when cards have been modified and need metadata persistence.
  const overlaysDirtyRef = useRef(false);
  // Latest overlayCards value for setTimeout closures.
  const overlayCardsRef = useRef(overlayCards);
  overlayCardsRef.current = overlayCards;

  // Shared clip-timeline data: owned here so ClipsLane header bars and the
  // InlineClipsEditor expanded panel read/write one draft (no double fetch).
  const clipTimeline = useClipTimeline(itemId, variant.variant_id, "plan-item");

  // Probe the actual variant duration so the overlay timeline shows the right length.
  const [variantDurationS, setVariantDurationS] = useState(30);
  useEffect(() => {
    const url = variant.output_url;
    if (!url) return;
    const v = document.createElement("video");
    v.preload = "metadata";
    v.onloadedmetadata = () => {
      if (isFinite(v.duration) && v.duration > 0) setVariantDurationS(v.duration);
      v.src = "";
    };
    v.src = url;
  }, [variant.output_url]);

  // Auto-save card metadata (render=false) 2.5 s after the user stops editing.
  // No FFmpeg is triggered here — rendering only happens on explicit download.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!overlaysDirtyRef.current) return;
    const cards = overlayCardsRef.current;
    const timer = setTimeout(async () => {
      overlaysDirtyRef.current = false;
      try {
        await setVariantMediaOverlays(itemId, variant.variant_id, cards, { render: false });
        refetch();
      } catch (err) {
        // Cards are safe in local state, but the save failed (e.g. backend
        // media_overlays_enabled off → 404). Surface it so the user knows the
        // overlay positions won't persist / won't be in the render.
        onError(
          err instanceof Error
            ? err.message
            : "Couldn't save your overlays — they won't be in the render.",
        );
      }
    }, 2500);
    return () => clearTimeout(timer);
  }, [overlayCards]); // eslint-disable-line react-hooks/exhaustive-deps

  /** Upload new files, append as new overlay cards with default settings. */
  async function handleOverlayUpload(
    files: { file: File; filename: string; content_type: string; file_size_bytes: number }[],
  ) {
    setOverlayUploading(true);
    try {
      const POSITION_CYCLE: { position: "top" | "center" | "bottom"; x_frac: number; y_frac: number }[] = [
        { position: "center", x_frac: 0.5, y_frac: 0.5 },
        { position: "top", x_frac: 0.5, y_frac: 0.18 },
        { position: "bottom", x_frac: 0.5, y_frac: 0.82 },
      ];

      // Build temporary cards (src_gcs_path placeholder) and blob URLs immediately.
      const tempCards: MediaOverlay[] = files.map((f, i) => {
        const slot = POSITION_CYCLE[(overlayCards.length + i) % POSITION_CYCLE.length];
        return {
          id: crypto.randomUUID(),
          kind: f.content_type.startsWith("video/") ? "video" : "image",
          src_gcs_path: "", // filled in after GCS upload completes
          position: slot.position,
          x_frac: slot.x_frac,
          y_frac: slot.y_frac,
          scale: 0.35,
          start_s: 0,
          end_s: +Math.min(5, variantDurationS).toFixed(2),
          z: overlayCards.length + i,
        };
      });
      const blobUrls: Record<string, string> = {};
      tempCards.forEach((card, i) => {
        blobUrls[card.id] = URL.createObjectURL(files[i].file);
      });

      // Probe video durations from the local File (fast — just reads container header).
      const durationsMap: Record<string, number> = {};
      await Promise.all(
        tempCards
          .filter((card) => card.kind === "video")
          .map(
            (card) =>
              new Promise<void>((resolve) => {
                const v = document.createElement("video");
                v.preload = "metadata";
                const done = () => {
                  if (isFinite(v.duration) && v.duration > 0) {
                    durationsMap[card.id] = v.duration;
                  }
                  v.src = "";
                  resolve();
                };
                v.onloadedmetadata = done;
                v.onerror = done;
                setTimeout(done, 3000);
                v.src = blobUrls[card.id];
              }),
          ),
      );

      // Show cards immediately — trim lane is live, CSS preview is live.
      const immediateCards = tempCards.map((card) =>
        durationsMap[card.id] ? { ...card, clip_duration_s: durationsMap[card.id] } : card,
      );
      setLocalPreviewUrls((prev) => ({ ...prev, ...blobUrls }));
      setOverlayCards((prev) => [...prev, ...immediateCards]);

      // Upload to GCS in the background; update src_gcs_path when done.
      const uploadUrls = await requestOverlayUploadUrls(
        itemId,
        files.map((f) => ({
          filename: f.filename,
          content_type: f.content_type,
          file_size_bytes: f.file_size_bytes,
        })),
      );
      await Promise.all(uploadUrls.map((u, i) => uploadToGcs(u.upload_url, files[i].file)));

      // Patch the cards already in state with their real GCS paths, then mark dirty
      // so the auto-save effect persists them (with real GCS paths) after 2.5 s.
      setOverlayCards((prev) =>
        prev.map((card) => {
          const idx = immediateCards.findIndex((c) => c.id === card.id);
          if (idx === -1) return card;
          return { ...card, src_gcs_path: uploadUrls[idx].gcs_path };
        }),
      );
      overlaysDirtyRef.current = true;
    } catch (err) {
      // Upload-URL request or GCS upload failed (e.g. backend media_overlays_enabled
      // off → overlays-upload-urls 404). Surface it instead of throwing uncaught.
      onError(
        err instanceof Error
          ? err.message
          : "Couldn't upload that overlay. Try again.",
      );
    } finally {
      setOverlayUploading(false);
    }
  }

  /** Clear all overlays (restore pre-overlay clean variant). */
  async function handleClearOverlays() {
    // Clear CSS preview immediately — user explicitly removed all cards.
    setLocalPreviewUrls((prev) => {
      Object.values(prev).forEach((url) => URL.revokeObjectURL(url));
      return {};
    });
    setOverlayCards([]);
    try {
      await setVariantMediaOverlays(itemId, variant.variant_id, [], { render: false });
      refetch();
    } catch (err) {
      onError(
        err instanceof Error ? err.message : "Couldn't clear your overlays. Try again.",
      );
    }
  }

  function handleUpdateCard(id: string, patch: Partial<MediaOverlay>) {
    // Resolve position presets to fracs so the CSS preview updates immediately.
    const resolved: Partial<MediaOverlay> = { ...patch };
    if (patch.position === "top") { resolved.x_frac = 0.5; resolved.y_frac = 0.18; }
    else if (patch.position === "center") { resolved.x_frac = 0.5; resolved.y_frac = 0.5; }
    else if (patch.position === "bottom") { resolved.x_frac = 0.5; resolved.y_frac = 0.82; }
    overlaysDirtyRef.current = true;
    setOverlayCards((prev) => prev.map((c) => (c.id === id ? { ...c, ...resolved } : c)));
  }

  function handleRemoveCard(id: string) {
    overlaysDirtyRef.current = true;
    setOverlayCards((prev) => prev.filter((c) => c.id !== id));
    setLocalPreviewUrls((prev) => {
      if (!prev[id]) return prev;
      URL.revokeObjectURL(prev[id]);
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }

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

  // ── SFX state + handlers ──────────────────────────────────────────────────
  const [sfxUploading, setSfxUploading] = useState(false);
  const [glossaryEffects, setGlossaryEffects] = useState<SoundEffectSummary[]>([]);
  const [glossaryLoading, setGlossaryLoading] = useState(false);

  // ── Text-elements state (T10 + T6) ────────────────────────────────────────
  // Optimistic render status per variantId so the UI doesn't freeze on apply
  // before the server round-trip returns (Part B: plan-item-edit-no-optimistic-state).
  const [optimisticRenderStatus, setOptimisticRenderStatus] = useState<Record<string, string>>({});
  // Transient error/retry banner shown after a save conflict (409) or failed save.
  const [textApplyError, setTextApplyError] = useState<string | null>(null);
  // Brief note after a TRIM_START clamp (e.g. "Minimum 0.1s") — auto-clears after 2 s.
  const [textElementNote, setTextElementNote] = useState<string | null>(null);
  // State 3 note: selected-bar tracking is managed internally by TextLane (onBarSelect).
  // UnifiedTimeline's textExpandedBarId is cleared when the selected bar is deleted.
  // Local mirror of textElements bars — seeded from:
  //   • variant.caption_cues (narrated variants, PR-B) — teal "narrated_caption" bars
  //   • variant.text_elements (generative variants, T6) — amber bars
  // Updated on every reducer mutation; used to derive State 5 (text too long) warning.
  const [textElements, setTextElements] = useState<TextElementBar[]>(() => {
    if (variant.caption_cues?.length) return convertCaptionCues(variant.caption_cues);
    if (variant.scene_timings?.length) return convertSceneTimings(variant.scene_timings);
    return convertApiTextElements(variant.text_elements);
  });
  // Re-sync from API data when a render completes (render_finished_at advances).
  useEffect(() => {
    if (variant.caption_cues?.length) {
      // Narrated: re-sync from fresh caption data after a reburn.
      setTextElements(convertCaptionCues(variant.caption_cues));
    } else if (variant.scene_timings?.length) {
      // Sequence: re-sync from scene_timings when they update.
      setTextElements(convertSceneTimings(variant.scene_timings));
    } else if (!variant.text_elements_user_edited) {
      setTextElements(convertApiTextElements(variant.text_elements));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [variant.render_finished_at]);
  // Debounce timer ref for the auto-apply after text-element edits.
  const textApplyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Load glossary when the Timeline tab is first opened.
  useEffect(() => {
    if (activeTab !== "timeline" || !SOUND_EFFECTS_ENABLED) return;
    if (glossaryEffects.length > 0) return;
    setGlossaryLoading(true);
    getSoundEffects()
      .then(setGlossaryEffects)
      .catch(() => {/* glossary is best-effort */})
      .finally(() => setGlossaryLoading(false));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

  async function handleSfxUpload(
    files: { file: File; filename: string; content_type: string; file_size_bytes: number }[],
  ) {
    setSfxUploading(true);
    try {
      const urls = await requestSfxUploadUrls(
        itemId,
        files.map((f) => ({ filename: f.filename, content_type: f.content_type, file_size_bytes: f.file_size_bytes })),
      );
      await Promise.all(urls.map((u, i) => uploadToGcs(u.upload_url, files[i].file)));
      const newPlacements: SoundEffectPlacement[] = urls.map((u, i) => ({
        id: crypto.randomUUID(),
        src_gcs_path: u.gcs_path,
        at_s: Math.min(Math.max(0, currentTimeS), Math.max(0, variantDurationS - 0.05)),
        gain: 1.0,
        label: files[i].filename.replace(/\.[^.]+$/, ""),
      }));
      handleSfxChange([...sfxPlacements, ...newPlacements]);
    } catch (err) {
      // Upload-URL request or GCS upload failed (e.g. backend
      // SOUND_EFFECTS_ENABLED off → sfx-upload-urls 404). Surface it.
      onError(
        err instanceof Error ? err.message : "Couldn't upload that sound effect. Try again.",
      );
    } finally {
      setSfxUploading(false);
    }
  }

  // Edits PERSIST (debounced) but do NOT render — the user explicitly clicks
  // "Apply" to burn the effects into the video (handleApplySfx). This keeps the
  // instant client-side preview snappy and avoids a render on every drag/retime.
  // sfxDirty = there are placement changes not yet reflected in the rendered
  // video. Seeded true when the variant has saved SFX that were never burned in
  // (sound_effects present but pre_sfx_video_path null — the saved-but-never-
  // rendered case that was the original bug).
  const [sfxDirty, setSfxDirty] = useState<boolean>(
    () => sfxPlacements.length > 0 && !variant.pre_sfx_video_path,
  );
  const sfxSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  function handleSfxChange(newPlacements: SoundEffectPlacement[]) {
    setSfxPlacements(newPlacements);
    setSfxDirty(true);
    if (sfxSaveTimer.current) clearTimeout(sfxSaveTimer.current);
    sfxSaveTimer.current = setTimeout(async () => {
      try {
        await setVariantSoundEffects(itemId, variant.variant_id, newPlacements);
      } catch (err) {
        // The client-side preview still plays the effect locally, but the save
        // failed — surface it (e.g. backend SOUND_EFFECTS_ENABLED off → 404).
        onError(
          err instanceof Error
            ? err.message
            : "Couldn't save your sound effects.",
        );
      }
    }, 600);
  }

  // "Apply" — flush the pending save (so the server has the latest placements),
  // then trigger the SFX burn-in render. Clears the dirty flag optimistically;
  // the variant flips to render_status="rendering" → "ready" via polling.
  async function handleApplySfx() {
    if (sfxSaveTimer.current) clearTimeout(sfxSaveTimer.current);
    try {
      await setVariantSoundEffects(itemId, variant.variant_id, sfxPlacements);
      await renderVariantSfx(itemId, variant.variant_id);
      markVariantRendering(variant.variant_id, variant.render_finished_at ?? null);
      setSfxDirty(false);
    } catch (err) {
      onError(
        err instanceof Error ? err.message : "Couldn't apply your sound effects to the video.",
      );
    }
  }

  // Fetch signed playback URLs for SFX placements that don't have one yet.
  // Key: use src_gcs_path when available, fall back to placement.id so glossary
  // effects (src_gcs_path="" until server resolves it) get a URL immediately.
  useEffect(() => {
    if (!SOUND_EFFECTS_ENABLED) return;
    const missing = sfxPlacements.filter((p) => {
      const key = p.src_gcs_path || p.id;
      return key && !sfxAudioUrls[key];
    });
    if (missing.length === 0) return;

    const newUrls: Record<string, string> = {};
    const userPaths: SoundEffectPlacement[] = [];

    for (const p of missing) {
      const key = p.src_gcs_path || p.id;
      const glossaryMatch = glossaryEffects.find((g) => g.id === p.sound_effect_id);
      if (glossaryMatch?.preview_url) {
        newUrls[key] = glossaryMatch.preview_url;
      } else if (p.src_gcs_path.startsWith("users/")) {
        userPaths.push(p);
      }
    }

    if (Object.keys(newUrls).length > 0) {
      setSfxAudioUrls((prev) => ({ ...prev, ...newUrls }));
    }

    for (const p of userPaths) {
      getSfxAudioUrl(itemId, p.src_gcs_path)
        .then((url) => setSfxAudioUrls((prev) => ({ ...prev, [p.src_gcs_path]: url })))
        .catch(() => {});
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sfxPlacements, glossaryEffects, sfxAudioUrls, itemId]);

  // ── Text-element handlers (T6) ────────────────────────────────────────────

  /**
   * Apply text-element bars to the variant via PUT text-elements (T6 wiring).
   *
   * Part A (apply-clears-preview-layer learning): clears localPreviewUrls
   * BEFORE triggering the render pass so the burned output takes over without
   * double-compositing previously-uploaded overlay blob URLs.
   *
   * Part B (plan-item-edit-no-optimistic-state learning): sets optimistic
   * "rendering" state synchronously so the UI reflects in-flight rendering
   * before the server round-trip completes.
   */
  const handleApplyTextElements = useCallback(
    async (variantId: string, elements: TextElementBar[]) => {
      // Part A: clear preview layer first.
      setLocalPreviewUrls((prev) => {
        Object.values(prev).forEach((url) => URL.revokeObjectURL(url));
        return {};
      });
      // Part B: optimistic rendering state so controls show "rendering" immediately.
      setOptimisticRenderStatus((prev) => ({ ...prev, [variantId]: "rendering" }));
      setTextApplyError(null);
      try {
        // Convert TextElementBar → TextElement for the API.
        // narrated_caption bars are handled by setPlanItemCaptions — filter them out here.
        const apiElements: TextElement[] = elements
          .filter((bar) => bar.role !== "narrated_caption")
          .map((bar) => ({
          id: bar.id,
          text: bar.text,
          start_s: bar.start_s,
          end_s: bar.end_s,
          role: bar.role as TextElement["role"],
          font_family: bar.font_family ?? null,
          size_px: bar.size_px ?? null,
          size_class: (bar.size_class as TextElement["size_class"]) ?? null,
          color: bar.color ?? null,
          highlight_color: bar.highlight_color ?? null,
          stroke_width: bar.stroke_width ?? null,
          effect: (bar.effect as TextElement["effect"]) ?? null,
          alignment: (bar.alignment as TextElement["alignment"]) ?? null,
          source_params: bar.source_params ?? null,
          position: "middle" as const,
        }));
        await putTextElements(itemId, variantId, apiElements);
        markVariantRendering(variantId, variant.render_finished_at ?? null);
      } catch (err) {
        // Clear optimistic state on failure so controls re-enable.
        setOptimisticRenderStatus((prev) => {
          const next = { ...prev };
          delete next[variantId];
          return next;
        });
        const msg = err instanceof Error ? err.message : "";
        if (msg.includes("409") || msg.toLowerCase().includes("conflict")) {
          // State 1: save conflict — refresh to get latest server state.
          setTextApplyError("Text updated elsewhere — refreshing");
          refetch();
        } else {
          // State 2: undo after failed save — inform the user; caller should revert reducer.
          setTextApplyError("Couldn't save text — retrying");
        }
      }
    },
    [setLocalPreviewUrls, markVariantRendering, variant.render_finished_at, refetch, itemId],
  );

  /**
   * Handle text-element changes from the reducer: update local mirror + debounce-apply.
   * Waits 1 s after the last edit before persisting so rapid drag/trim gestures
   * don't flood the API.
   *
   * PR-B: for narrated_caption bars, persists via setPlanItemCaptions (no re-render —
   * the player overlays them instantly).  Generative bars use the existing
   * handleApplyTextElements path (triggers a full reburn).
   */
  const handleTextElementsChange = useCallback(
    (bars: TextElementBar[]) => {
      setTextElements(bars);
      if (textApplyTimer.current) clearTimeout(textApplyTimer.current);
      if (bars[0]?.role === "narrated_caption") {
        textApplyTimer.current = setTimeout(() => {
          const cues: CaptionCue[] = bars.map((b) => ({
            text: b.text,
            start_s: b.start_s,
            end_s: b.end_s,
          }));
          void setPlanItemCaptions(itemId, variant.variant_id, cues);
        }, 1000);
      } else if (bars[0]?.role === "generative_sequence" && variant.scene_timings?.length) {
        // PR-E: sequence bars — persist via patchPlanItemSceneTiming (no re-render).
        textApplyTimer.current = setTimeout(() => {
          const overrides: SceneTimingPatch[] = bars.map((b, i) => ({
            scene_index: i,
            start_s: b.start_s,
            end_s: b.end_s,
          }));
          void patchPlanItemSceneTiming(itemId, variant.variant_id, overrides);
        }, 1000);
      } else if (bars[0]?.role === "generative_intro" && variant.intro_start_s != null) {
        // PR-E: intro timing bar — persist via setPlanItemIntroTiming (no re-render).
        textApplyTimer.current = setTimeout(() => {
          const bar = bars[0];
          void setPlanItemIntroTiming(itemId, variant.variant_id, bar.start_s, bar.end_s);
        }, 1000);
      } else {
        textApplyTimer.current = setTimeout(() => {
          void handleApplyTextElements(variant.variant_id, bars);
        }, 1000);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [variant.variant_id, variant.scene_timings, variant.intro_start_s, handleApplyTextElements, itemId],
  );

  /** State 4: called by UnifiedTimeline when a trim drag is clamped to MIN_DUR_S. */
  const handleTextTrimClamped = useCallback(() => {
    setTextElementNote("Minimum 0.1s");
    const t = setTimeout(() => setTextElementNote(null), 2000);
    return () => clearTimeout(t);
  }, []);

  const showSongSection = activeTab === "song";
  const showTimelineSection = activeTab === "timeline" && SOUND_EFFECTS_ENABLED;

  return (
    <>
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
          hasClipEdits={false}
          hideSections={["caption", "size", "layout", "style", "clips"]}
        />
      )}

      {/* Timeline tab: unified multi-lane timeline (SFX + Overlays + Text + Clips inline) */}
      {showTimelineSection && (
        <div className="space-y-1.5">
          {/* State 6: no base_video_path = fast reburn unavailable — inform the user. */}
          {!variant.base_video_path && (
            <p className="text-[11px] text-zinc-500">
              Full re-render needed (may take a moment)
            </p>
          )}
          {/* States 1+2: save conflict or failed save — amber banner. */}
          {textApplyError && (
            <p className="rounded bg-amber-900/30 px-2 py-1 text-[11px] text-amber-400">
              {textApplyError}
            </p>
          )}
          {/* State 4: minimum-duration clamp note — auto-clears after 2 s. */}
          {textElementNote && (
            <p className="px-1 text-[11px] text-zinc-500">{textElementNote}</p>
          )}
          {/* State 5: text too long — inline character count warning. */}
          {textElements.some((b) => b.text.length > 500) && (
            <p className="px-1 text-[11px] text-amber-400">
              Text block exceeds 500 chars — may be truncated on render
            </p>
          )}
          <div className="rounded-xl bg-[#0c0c0e] border border-white/10 p-3">
            <UnifiedTimeline
              totalDurationS={variantDurationS}
              currentTimeS={currentTimeS}
              sfxPlacements={sfxPlacements}
              sfxGlossaryEffects={glossaryEffects}
              sfxGlossaryLoading={glossaryLoading}
              sfxRendering={variant.render_status === "rendering"}
              sfxFailed={variant.render_status === "failed"}
              sfxUploading={sfxUploading}
              sfxDirty={sfxDirty}
              onSfxChange={handleSfxChange}
              onApplySfx={handleApplySfx}
              onSfxUploadRequest={handleSfxUpload}
              overlayCards={overlayCards}
              overlaysEnabled={MEDIA_OVERLAYS_ENABLED}
              overlayUploading={overlayUploading}
              localPreviewUrls={localPreviewUrls}
              onOverlayUploadRequest={handleOverlayUpload}
              onUpdateCard={handleUpdateCard}
              onRemoveCard={handleRemoveCard}
              onClearOverlays={handleClearOverlays}
              textElements={textElements}
              onTextElementsChange={handleTextElementsChange}
              onTextApply={(bars) => {
                if (bars[0]?.role === "narrated_caption") {
                  // Narrated captions: persist + trigger reburn via Apply endpoint.
                  const cues: CaptionCue[] = bars.map((b) => ({
                    text: b.text,
                    start_s: b.start_s,
                    end_s: b.end_s,
                  }));
                  void setPlanItemCaptions(itemId, variant.variant_id, cues).then(() =>
                    applyPlanItemCaptions(itemId, variant.variant_id),
                  );
                } else if (bars[0]?.role === "generative_sequence" && variant.scene_timings?.length) {
                  // PR-E: sequence bars — flush timing patch then re-render.
                  const overrides: SceneTimingPatch[] = bars.map((b, i) => ({
                    scene_index: i,
                    start_s: b.start_s,
                    end_s: b.end_s,
                  }));
                  void patchPlanItemSceneTiming(itemId, variant.variant_id, overrides).then(() =>
                    handleApplyTextElements(variant.variant_id, bars),
                  );
                } else if (bars[0]?.role === "generative_intro" && variant.intro_start_s != null) {
                  // PR-E: intro timing bar — flush timing patch then re-render.
                  const bar = bars[0];
                  void setPlanItemIntroTiming(itemId, variant.variant_id, bar.start_s, bar.end_s).then(() =>
                    handleApplyTextElements(variant.variant_id, bars),
                  );
                } else {
                  void handleApplyTextElements(variant.variant_id, bars);
                }
              }}
              onTextTrimClamped={handleTextTrimClamped}
              isFirstSequenceEdit={
                variant.intro_mode === "sequence" && !variant.text_elements_user_edited
              }
              clipTimelineHandle={clipTimeline}
              clipsPanel={
                <InlineClipsEditor
                  ownerId={itemId}
                  variantId={variant.variant_id}
                  base="plan-item"
                  onRenderEnqueued={() => {
                    markVariantRendering(variant.variant_id, variant.render_finished_at ?? null);
                    refetch();
                  }}
                  externalState={clipTimeline.state}
                  externalDispatch={clipTimeline.dispatch}
                  externalClips={clipTimeline.clips}
                  onReload={clipTimeline.reload}
                />
              }
            />
          </div>
          {/* Text editing controls — rendered below the timeline for text-mode variants. */}
          {variant.text_mode !== "none" && (
            <div className="mt-2 space-y-3">
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
              {instantEligible && (
                <EditToolbar
                  session={session}
                  styleSets={[]}
                  fallbackSizePx={variant.intro_text_size_px}
                  resolvedParams={resolveIntroParams(variant, styleSets, session.draft)}
                />
              )}
            </div>
          )}
        </div>
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
  textElements,
}: {
  variant: PlanItemVariant;
  styleSets: GenerativeStyleSet[];
  session: VariantEditSession;
  playToken?: number;
  /**
   * T6: Full TextElement array from the variant (API data). When non-empty,
   * the preview renders ALL elements as CSS overlays instead of the single
   * IntroTextPreview (which models the legacy linear/cluster intro path).
   */
  textElements?: TextElement[];
}) {
  const introParams = resolveIntroParams(variant, styleSets, session.draft);

  // Live layout follows the draft (so toggling Classic/Editorial re-lays the
  // overlay instantly), falling back to the variant's persisted layout.
  const previewLayout =
    (session.draft.layout ?? variant.intro_layout) === "cluster" ? "cluster" : "linear";

  // When the draft is clean (no uncommitted edits, not saving), show the burned
  // output_url — byte-identical to what the download button serves. Switch to
  // the WYSIWYG DOM overlay only while the user is actively editing or a reburn
  // is in flight, giving 0-latency live preview during edits while ensuring
  // what they see at rest IS what they get.
  // (fireCommit already calls setBaseline(toCommit) so isDirty resets to false
  // as soon as a commit fires; it goes true again only on the next keystroke.)
  const burnedSrc: string | null =
    !session.isDirty && !session.isSaving ? (variant.output_url ?? null) : null;
  const burnedIdentity = `${variant.variant_id}:${variant.render_finished_at ?? ""}`;

  // N-element preview: use the text_elements array when available (T6).
  // Each element is positioned by its API-persisted x_frac/y_frac or named
  // position preset.  Font size scales by the rendered box height via CSS.
  const textLayouts =
    !burnedSrc && textElements && textElements.length > 0
      ? resolveTextElementsLayout(textElements)
      : null;

  return (
    <div className="relative aspect-[9/16] w-full overflow-hidden rounded-xl border border-zinc-200 bg-zinc-100">
      {burnedSrc ? (
        <StableVideo
          src={burnedSrc}
          identity={burnedIdentity}
          controls
          loop
          autoPlay
          muted
          playsInline
          className="h-full w-full object-contain"
        />
      ) : variant.base_video_url ? (
        // StableVideo holds the base src across re-signed polls (same base_video_path
        // identity → no reload) and only swaps when a new base video is rendered
        // (clip timeline edit changes base_video_path → identity changes → swap).
        <StableVideo
          src={variant.base_video_url}
          identity={variant.base_video_path ?? undefined}
          controls
          loop
          autoPlay
          muted
          playsInline
          className="h-full w-full object-contain"
        />
      ) : (
        <div className="flex h-full items-center justify-center text-sm text-[#71717a]">
          No preview
        </div>
      )}
      {/* N-element text overlay (T6): shows all text_elements from the API. */}
      {textLayouts ? (
        textLayouts.map((layout) => (
          <div
            key={layout.id}
            className="pointer-events-none absolute"
            style={{
              left: `${layout.xFrac * 100}%`,
              top: `${layout.yFrac * 100}%`,
              transform: "translate(-50%, -50%)",
              textAlign: layout.alignment,
              color: layout.color,
              // Scale from 1920-px canvas to the 9:16 preview box via vH-equivalent.
              // The preview box is aspect-[9/16]; its height drives the font scale.
              fontSize: `${(layout.sizePx / 1920) * 100}cqh`,
              fontFamily: `"${layout.fontFamily}", serif`,
              fontWeight: 700,
              lineHeight: 1.15,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              maxWidth: "90%",
              textShadow: "0 2px 8px rgba(0,0,0,0.7)",
            }}
          >
            {layout.text}
          </div>
        ))
      ) : (
        // Legacy single-element preview: driven by the instant-editor draft.
        !burnedSrc && (
          <IntroTextPreview params={introParams} editable={false} layout={previewLayout} playToken={playToken} />
        )
      )}
    </div>
  );
}

/** Video overlay card synced to the main edit player.
 *  Seeks to the trim-offset position in lock-step with the edit video, and
 *  mirrors play/pause so it never plays independently in a loop. */
function TrimmedVideoPreview({
  src,
  trimStart,
  trimEnd,
  mainVideoRef,
  cardStartS,
}: {
  src: string;
  trimStart: number;
  trimEnd: number | null;
  /** Ref to the main edit <video> element used for sync. */
  mainVideoRef: React.RefObject<HTMLVideoElement | null>;
  /** The card's start_s on the edit timeline (used to compute card offset). */
  cardStartS: number;
}) {
  const ref = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const card = ref.current;
    const main = mainVideoRef.current;
    if (!card) return;
    // No main video (configuration-only mode, no render yet) — just autoplay.
    if (!main) {
      card.currentTime = trimStart;
      card.play().catch(() => {});
      return;
    }

    // Seek card to its trim-offset position matching the main video's current time.
    function syncTime() {
      if (!card || !main) return;
      const cardTime = trimStart + Math.max(0, main.currentTime - cardStartS);
      const cappedTime = trimEnd !== null ? Math.min(cardTime, trimEnd) : cardTime;
      // Only seek if the drift exceeds 150ms to avoid thrashing.
      if (Math.abs(card.currentTime - cappedTime) > 0.15) {
        card.currentTime = cappedTime;
      }
    }

    const c = card, m = main;
    function onMainPlay() { c.play().catch(() => {}); syncTime(); }
    function onMainPause() { c.pause(); syncTime(); }
    function onMainTimeUpdate() { syncTime(); }
    function onMainSeeked() { syncTime(); }

    // Seed initial state.
    syncTime();
    if (!main.paused) card.play().catch(() => {});
    else card.pause();

    m.addEventListener("play", onMainPlay);
    m.addEventListener("pause", onMainPause);
    m.addEventListener("timeupdate", onMainTimeUpdate);
    m.addEventListener("seeked", onMainSeeked);
    return () => {
      m.removeEventListener("play", onMainPlay);
      m.removeEventListener("pause", onMainPause);
      m.removeEventListener("timeupdate", onMainTimeUpdate);
      m.removeEventListener("seeked", onMainSeeked);
    };
  }, [src, trimStart, trimEnd, cardStartS, mainVideoRef]);

  return <video ref={ref} src={src} muted playsInline className="w-full h-auto rounded" />;
}

/** Large hero player for the focused variant. */
function Hero({
  variant,
  generating,
  overlayCards = [],
  localPreviewUrls = {},
  sfxPlacements = [],
  sfxAudioUrls = {},
  renderingAction = null,
  showUpdatedCue = false,
}: {
  variant: PlanItemVariant | null;
  generating: boolean;
  overlayCards?: MediaOverlay[];
  localPreviewUrls?: Record<string, string>;
  sfxPlacements?: SoundEffectPlacement[];
  sfxAudioUrls?: Record<string, string>;
  /** Describes what edit is in-flight so the overlay can show a meaningful label. */
  renderingAction?: { type: "song" | "text" | "style" | "other"; label: string } | null;
  /** Show the "✓ Updated" confirmation cue for 4 s after render_finished_at advances. */
  showUpdatedCue?: boolean;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [videoTime, setVideoTime] = useState(0);

  // Sync SFX audio elements to the video playhead for instant preview.
  useSfxPreview(videoRef, sfxPlacements, sfxAudioUrls);

  // Re-attach when the video src changes (output_url becomes available after render).
  useEffect(() => {
    const el = videoRef.current;
    if (!el) return;
    const onTimeUpdate = () => setVideoTime(el.currentTime);
    el.addEventListener("timeupdate", onTimeUpdate);
    return () => el.removeEventListener("timeupdate", onTimeUpdate);
  }, [variant?.output_url]);

  if (!variant) return <SkeletonTile />;
  const rendering = variant.render_status === "rendering";
  const failed = variant.render_status === "failed";

  // StableVideo identity: composite of variant_id + render_finished_at so it
  // adopts a new src on BOTH a re-render of the same variant (render_finished_at
  // advances) and a focus switch to a different variant (variant_id changes).
  // The old video keeps playing through a re-render; the overlay dims it gently
  // and the swap happens automatically when render_finished_at advances.
  const heroIdentity = `${variant.variant_id}:${variant.render_finished_at ?? ""}`;

  // Cards visible in the CSS preview layer:
  //   - must have a blob URL (locally uploaded, not yet FFmpeg-burned)
  //   - when a rendered video exists: only show the card during its [start_s, end_s]
  //     window so the preview matches what the final render will look like
  //   - when no video yet (configuration-only mode): show all cards unconditionally
  const previewableCards = overlayCards.filter((c) => {
    if (!localPreviewUrls[c.id]) return false;
    if (!variant.output_url) return true;
    return videoTime >= c.start_s && videoTime <= c.end_s;
  });

  return (
    <div className="relative aspect-[9/16] w-full overflow-hidden rounded-xl border border-zinc-200 bg-zinc-100">
      {variant.output_url ? (
        <StableVideo
          ref={videoRef}
          src={variant.output_url}
          identity={heroIdentity}
          controls
          className="h-full w-full object-contain"
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
      {/* Instant CSS preview layer — shows uploaded cards positioned/scaled over
          the video immediately without waiting for the FFmpeg render pass. */}
      {previewableCards.map((card) => (
        <div
          key={card.id}
          style={{
            position: "absolute",
            left: `${card.x_frac * 100}%`,
            top: `${card.y_frac * 100}%`,
            transform: "translate(-50%, -50%)",
            width: `${card.scale * 100}%`,
            pointerEvents: "none",
          }}
        >
          {card.kind === "image" ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={localPreviewUrls[card.id]}
              alt=""
              className="w-full h-auto rounded"
            />
          ) : (
            <TrimmedVideoPreview
              src={localPreviewUrls[card.id]}
              trimStart={card.clip_trim_start_s ?? 0}
              trimEnd={card.clip_trim_end_s ?? null}
              mainVideoRef={videoRef}
              cardStartS={card.start_s}
            />
          )}
        </div>
      ))}
      {/* While a re-render runs, keep old video playing under a gentle overlay.
          pointer-events-none ensures the video controls beneath remain usable. */}
      {rendering && variant.output_url && (
        <div className="pointer-events-none absolute inset-0" role="status" aria-label="Rendering new version">
          <div className="absolute inset-0 bg-white/25" />
          <ShimmerSweep tone="light" />
          <HeroRenderingLabel
            startedAt={variant.render_started_at ?? null}
            action={renderingAction}
          />
        </div>
      )}
      {/* "✓ Updated" confirmation — flashes for 4 s when the new video swaps in. */}
      {showUpdatedCue && !rendering && variant.output_url && (
        <div className="pointer-events-none absolute inset-0 flex items-end justify-center pb-5">
          <span className="rounded-full bg-lime-600/90 px-3.5 py-1.5 text-xs font-semibold text-white shadow-sm">
            ✓ Updated
          </span>
        </div>
      )}
    </div>
  );
}

/** Status label shown during a same-variant re-render, with a stall hint after 5 min.
 *  Shows action-specific copy when `action` is provided (e.g. the picked song name). */
function HeroRenderingLabel({
  startedAt,
  action,
}: {
  startedAt: string | null;
  action?: { type: "song" | "text" | "style" | "other"; label: string } | null;
}) {
  const STALL_HINT_MS = 300_000; // 5 min
  const [elapsed, setElapsed] = useState(() =>
    startedAt ? Date.now() - new Date(startedAt).getTime() : 0,
  );
  useEffect(() => {
    const id = setInterval(() => {
      setElapsed(startedAt ? Date.now() - new Date(startedAt).getTime() : 0);
    }, 5000);
    return () => clearInterval(id);
  }, [startedAt]);

  if (elapsed >= STALL_HINT_MS) {
    return (
      <div className="absolute inset-0 flex flex-col items-center justify-end pb-6 gap-1 text-center">
        <span className="rounded-full bg-white/80 px-3 py-1 text-xs text-[#3f3f46]">
          Taking longer than usual…
        </span>
      </div>
    );
  }

  // Song swap: full re-render takes ~1-3 min — show the song name + duration hint.
  if (action?.type === "song") {
    return (
      <div className="absolute inset-0 flex flex-col items-center justify-end pb-6 gap-1.5 text-center">
        <span className="rounded-full bg-white/90 px-3 py-1 text-[11px] font-medium text-lime-700 leading-tight max-w-[85%] truncate">
          Applying &ldquo;{action.label}&rdquo;
        </span>
        <span className="rounded-full bg-white/70 px-2.5 py-0.5 text-[10px] text-[#71717a]">
          ~1–3 min
        </span>
      </div>
    );
  }

  // Text reburn: fast path, a few seconds.
  if (action?.type === "text") {
    return (
      <div className="absolute inset-0 flex flex-col items-center justify-end pb-6 gap-1 text-center">
        <span className="rounded-full bg-white/80 px-3 py-1 text-xs text-lime-700">
          {action.label || "Updating text…"}
        </span>
        <span className="rounded-full bg-white/70 px-2.5 py-0.5 text-[10px] text-[#71717a]">
          a few seconds
        </span>
      </div>
    );
  }

  // Style / size / layout / generic re-render.
  const genericLabel = action?.label ?? "Rendering new version…";
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-end pb-6 gap-1 text-center">
      <span className="rounded-full bg-white/80 px-3 py-1 text-xs text-lime-700">
        {genericLabel}
      </span>
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

