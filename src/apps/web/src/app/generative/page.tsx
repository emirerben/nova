"use client";

import { useCallback, useEffect, useState } from "react";
import {
  changeVariantStyle,
  createGenerativeJob,
  getGenerativeJobStatus,
  getGenerativeStyleSets,
  GENERATIVE_TERMINAL_STATUSES,
  retextVariant,
  setVariantIntroSize,
  setVariantMix,
  swapVariantSong,
  uploadGenerativeClip,
  type GenerativeJobStatus,
  type GenerativeStyleSet,
  type GenerativeVariant,
} from "@/lib/generative-api";
import { getMusicTracks, type MusicTrackSummary } from "@/lib/music-api";
import { VariantCard } from "./VariantCard";
import { VoiceRecorder } from "./VoiceRecorder";
import {
  GENERATIVE_PHASE_ORDER,
  GENERATIVE_PHASE_LABEL,
} from "@/lib/job-phases";
import {
  ProgressTheater,
  PayoffField,
  VariantRenderCard,
} from "@/components/progress";
import { formatElapsed } from "@/components/progress/logic";
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";

// ===== Helpers =====

function isTerminalStatus(data: GenerativeJobStatus): boolean {
  return GENERATIVE_TERMINAL_STATUSES.includes(data.status);
}

function isSuccessStatus(status: string): boolean {
  return status === "variants_ready" || status === "variants_ready_partial";
}

/**
 * D12 receipt text: "Ready in m:ss"
 * Falls back to created_at / updated_at when PR2 fields aren't present.
 */
function deriveReceiptText(status: GenerativeJobStatus): string {
  const startRaw = status.started_at ?? status.created_at;
  const endRaw = status.finished_at ?? status.updated_at;
  if (!startRaw || !endRaw) return "Your edits are ready";
  const elapsedMs = new Date(endRaw).getTime() - new Date(startRaw).getTime();
  if (elapsedMs <= 0) return "Your edits are ready";
  return `Ready in ${formatElapsed(elapsedMs)}`;
}

// ===== Page =====

export default function GenerativePage() {
  const [uploads, setUploads] = useState<{ gcs_path: string; name: string }[]>([]);
  const [voiceoverPath, setVoiceoverPath] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [tracks, setTracks] = useState<MusicTrackSummary[]>([]);
  const [styleSets, setStyleSets] = useState<GenerativeStyleSet[]>([]);
  const [styleSetsError, setStyleSetsError] = useState(false);

  // Style sets — retryable so a transient blip doesn't permanently hide the picker.
  const loadStyleSets = useCallback(() => {
    getGenerativeStyleSets()
      .then((s) => {
        setStyleSets(s);
        setStyleSetsError(false);
      })
      .catch(() => {
        setStyleSets([]);
        setStyleSetsError(true);
      });
  }, []);

  // Song library for the swap picker + style sets.
  useEffect(() => {
    getMusicTracks()
      .then((r) => setTracks(r.tracks))
      .catch(() => setTracks([]));
    loadStyleSets();
  }, [loadStyleSets]);

  // ===== Polling (replaces hand-rolled setTimeout loop) =====
  //
  // usePolledJobStatus handles:
  //   - D8: visibilitychange → immediate refetch (built in)
  //   - transient error resilience (keeps polling on fetch failure)
  //   - terminal stop (stops polling when isTerminalAndDone returns true)
  //
  // We also poll while any variant is still rendering after a terminal status —
  // swap/retext flips a variant back to "rendering" while the job status stays terminal.

  const fetcher = useCallback(async () => {
    if (!jobId) throw new Error("no jobId");
    return getGenerativeJobStatus(jobId);
  }, [jobId]);

  const isTerminalAndDone = useCallback(
    (data: GenerativeJobStatus) => {
      const terminal = isTerminalStatus(data);
      const anyRendering = data.variants?.some((v) => v.render_status === "rendering") ?? false;
      return terminal && !anyRendering;
    },
    [],
  );

  const {
    data: status,
    error: pollError,
    refetch,
  } = usePolledJobStatus<GenerativeJobStatus>(
    fetcher,
    undefined, // use default POLL_INTERVAL_MS
    isTerminalAndDone,
  );

  // ===== Upload / submit handlers =====

  const handleFiles = useCallback(async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    setSubmitError(null);
    try {
      const results = await Promise.all(
        Array.from(files).map(async (f) => {
          const r = await uploadGenerativeClip(f);
          return { gcs_path: r.gcs_path, name: f.name };
        }),
      );
      setUploads((prev) => [...prev, ...results]);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }, []);

  const handleGenerate = useCallback(async () => {
    setSubmitError(null);
    try {
      const res = await createGenerativeJob(
        uploads.map((u) => u.gcs_path),
        voiceoverPath,
      );
      setJobId(res.job_id);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : "Failed to start");
    }
  }, [uploads, voiceoverPath]);

  // ===== Variant mutation: trigger re-render after any mutation =====
  const refresh = useCallback(() => {
    refetch();
  }, [refetch]);

  // ===== D10 retry handler for failed variants =====
  const handleRetry = useCallback(
    async (variantId: string) => {
      if (!jobId) return;
      try {
        // Use a no-op retext to re-queue the variant render.
        // If a dedicated retry endpoint ships later, swap it in here.
        await retextVariant(jobId, variantId, {});
      } catch {
        // Best-effort — the next poll cycle will reflect real state.
      }
      refetch();
    },
    [jobId, refetch],
  );

  // ===== Theater props derivation =====

  const theaterIsTerminal = status != null && isTerminalAndDone(status);
  const theaterIsSuccess = status != null && isSuccessStatus(status.status);
  const receiptText = status ? deriveReceiptText(status) : "Your edits are ready";

  // D9: before started_at lands, currentPhase defaults to "queued".
  // Deploy-skew: if current_phase is null AND started_at is null → treat as queued.
  // ProgressTheater handles null gracefully (ETA suppressed, no crash).
  const currentPhase: string | null = (() => {
    if (!status) return null;
    if (status.current_phase) return status.current_phase;
    if (!status.started_at) return "queued";
    return null;
  })();

  // ===== Render =====

  return (
    <main className="min-h-[calc(100vh-3.5rem)] bg-black text-white">
      <div className="max-w-5xl mx-auto px-4 py-12">
        <h1 className="text-2xl font-semibold mb-2">Generative edit</h1>
        <p className="text-zinc-400 mb-8">
          Upload your clips. We pick a song, write the text, and give you a few versions to choose from.
        </p>

        {/* Submit-phase errors (upload / job creation) */}
        {submitError && (
          <div className="mb-6 rounded border border-red-700 bg-red-950/50 px-4 py-3 text-red-200">
            {submitError}
          </div>
        )}

        {/* Transient poll errors — don't hide the theater, just surface a note */}
        {pollError && status && (
          <div className="mb-4 rounded border border-amber-700/60 bg-amber-950/40 px-4 py-2 text-sm text-amber-200">
            Trouble reaching the server — retrying…
          </div>
        )}

        {/* ===== Upload form (shown when no job has been submitted yet) ===== */}
        {!jobId && (
          <section className="space-y-5">
            <div>
              <label className="block text-sm text-zinc-400 mb-2">Clips</label>
              <input
                type="file"
                accept="video/*,image/*"
                multiple
                disabled={uploading}
                onChange={(e) => handleFiles(e.target.files)}
                className="block w-full text-sm text-zinc-300 file:mr-4 file:rounded file:border-0 file:bg-zinc-800 file:px-4 file:py-2 file:text-white"
              />
              {uploading && <p className="mt-2 text-sm text-zinc-500">Uploading…</p>}
              {uploads.length > 0 && (
                <ul className="mt-3 space-y-1 text-sm text-zinc-400">
                  {uploads.map((u, i) => (
                    <li key={i}>• {u.name}</li>
                  ))}
                </ul>
              )}
            </div>

            <div>
              <label className="block text-sm text-zinc-400 mb-2">Voiceover (optional)</label>
              <VoiceRecorder onVoiceover={setVoiceoverPath} />
            </div>

            <p className="text-xs text-zinc-500">
              Length is set automatically from your clips and the matched song —
              the edit is never longer than the footage you upload.
            </p>

            <button
              onClick={handleGenerate}
              disabled={uploads.length === 0 || uploading}
              className="rounded bg-white px-6 py-2.5 font-medium text-black disabled:opacity-40"
            >
              Generate edits
            </button>
            <p className="text-xs text-zinc-500">
              {voiceoverPath
                ? "We'll build voiceover edits around your recording — sync your footage to your voice."
                : "Add a voiceover above and you'll get voiceover edits instead."}
            </p>
          </section>
        )}

        {/* ===== Progress theater (shown immediately once job is submitted) ===== */}
        {jobId && (
          <section>
            {/* Style-sets blip warning */}
            {styleSetsError && styleSets.length === 0 && (
              <div className="mb-4 flex items-center gap-3 rounded border border-amber-700/60 bg-amber-950/40 px-4 py-2 text-sm text-amber-200">
                <span>Couldn&apos;t load text styles — the style picker is hidden.</span>
                <button
                  onClick={loadStyleSets}
                  className="rounded border border-amber-600 px-2 py-0.5 text-xs text-amber-100 hover:bg-amber-900/50"
                >
                  Retry
                </button>
              </div>
            )}

            {/* Total failure — no variants produced */}
            {status?.status === "processing_failed" && (
              <div className="mb-6">
                <p className="text-red-300 mb-4">
                  {status.error_detail ?? "Something went wrong — we couldn't process your clips."}
                </p>
                <button
                  onClick={() => {
                    setJobId(null);
                    setUploads([]);
                    setSubmitError(null);
                  }}
                  className="rounded border border-zinc-700 px-4 py-2 text-sm text-zinc-300"
                >
                  Start over
                </button>
              </div>
            )}

            {/*
             * D5 ProgressTheater.
             *
             * Visible from the moment jobId lands — no waiting for the first poll.
             * Deploy-skew: null phase fields → shimmer + ETA suppressed, no crash.
             * D9 queued state: currentPhase="queued" before started_at arrives.
             * D12 receipt: collapses to "Ready in m:ss" after CELEBRATION_HOLD_MS.
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
            >
              {/*
               * D7 PayoffField — shimmer until variants array is populated.
               * Slot count always from variants.length, never a hard-coded constant.
               */}
              <PayoffField
                variants={status?.variants ?? null}
                renderCard={(variant, isNewlyReady) => {
                  // variant conforms to VariantLike; cast to the richer type for controls.
                  const gv = variant as GenerativeVariant;

                  return (
                    <div key={gv.variant_id} className="flex flex-col gap-4">
                      {/* D10: VariantRenderCard maps error_class → human copy */}
                      <VariantRenderCard
                        variant={gv}
                        isNewlyReady={isNewlyReady}
                        onRetry={() => void handleRetry(gv.variant_id)}
                      />

                      {/* Swap / retext / style / resize / mix controls —
                          only visible once the variant is ready */}
                      {gv.render_status === "ready" && (
                        <VariantCard
                          variant={gv}
                          tracks={tracks}
                          styleSets={styleSets}
                          onSwap={async (trackId) => {
                            await swapVariantSong(jobId, gv.variant_id, trackId);
                            refresh();
                          }}
                          onRetext={async (text) => {
                            await retextVariant(jobId, gv.variant_id, { text });
                            refresh();
                          }}
                          onRemoveText={async () => {
                            await retextVariant(jobId, gv.variant_id, { remove: true });
                            refresh();
                          }}
                          onChangeStyle={async (styleSetId) => {
                            await changeVariantStyle(jobId, gv.variant_id, styleSetId);
                            refresh();
                          }}
                          onResize={async (px) => {
                            await setVariantIntroSize(jobId, gv.variant_id, px);
                            refresh();
                          }}
                          onSetMix={async (mix) => {
                            await setVariantMix(jobId, gv.variant_id, mix);
                            refresh();
                          }}
                        />
                      )}
                    </div>
                  );
                }}
              />
            </ProgressTheater>
          </section>
        )}
      </div>
    </main>
  );
}
