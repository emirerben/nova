"use client";

import { useCallback, useEffect, useState } from "react";
import {
  createGenerativeJob,
  getGenerativeJobStatus,
  getGenerativeStyleSets,
  GENERATIVE_TERMINAL_STATUSES,
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
import { formatElapsed } from "@/components/progress/logic";
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";
import { LightShell } from "@/components/ui/LightShell";
import { Eyebrow } from "@/components/ui/Eyebrow";
import { InkButton } from "@/components/ui/InkButton";

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
    undefined,
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
  const receiptText = status ? deriveReceiptText(status) : "Your edits are ready";

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
        <div className="mb-6 rounded border border-zinc-200 bg-[#fafaf8] px-4 py-3 text-[#3f3f46]">
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
              type="file"
              accept="video/*,image/*"
              multiple
              disabled={uploading}
              onChange={(e) => handleFiles(e.target.files)}
              className="block w-full text-sm text-[#71717a] file:mr-4 file:rounded-full file:border-0 file:bg-[#0c0c0e] file:px-4 file:py-2 file:text-sm file:font-medium file:text-white hover:file:opacity-80"
            />
            {uploading && <p className="mt-2 text-sm text-[#71717a]">Uploading…</p>}
            {uploads.length > 0 && (
              <ul className="mt-3 space-y-1 text-sm text-[#71717a]">
                {uploads.map((u, i) => (
                  <li key={i}>• {u.name}</li>
                ))}
              </ul>
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
                className="rounded border border-zinc-300 px-2 py-0.5 text-xs text-[#3f3f46] hover:bg-zinc-100"
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
