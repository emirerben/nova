"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  changeVariantStyle,
  createGenerativeJob,
  getGenerativeJobStatus,
  getGenerativeStyleSets,
  GENERATIVE_TERMINAL_STATUSES,
  retextVariant,
  swapVariantSong,
  uploadGenerativeClip,
  type GenerativeJobStatus,
  type GenerativeStyleSet,
} from "@/lib/generative-api";
import { getMusicTracks, type MusicTrackSummary } from "@/lib/music-api";
import { VariantCard } from "./VariantCard";

const POLL_MS = 2000;

export default function GenerativePage() {
  const [uploads, setUploads] = useState<{ gcs_path: string; name: string }[]>([]);
  const [uploading, setUploading] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [status, setStatus] = useState<GenerativeJobStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tracks, setTracks] = useState<MusicTrackSummary[]>([]);
  const [styleSets, setStyleSets] = useState<GenerativeStyleSet[]>([]);
  // True when the style-sets fetch failed. Without this, a transient API blip
  // leaves styleSets=[] and the change-style picker (gated on length>0 in
  // VariantCard) silently disappears — indistinguishable from "feature missing".
  const [styleSetsError, setStyleSetsError] = useState(false);
  // Bumped after every poll (success OR failure) so the polling effect always
  // re-arms — a transient fetch error must not silently kill polling.
  const [tick, setTick] = useState(0);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Style sets for the style picker — retryable so a blip doesn't permanently
  // hide the control.
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

  // Song library for the swap picker + style sets for the style picker.
  useEffect(() => {
    getMusicTracks()
      .then((r) => setTracks(r.tracks))
      .catch(() => setTracks([]));
    loadStyleSets();
  }, [loadStyleSets]);

  const isTerminal = status != null && GENERATIVE_TERMINAL_STATUSES.includes(status.status);

  // Poll job status until terminal. Swap/retext also re-arm the poll (they flip a
  // variant back to "rendering" and the job status stays terminal, so we poll while
  // any variant is still rendering too).
  const anyRendering =
    status?.variants?.some((v) => v.render_status === "rendering") ?? false;

  useEffect(() => {
    if (!jobId) return;
    if (isTerminal && !anyRendering) return;
    let cancelled = false;
    pollRef.current = setTimeout(async () => {
      try {
        const s = await getGenerativeJobStatus(jobId);
        if (!cancelled) setStatus(s);
      } catch (e) {
        // Re-arm on transient error (bump tick) instead of dying silently.
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Failed to poll status");
          setTick((x) => x + 1);
        }
      }
    }, POLL_MS);
    return () => {
      cancelled = true;
      if (pollRef.current) clearTimeout(pollRef.current);
    };
  }, [jobId, status, tick, isTerminal, anyRendering]);

  // Optimistically flip a variant to "rendering" in local state so the poll arms
  // immediately after swap/retext — the worker only sets the real "rendering" flag
  // once it dequeues the task, which is after the POST returns and refresh() runs.
  const markVariantRendering = useCallback((variantId: string) => {
    setStatus((s) =>
      s
        ? {
            ...s,
            variants: s.variants.map((v) =>
              v.variant_id === variantId
                ? { ...v, render_status: "rendering" as const, ok: false, error: null }
                : v,
            ),
          }
        : s,
    );
  }, []);

  const handleFiles = useCallback(async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError(null);
    try {
      const results = await Promise.all(
        Array.from(files).map(async (f) => {
          const r = await uploadGenerativeClip(f);
          return { gcs_path: r.gcs_path, name: f.name };
        }),
      );
      setUploads((prev) => [...prev, ...results]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }, []);

  const handleGenerate = useCallback(async () => {
    setError(null);
    try {
      const res = await createGenerativeJob(uploads.map((u) => u.gcs_path));
      setJobId(res.job_id);
      setStatus(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start");
    }
  }, [uploads]);

  const refresh = useCallback(async () => {
    if (jobId) setStatus(await getGenerativeJobStatus(jobId));
  }, [jobId]);

  return (
    <main className="min-h-[calc(100vh-3.5rem)] bg-black text-white">
      <div className="max-w-5xl mx-auto px-4 py-12">
        <h1 className="text-2xl font-semibold mb-2">Generative edit</h1>
        <p className="text-zinc-400 mb-8">
          Upload your clips. We pick a song, write the text, and give you a few versions to choose from.
        </p>

        {error && (
          <div className="mb-6 rounded border border-red-700 bg-red-950/50 px-4 py-3 text-red-200">
            {error}
          </div>
        )}

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
          </section>
        )}

        {jobId && (
          <section>
            <StatusBanner status={status} />
            {status?.status === "processing_failed" && (
              <button
                onClick={() => {
                  setJobId(null);
                  setStatus(null);
                  setUploads([]);
                  setError(null);
                }}
                className="mt-4 rounded border border-zinc-700 px-4 py-2 text-sm text-zinc-300"
              >
                Start over
              </button>
            )}
            {styleSetsError && styleSets.length === 0 && (
              <div className="mt-4 flex items-center gap-3 rounded border border-amber-700/60 bg-amber-950/40 px-4 py-2 text-sm text-amber-200">
                <span>Couldn&apos;t load text styles — the style picker is hidden.</span>
                <button
                  onClick={loadStyleSets}
                  className="rounded border border-amber-600 px-2 py-0.5 text-xs text-amber-100 hover:bg-amber-900/50"
                >
                  Retry
                </button>
              </div>
            )}
            <div className="mt-6 grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-3">
              {(status?.variants ?? []).map((v) => (
                <VariantCard
                  key={v.variant_id}
                  variant={v}
                  tracks={tracks}
                  styleSets={styleSets}
                  onSwap={async (trackId) => {
                    markVariantRendering(v.variant_id);
                    await swapVariantSong(jobId, v.variant_id, trackId);
                    await refresh();
                  }}
                  onRetext={async (text) => {
                    markVariantRendering(v.variant_id);
                    await retextVariant(jobId, v.variant_id, { text });
                    await refresh();
                  }}
                  onRemoveText={async () => {
                    markVariantRendering(v.variant_id);
                    await retextVariant(jobId, v.variant_id, { remove: true });
                    await refresh();
                  }}
                  onChangeStyle={async (styleSetId) => {
                    markVariantRendering(v.variant_id);
                    await changeVariantStyle(jobId, v.variant_id, styleSetId);
                    await refresh();
                  }}
                />
              ))}
            </div>
          </section>
        )}
      </div>
    </main>
  );
}

function StatusBanner({ status }: { status: GenerativeJobStatus | null }) {
  const s = status?.status ?? "queued";
  const label: Record<string, string> = {
    queued: "Queued…",
    processing: "Analyzing your clips…",
    matching: "Matching a song…",
    rendering: "Rendering your edits…",
    variants_ready: "Your edits are ready — pick one.",
    variants_ready_partial: "Some edits are ready (others failed).",
    variants_failed: "We couldn't render any edits.",
    processing_failed: status?.error_detail ?? "Something went wrong.",
  };
  return <p className="text-zinc-300">{label[s] ?? s}</p>;
}
