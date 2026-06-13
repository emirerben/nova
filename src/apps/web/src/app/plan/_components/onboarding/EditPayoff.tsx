"use client";

import { useCallback, useState } from "react";
import {
  getGenerativeJobStatus,
  GENERATIVE_TERMINAL_STATUSES,
  type GenerativeJobStatus,
  type GenerativeVariant,
  type GenerativeStyleSet,
} from "@/lib/generative-api";
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";
import { ProgressTheater, PayoffField } from "@/components/progress";
import { GENERATIVE_PHASE_ORDER, GENERATIVE_PHASE_LABEL } from "@/lib/job-phases";
import { VariantTile } from "@/app/generative/VariantTile";
import { formatElapsed } from "@/components/progress/logic";
import { getMusicTracks, type MusicTrackSummary } from "@/lib/music-api";
import { getGenerativeStyleSets } from "@/lib/generative-api";
import { useEffect } from "react";

function isTerminalStatus(data: GenerativeJobStatus): boolean {
  return GENERATIVE_TERMINAL_STATUSES.includes(data.status);
}

function isSuccessStatus(status: string): boolean {
  return status === "variants_ready" || status === "variants_ready_partial";
}

function deriveReceiptText(status: GenerativeJobStatus): string {
  const startRaw = status.started_at ?? status.created_at;
  const endRaw = status.finished_at ?? status.updated_at;
  if (!startRaw || !endRaw) return "Your edits are ready";
  const elapsedMs = new Date(endRaw).getTime() - new Date(startRaw).getTime();
  if (elapsedMs <= 0) return "Your edits are ready";
  return `Ready in ${formatElapsed(elapsedMs)}`;
}

export function EditPayoff({
  jobId,
  onMakePlan,
  onReRoll,
}: {
  jobId: string;
  onMakePlan: () => void;
  onReRoll: () => void;
}) {
  const [tracks, setTracks] = useState<MusicTrackSummary[]>([]);
  const [styleSets, setStyleSets] = useState<GenerativeStyleSet[]>([]);

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
    (data: GenerativeJobStatus) => isTerminalStatus(data),
    [],
  );

  const {
    data: status,
    refetch,
  } = usePolledJobStatus<GenerativeJobStatus>(fetcher, undefined, isTerminalAndDone);

  const refresh = useCallback(() => { refetch(); }, [refetch]);

  const theaterIsTerminal = status != null && isTerminalAndDone(status);
  const theaterIsSuccess = status != null && isSuccessStatus(status.status);
  const receiptText = status ? deriveReceiptText(status) : "Your edits are ready";

  const currentPhase: string | null = (() => {
    if (!status) return null;
    if (status.current_phase) return status.current_phase;
    if (!status.started_at) return "queued";
    return null;
  })();

  // Total failure state
  if (status?.status === "processing_failed") {
    return (
      <div className="flex flex-col gap-6 px-4 py-8 max-w-lg mx-auto text-center animate-fade-up">
        <p className="text-[#71717a]">
          {status.error_detail ?? "We couldn't finish your edit right now."}
        </p>
        <button
          onClick={onReRoll}
          className="text-sm text-lime-700 underline hover:text-lime-900 min-h-[44px]"
        >
          Try again
        </button>
        <button
          onClick={onMakePlan}
          className="text-sm text-[#71717a] hover:text-[#0c0c0e] min-h-[44px]"
        >
          Make a plan instead
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6 px-4 py-8 max-w-2xl mx-auto animate-fade-up">
      {/* About 90 seconds expectation-setter */}
      {!theaterIsTerminal && (
        <p className="text-center text-xs text-[#a1a1aa]">
          About 90 seconds to render
        </p>
      )}

      {/*
       * ProgressTheater with tone="light" for cream canvas.
       * PayoffField inside renders VariantTile for each variant.
       */}
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
        size="full"
        tone="light"
      >
        <PayoffField
          variants={status?.variants ?? null}
          tone="light"
          renderCard={(variant, isNewlyReady) => {
            const gv = variant as GenerativeVariant;
            return (
              <VariantTile
                key={gv.variant_id}
                variant={gv}
                jobId={jobId}
                tracks={tracks}
                styleSets={styleSets}
                isNewlyReady={isNewlyReady}
                onRetry={() => {}}
                refresh={refresh}
              />
            );
          }}
        />
      </ProgressTheater>

      {/* Re-roll */}
      <button
        onClick={onReRoll}
        className="text-xs text-[#a1a1aa] hover:text-[#71717a] text-center focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 rounded min-h-[44px]"
      >
        try different clips or style
      </button>

      {/* Divider — second act */}
      <div className="border-t border-[#e4e4e7] pt-6">
        <p className="text-center text-sm text-[#71717a] mb-3">
          Want a 30-day content plan using this footage?
        </p>
        <button
          onClick={onMakePlan}
          className="w-full rounded-xl bg-[#0c0c0e] text-white py-3 font-medium hover:opacity-80 focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 min-h-[44px]"
        >
          Make my plan →
        </button>
      </div>
    </div>
  );
}
