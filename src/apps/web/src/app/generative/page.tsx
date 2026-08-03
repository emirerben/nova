"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  createGenerativeJob,
  getGenerativeJobStatus,
  getGenerativeStyleSets,
  GENERATIVE_SUCCESS_STATUSES,
  isGenerativeJobSettled,
  retextVariant,
  uploadGenerativeClip,
  type GenerativeJobStatus,
  type GenerativeStyleSet,
  type GenerativeVariant,
} from "@/lib/generative-api";
import { getMusicTracks, type MusicTrackSummary } from "@/lib/music-api";
import { VariantTile } from "./VariantTile";
import { VoiceRecorder } from "./VoiceRecorder";
import { FONT_FACES } from "@/lib/font-faces";
import {
  GENERATIVE_PHASE_ORDER,
  GENERATIVE_PHASE_LABEL,
} from "@/lib/job-phases";
import {
  ProgressTheater,
  PayoffField,
} from "@/components/progress";
import { deriveReceiptText } from "@/components/progress/logic";
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";
import { LightShell } from "@/components/ui/LightShell";
import { Eyebrow } from "@/components/ui/Eyebrow";
import { InkButton } from "@/components/ui/InkButton";

// ===== Helpers =====

function isSuccessStatus(status: string): boolean {
  return GENERATIVE_SUCCESS_STATUSES.includes(status);
}

const MAX_GENERATIVE_CLIPS = 20;
type UploadedClip = { gcs_path: string; name: string; order: number };
type PendingClip = { file: File; order: number };

// ===== Page =====

export default function GenerativePage() {
  const [uploads, setUploads] = useState<UploadedClip[]>([]);
  const [failedUploads, setFailedUploads] = useState<PendingClip[]>([]);
  const nextUploadOrder = useRef(0);
  const clipInputRef = useRef<HTMLInputElement>(null);
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

  // Resume an existing job via ?job=<id> — recovers the results view after a
  // refresh (in-memory state otherwise loses the job) and doubles as the QA
  // entry point for the instant editor. Read from window (not useSearchParams)
  // to avoid the app-router Suspense-boundary build requirement.
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("job");
    if (id) setJobId(id);
  }, []);

  // ===== Polling =====
  const fetcher = useCallback(async () => {
    if (!jobId) throw new Error("no jobId");
    return getGenerativeJobStatus(jobId);
  }, [jobId]);

  const isTerminalAndDone = useCallback(
    (data: GenerativeJobStatus) => isGenerativeJobSettled(data.status, data.variants),
    [],
  );

  const {
    data: status,
    error: pollError,
    refetch,
  } = usePolledJobStatus<GenerativeJobStatus>(
    fetcher,
    undefined,
    isTerminalAndDone,
  );

  // ===== Upload / submit handlers =====

  const handleFiles = useCallback(
    async (files: FileList | readonly File[] | readonly PendingClip[] | null) => {
      if (!files || files.length === 0) return;
      const incoming = Array.from(files as ArrayLike<File | PendingClip>);
      if (uploads.length + incoming.length > MAX_GENERATIVE_CLIPS) {
        setSubmitError(`You can upload up to ${MAX_GENERATIVE_CLIPS} clips.`);
        return;
      }
      const selected = incoming.map((entry) =>
        entry instanceof File
          ? { file: entry, order: nextUploadOrder.current++ }
          : entry,
      );
      setUploading(true);
      setSubmitError(null);
      setFailedUploads([]);
      try {
        const settled = await Promise.allSettled(
          selected.map(async ({ file, order }) => {
            const r = await uploadGenerativeClip(file);
            const completed = { gcs_path: r.gcs_path, name: file.name, order };
            setUploads((prev) => [...prev, completed]);
            return completed;
          }),
        );
        const completed = settled.flatMap((result) =>
          result.status === "fulfilled" ? [result.value] : [],
        );
        const failedFiles = settled.flatMap((result, index) =>
          result.status === "rejected" ? [selected[index]] : [],
        );
        setFailedUploads(failedFiles);
        const failed = settled.find((result) => result.status === "rejected");
        if (failed?.status === "rejected") {
          setSubmitError(
            failed.reason instanceof Error
              ? `${completed.length} uploaded · ${failedFiles.length} didn’t upload · ${failed.reason.message}`
              : `${completed.length} uploaded · ${failedFiles.length} didn’t upload`,
          );
        }
      } finally {
        setUploading(false);
      }
    },
    [uploads.length],
  );

  const handleGenerate = useCallback(async () => {
    setSubmitError(null);
    try {
      const res = await createGenerativeJob(
        [...uploads].sort((a, b) => a.order - b.order).map((u) => u.gcs_path),
        voiceoverPath,
      );
      setJobId(res.job_id);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : "Failed to start");
    }
  }, [uploads, voiceoverPath]);

  const refresh = useCallback(() => {
    refetch();
  }, [refetch]);

  const handleRetry = useCallback(
    async (variantId: string) => {
      if (!jobId) return;
      try {
        await retextVariant(jobId, variantId, {});
      } catch {
        // Best-effort
      }
      refetch();
    },
    [jobId, refetch],
  );

  // ===== Theater props =====
  const theaterIsTerminal = status != null && isTerminalAndDone(status);
  const theaterIsSuccess = status != null && isSuccessStatus(status.status);
  const receiptText = status ? deriveReceiptText(status.started_at ?? status.created_at, status.finished_at ?? status.updated_at) : "Your edits are ready";

  const currentPhase: string | null = (() => {
    if (!status) return null;
    if (status.current_phase) return status.current_phase;
    if (!status.started_at) return "queued";
    return null;
  })();

  // ===== Render =====

  return (
    <LightShell size="wide">
      {/* @font-face for the instant-edit preview + style chips — the registry
          fonts must be loaded on this PUBLIC page (admin gets them via its
          layout) or the client overlay renders in a fallback face. */}
      <style dangerouslySetInnerHTML={{ __html: FONT_FACES }} />
      {/* Page header */}
      <div className="mb-10">
        <Eyebrow tone="lime" className="mb-3">Generative edit</Eyebrow>
        <h1 className="font-display text-3xl text-[#0c0c0e]">Make your edit</h1>
        <p className="mt-2 text-[#71717a]">
          Upload your clips. We pick a song, write the text, and give you a few versions to choose from.
        </p>
      </div>

      {/* Submit-phase errors */}
      {submitError && (
        <div
          role="status"
          aria-live="polite"
          className="mb-6 rounded border border-zinc-200 bg-[#fafaf8] px-4 py-3 text-[#3f3f46]"
        >
          {submitError}
        </div>
      )}

      {/* Transient poll errors */}
      {pollError && status && (
        <div className="mb-4 rounded border border-zinc-200 bg-[#fafaf8] px-4 py-2 text-sm text-[#71717a]">
          Trouble reaching the server — retrying…
        </div>
      )}

      {/* ===== Upload form ===== */}
      {!jobId && (
        <section className="space-y-5">
          <div>
            <label className="block text-sm text-[#71717a] mb-2">Clips</label>
            <input
              ref={clipInputRef}
              type="file"
              accept="video/*,image/*"
              multiple
              disabled={uploading}
              aria-label="Upload clips"
              className="sr-only"
              tabIndex={-1}
              onChange={(e) => {
                handleFiles(e.target.files);
                // Reset so re-selecting the same file fires change again and
                // Safari drops its native filename + thumbnail rendering.
                e.target.value = "";
              }}
            />
            <button
              type="button"
              disabled={uploading}
              onClick={() => clipInputRef.current?.click()}
              className="inline-flex min-h-11 items-center rounded-full bg-[#0c0c0e] px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-80 focus-visible:outline-2 focus-visible:outline-[#0c0c0e] disabled:opacity-40 sm:min-h-0"
            >
              Add clips
            </button>
            <p role="status" aria-live="polite" className="mt-2 text-sm text-[#71717a]">
              {uploading
                ? `${uploads.length} clip${uploads.length === 1 ? "" : "s"} ready · Uploading…`
                : uploads.length > 0
                  ? `${uploads.length} clip${uploads.length === 1 ? "" : "s"} ready`
                  : ""}
            </p>
            {uploads.length > 0 && (
              <ul className="mt-3 space-y-1 text-sm text-[#71717a]">
                {[...uploads].sort((a, b) => a.order - b.order).map((u) => (
                  <li key={u.order}>• {u.name}</li>
                ))}
              </ul>
            )}
            {failedUploads.length > 0 && (
              <div className="mt-3 text-sm text-[#71717a]">
                <p>
                  Didn&apos;t upload: {failedUploads.map(({ file }) => file.name).join(", ")}
                </p>
                <button
                  type="button"
                  onClick={() => void handleFiles(failedUploads)}
                  className="mt-2 min-h-11 rounded-full border border-zinc-300 px-4 py-2 text-sm text-[#3f3f46] hover:border-zinc-500"
                >
                  Retry failed clips
                </button>
              </div>
            )}
          </div>

          <div>
            <label className="block text-sm text-[#71717a] mb-2">Voiceover (optional)</label>
            <VoiceRecorder onVoiceover={setVoiceoverPath} />
          </div>

          <p className="text-xs text-[#a1a1aa]">
            Length is set automatically from your clips and the matched song —
            the edit is never longer than the footage you upload.
          </p>

          <InkButton
            onClick={handleGenerate}
            disabled={uploads.length === 0 || uploading}
          >
            Generate edits
          </InkButton>
          <p className="text-xs text-[#a1a1aa]">
            {voiceoverPath
              ? "We'll build voiceover edits around your recording — sync your footage to your voice."
              : "Add a voiceover above and you'll get voiceover edits instead."}
          </p>
        </section>
      )}

      {/* ===== Progress theater ===== */}
      {jobId && (
        <section>
          {/* Style-sets blip warning */}
          {styleSetsError && styleSets.length === 0 && (
            <div className="mb-4 flex items-center gap-3 rounded border border-zinc-200 bg-[#fafaf8] px-4 py-2 text-sm text-[#71717a]">
              <span>Couldn&apos;t load text styles — the style picker is hidden.</span>
              <button
                onClick={loadStyleSets}
                className="min-h-11 rounded border border-zinc-300 px-3 py-0.5 text-xs text-[#3f3f46] hover:bg-zinc-100 sm:min-h-0 sm:px-2"
              >
                Retry
              </button>
            </div>
          )}

          {/* Total failure */}
          {status?.status === "processing_failed" && (
            <div className="mb-6">
              <p className="text-[#3f3f46] mb-4">
                {status.error_detail ?? "Something went wrong — we couldn't process your clips."}
              </p>
              <button
                onClick={() => {
                  setJobId(null);
                  setUploads([]);
                  setSubmitError(null);
                }}
                className="rounded-full border border-zinc-200 px-4 py-2 text-sm text-[#3f3f46] hover:border-zinc-400"
              >
                Start over
              </button>
            </div>
          )}

          {/*
           * D5 ProgressTheater — tone="light" for cream canvas.
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
            retrying={status?.retrying ?? false}
            size="full"
            tone="light"
          >
            {/*
             * D7 PayoffField — shimmer until variants array is populated.
             */}
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
                    onRetry={() => void handleRetry(gv.variant_id)}
                    refresh={refresh}
                  />
                );
              }}
            />
          </ProgressTheater>
        </section>
      )}
    </LightShell>
  );
}
