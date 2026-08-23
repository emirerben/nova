"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { StableVideo } from "@/components/StableVideo";
import {
  changeVariantStyle,
  editVariant,
  getGenerativeJobStatus,
  getGenerativeStyleSets,
  GENERATIVE_SUCCESS_STATUSES,
  isGenerativeJobSettled,
  retextVariant,
  setVariantIntroSize,
  swapVariantSong,
  type GenerativeJobStatus,
  type GenerativeStyleSet,
  type GenerativeVariant,
} from "@/lib/generative-api";
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";
import { ProgressTheater } from "@/components/progress";
import { GENERATIVE_PHASE_ORDER, GENERATIVE_PHASE_LABEL } from "@/lib/job-phases";
import { TEXT_MODE_LABEL } from "@/app/generative/VariantCard";
import { InlineClipsEditor } from "@/app/plan/_components/InlineClipsEditor";
import { downloadVideo } from "@/lib/download-video";
import { deriveReceiptText } from "@/components/progress/logic";
import { jobFailureCopy } from "@/lib/job-failure-copy";
import { getMusicTracks, type MusicTrackSummary } from "@/lib/music-api";
import PlanVariantEditor from "@/app/plan/_components/PlanVariantEditor";
import type { PlanItemVariant } from "@/lib/plan-api";
import { useVariantEditSession } from "@/lib/variant-editor/useVariantEditSession";
import { EditToolbar } from "@/components/variant-editor/EditToolbar";
import { IntroTextPreview } from "@/components/variant-editor/IntroTextPreview";
import { isInstantEditEligible } from "@/lib/variant-editor/eligibility";
import { resolveIntroParams } from "@/components/variant-editor/resolve-intro-params";
import type { EditableVariant } from "@/lib/variant-editor/types";

function isSuccessStatus(status: string): boolean {
  return GENERATIVE_SUCCESS_STATUSES.includes(status);
}


/**
 * Two-column panel for ONE focused variant.
 * LEFT: hero video.
 * RIGHT: PlanVariantEditor — same rich controls as the plan-item page.
 * Keyed by variant_id so session hooks reset on variant switch.
 */
function FocusedVariantPanel({
  variant,
  jobId,
  tracks,
  styleSets,
  refresh,
}: {
  variant: GenerativeVariant;
  jobId: string;
  tracks: MusicTrackSummary[];
  styleSets: GenerativeStyleSet[];
  refresh: () => void;
}) {
  const session = useVariantEditSession(
    variant as unknown as EditableVariant,
    async (payload) => {
      await editVariant(jobId, variant.variant_id, payload);
      refresh();
    },
  );

  const instantEligible = isInstantEditEligible(variant as unknown as EditableVariant);
  const [clipsOpen, setClipsOpen] = useState(false);

  useEffect(() => {
    if (!session.isSaving) return;
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [session.isSaving, refresh]);

  // StableVideo handles URL stability; no manual pin ref needed.

  const rendering = variant.render_status === "rendering";
  const failed = variant.render_status === "failed";

  const introParams = resolveIntroParams(
    variant as unknown as EditableVariant,
    styleSets,
    session.draft,
    session.isDirty,
  );

  // For instant-eligible variants: always show the text-free base video +
  // IntroTextPreview overlay so font/animation/color/text changes are visible
  // live (0-latency, no re-render). Matches the plan-item page WYSIWYG pattern.
  const showWysiwyg = instantEligible && !!variant.base_video_url;

  return (
    <div className="flex flex-col gap-6 lg:flex-row lg:items-start">
      {/* LEFT: hero video — fixed narrow column so the editor gets the room */}
      <div className="w-full shrink-0 sm:max-w-[260px] lg:w-[260px]">
        <div className="relative aspect-[9/16] w-full overflow-hidden rounded-xl border border-zinc-200 bg-zinc-100">
          {showWysiwyg ? (
            <>
              <StableVideo
                src={variant.base_video_url}
                identity={variant.base_video_path ?? undefined}
                autoPlay
                loop
                muted
                playsInline
                className="absolute inset-0 h-full w-full object-contain"
              />
              {!session.draft.removed && (
                <div className="absolute inset-0">
                  <IntroTextPreview
                    params={introParams}
                    layout={
                      session.draft.layout ??
                      (variant.intro_layout as "linear" | "cluster" | null) ??
                      "linear"
                    }
                    playToken={session.playToken}
                  />
                </div>
              )}
              {session.isSaving && (
                <div className="absolute inset-0 flex items-end justify-center pb-4">
                  <span className="rounded-full bg-black/60 px-3 py-1 text-xs text-white">
                    Saving…
                  </span>
                </div>
              )}
            </>
          ) : rendering || session.isSaving ? (
            <div className="flex h-full items-center justify-center text-sm text-[#71717a]">
              Rendering…
            </div>
          ) : failed ? (
            <div className="flex h-full items-center justify-center px-3 text-center text-sm text-[#3f3f46]">
              We couldn&apos;t render this version.
            </div>
          ) : variant.output_url ? (
            <StableVideo
              src={variant.output_url}
              identity={variant.render_finished_at ?? undefined}
              controls
              className="h-full w-full object-contain"
            />
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-[#71717a]">
              Preview will appear after the first render.
            </div>
          )}
        </div>
        {!failed && variant.output_url && (
          <Button
            type="button"
            variant="outline"
            disabled={session.isSaving}
            onClick={
              session.isSaving
                ? undefined
                : () =>
                    downloadVideo(
                      variant.download_url ?? variant.output_url!,
                      `kria-${variant.variant_id}.mp4`,
                      !variant.download_url,
                    )
            }
            className={`mt-2 h-auto min-h-[44px] w-full rounded-lg py-2 text-xs focus-visible:ring-lime-600 ${
              session.isSaving
                ? "border-zinc-200 text-[#a1a1aa] cursor-not-allowed"
                : "border-zinc-200 text-[#3f3f46] hover:border-zinc-400"
            }`}
          >
            {session.isSaving ? "Saving…" : "Download"}
          </Button>
        )}
      </div>

      {/* RIGHT: Font/Animation/Color/TextSize via EditToolbar (same as plan-item page) */}
      <div className="min-w-0 flex-1">
        <PlanVariantEditor
          variant={variant as unknown as PlanItemVariant}
          tracks={tracks}
          styleSets={instantEligible ? [] : styleSets}
          onSwap={async (trackId) => {
            await swapVariantSong(jobId, variant.variant_id, trackId);
            refresh();
          }}
          onRetext={
            instantEligible
              ? async (text) => {
                  session.setText(text);
                }
              : async (text) => {
                  await retextVariant(jobId, variant.variant_id, { text });
                  refresh();
                }
          }
          onRemoveText={
            instantEligible
              ? async () => {
                  session.setRemoved(true);
                }
              : async () => {
                  await retextVariant(jobId, variant.variant_id, { remove: true });
                  refresh();
                }
          }
          onChangeStyle={async (styleSetId) => {
            if (instantEligible) {
              session.setStyle(styleSetId);
            } else {
              await changeVariantStyle(jobId, variant.variant_id, styleSetId);
              refresh();
            }
          }}
          onResize={
            instantEligible
              ? undefined
              : async (px) => {
                  await setVariantIntroSize(jobId, variant.variant_id, px);
                  refresh();
                }
          }
          onChangeLayout={async (layout) => {
            if (instantEligible) {
              session.setLayout(layout);
            } else {
              await editVariant(jobId, variant.variant_id, { intro_layout: layout });
              refresh();
            }
          }}
          onEditClips={() => setClipsOpen((o) => !o)}
          showClipEditor
          clipSlotCount={null}
          hasClipEdits={false}
        />
        {instantEligible && (
          <div className="mt-6">
            <EditToolbar
              session={session}
              styleSets={[]}
              fallbackSizePx={variant.intro_text_size_px ?? null}
              resolvedParams={introParams}
            />
          </div>
        )}
        {clipsOpen && (
          <div className="mt-4">
            <InlineClipsEditor
              ownerId={jobId}
              variantId={variant.variant_id}
              base="generative"
              onRenderEnqueued={() => { setClipsOpen(false); refresh(); }}
            />
          </div>
        )}
      </div>
    </div>
  );
}

export function EditPayoff({
  jobId,
  onMakePlan,
  onReRoll,
  hidePlanCta = false,
}: {
  jobId: string;
  onMakePlan: () => void;
  onReRoll: () => void;
  hidePlanCta?: boolean;
}) {
  const [tracks, setTracks] = useState<MusicTrackSummary[]>([]);
  const [styleSets, setStyleSets] = useState<GenerativeStyleSet[]>([]);
  const [focusedVariantId, setFocusedVariantId] = useState<string | null>(null);

  useEffect(() => {
    getMusicTracks()
      .then((r) => setTracks(r.tracks))
      .catch(() => setTracks([]));
    getGenerativeStyleSets()
      .then((s) => setStyleSets(s))
      .catch(() => setStyleSets([]));
  }, []);

  const fetcher = useCallback(async () => {
    return getGenerativeJobStatus(jobId);
  }, [jobId]);

  const isTerminalAndDone = useCallback(
    (data: GenerativeJobStatus) => isGenerativeJobSettled(data.status, data.variants),
    [],
  );

  const { data: status, refetch } = usePolledJobStatus<GenerativeJobStatus>(
    fetcher,
    undefined,
    isTerminalAndDone,
  );

  const refresh = useCallback(() => {
    refetch();
  }, [refetch]);

  const theaterIsTerminal = status != null && isTerminalAndDone(status);
  const theaterIsSuccess = status != null && isSuccessStatus(status.status);
  const receiptText = status ? deriveReceiptText(status.started_at ?? status.created_at, status.finished_at ?? status.updated_at) : "Your video is ready";

  const currentPhase: string | null = (() => {
    if (!status) return null;
    if (status.current_phase) return status.current_phase;
    if (!status.started_at) return "queued";
    return null;
  })();

  const allVariants = (status?.variants ?? []) as GenerativeVariant[];

  // Stable string dep so the auto-focus effect doesn't re-run on every poll.
  const firstReadyVariantId =
    allVariants.find((v) => v.render_status === "ready")?.variant_id ?? null;

  // Auto-focus first ready variant when it arrives.
  useEffect(() => {
    if (focusedVariantId || !firstReadyVariantId) return;
    setFocusedVariantId(firstReadyVariantId);
  }, [focusedVariantId, firstReadyVariantId]);

  const focusedVariant =
    allVariants.find((v) => v.variant_id === focusedVariantId) ??
    allVariants.find((v) => v.render_status === "ready") ??
    null;

  // Total failure state
  if (status?.status === "processing_failed") {
    const failure = jobFailureCopy(status.error_detail);
    return (
      <div className="flex flex-col gap-6 px-4 py-8 max-w-lg mx-auto text-center animate-fade-up">
        <p className="text-[#71717a]">
          {failure.detail}
        </p>
        <Button
          type="button"
          variant="link"
          onClick={onReRoll}
          className="h-auto min-h-[44px] p-0 text-sm text-lime-700 underline hover:text-lime-900"
        >
          Create another version
        </Button>
        <Button
          type="button"
          variant="ghost"
          onClick={onMakePlan}
          className="h-auto min-h-[44px] p-0 text-sm text-[#71717a] hover:bg-transparent hover:text-[#0c0c0e]"
        >
          Create my content plan
        </Button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6 py-4 animate-fade-up">
      {/* Progress theater — shows phases while rendering, receipt when done */}
      <ProgressTheater
        phases={GENERATIVE_PHASE_ORDER}
        phaseLabels={GENERATIVE_PHASE_LABEL}
        currentPhase={currentPhase}
        expectedPhaseMs={status?.expected_phase_durations ?? null}
        phaseLog={status?.phase_log ?? null}
        startedAt={status?.started_at ?? null}
        jobCreatedAt={status?.created_at ?? new Date().toISOString()}
        isTerminal={theaterIsTerminal}
        isSuccess={theaterIsSuccess}
        receiptText={receiptText}
        variants={status?.variants ?? null}
        retrying={status?.retrying ?? false}
        size="full"
        tone="light"
      >
        {null}
      </ProgressTheater>

      {/* Two-column edit layout — shown once terminal+success */}
      {theaterIsTerminal && theaterIsSuccess && (
        <div className="flex flex-col gap-6">
          {/* Variant filmstrip — only shown when 2+ variants */}
          {allVariants.length > 1 && (
            <div
              className="flex gap-3 overflow-x-auto pb-1"
              role="radiogroup"
              aria-label="Edit variants"
            >
              {allVariants.map((v) => {
                const isSelected = v.variant_id === (focusedVariant?.variant_id ?? "");
                const isReady = v.render_status === "ready";
                const isFailed = v.render_status === "failed";
                return (
                  <Button
                    key={v.variant_id}
                    type="button"
                    variant="ghost"
                    role="radio"
                    aria-checked={isSelected}
                    aria-label={TEXT_MODE_LABEL[v.text_mode] ?? v.text_mode}
                    disabled={!isReady}
                    onClick={() => setFocusedVariantId(v.variant_id)}
                    className="flex h-auto w-auto shrink-0 flex-col items-center gap-1 rounded p-0 hover:bg-transparent focus-visible:ring-lime-600"
                  >
                    <div
                      className={[
                        "w-16 aspect-[9/16] shrink-0 overflow-hidden rounded-md border bg-zinc-100",
                        isSelected
                          ? "border-lime-600 ring-1 ring-lime-600"
                          : isReady
                          ? "border-zinc-200 hover:border-zinc-400"
                          : "border-zinc-200 opacity-50",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                    >
                      {isReady && v.output_url ? (
                        <StableVideo
                          src={v.output_url}
                          identity={v.render_finished_at ?? undefined}
                          className="h-full w-full object-cover"
                          muted
                          playsInline
                        />
                      ) : isFailed ? (
                        <div className="flex h-full items-center justify-center text-[10px] text-[#71717a]">
                          —
                        </div>
                      ) : (
                        <div className="flex h-full items-center justify-center">
                          <div className="w-3 h-3 border border-zinc-400 border-t-transparent rounded-full animate-spin motion-reduce:animate-none" />
                        </div>
                      )}
                    </div>
                    <span className="w-16 truncate text-center text-[10px] text-[#71717a]">
                      {TEXT_MODE_LABEL[v.text_mode] ?? v.text_mode}
                    </span>
                  </Button>
                );
              })}
            </div>
          )}

          {/* Focused variant two-column panel */}
          {focusedVariant && (
            <FocusedVariantPanel
              key={focusedVariant.variant_id}
              variant={focusedVariant}
              jobId={jobId}
              tracks={tracks}
              styleSets={styleSets}
              refresh={refresh}
            />
          )}
        </div>
      )}

      {/* Re-roll */}
      <Button
        type="button"
        variant="ghost"
        onClick={onReRoll}
        className="h-auto min-h-[44px] rounded text-center text-xs text-[#a1a1aa] hover:bg-transparent hover:text-[#71717a] focus-visible:ring-lime-600"
      >
        Try different footage or style
      </Button>

      {/* Second-act plan CTA (suppressed when stacked with other jobs) */}
      {!hidePlanCta && (
        <div className="border-t border-[#e4e4e7] pt-6">
          <p className="text-center text-sm text-[#71717a] mb-3">
            Want a content plan from this footage?
          </p>
          <Button
            type="button"
            onClick={onMakePlan}
            className="h-auto min-h-[44px] w-full rounded-xl bg-[#0c0c0e] py-3 font-medium text-white hover:opacity-80 focus-visible:ring-lime-600"
          >
            Create my content plan
          </Button>
        </div>
      )}
    </div>
  );
}
