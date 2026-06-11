"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  changeVariantStyle,
  editVariant,
  getGenerativeJobStatus,
  getGenerativeStyleSets,
  GENERATIVE_TERMINAL_STATUSES,
  retextVariant,
  swapVariantSong,
  type GenerativeJobStatus,
  type GenerativeStyleSet,
} from "@/lib/generative-api";
import { getMusicTracks, type MusicTrackSummary } from "@/lib/music-api";
import { VariantCard } from "@/app/generative/VariantCard";

const POLL_MS = 2500;

/**
 * Admin generative-job detail. Shows each variant's preview + the same per-variant
 * controls the public page has (edit text, swap song, change style) so an admin can
 * iterate on a job. Drives the public generative endpoints directly — they require
 * no admin token (same as swap-song), so no admin proxy is involved.
 */
export default function AdminGenerativeDetailPage({
  params,
}: {
  params: { id: string };
}) {
  const jobId = params.id;
  const [status, setStatus] = useState<GenerativeJobStatus | null>(null);
  const [tracks, setTracks] = useState<MusicTrackSummary[]>([]);
  const [styleSets, setStyleSets] = useState<GenerativeStyleSet[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    getMusicTracks()
      .then((r) => setTracks(r.tracks))
      .catch(() => setTracks([]));
    getGenerativeStyleSets()
      .then(setStyleSets)
      .catch(() => setStyleSets([]));
  }, []);

  const isTerminal = status != null && GENERATIVE_TERMINAL_STATUSES.includes(status.status);
  const anyRendering = status?.variants?.some((v) => v.render_status === "rendering") ?? false;

  useEffect(() => {
    if (isTerminal && !anyRendering && status != null) return;
    let cancelled = false;
    pollRef.current = setTimeout(async () => {
      try {
        const s = await getGenerativeJobStatus(jobId);
        if (!cancelled) setStatus(s);
      } catch (e) {
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

  const refresh = useCallback(async () => {
    setStatus(await getGenerativeJobStatus(jobId));
  }, [jobId]);

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

  return (
    <main className="min-h-screen bg-black text-white px-4 py-12">
      <div className="max-w-6xl mx-auto">
        <header className="mb-8 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">Generative job</h1>
            <p className="mt-1 font-mono text-xs text-zinc-500">{jobId}</p>
            <p className="mt-1 text-sm text-zinc-400">
              Status: <span className="text-zinc-200">{status?.status ?? "loading…"}</span>
            </p>
          </div>
          <div className="flex gap-2">
            <Link
              href={`/admin/jobs/${jobId}`}
              className="rounded-lg bg-zinc-800 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-700"
            >
              Debug ↗
            </Link>
            <Link
              href="/admin/generative"
              className="rounded-lg bg-zinc-800 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-700"
            >
              ← Generative
            </Link>
          </div>
        </header>

        {error && (
          <div className="mb-6 rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-300">
            {error}
          </div>
        )}

        <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-3">
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
              onChangeLayout={async (layout) => {
                markVariantRendering(v.variant_id);
                await editVariant(jobId, v.variant_id, { intro_layout: layout });
                await refresh();
              }}
            />
          ))}
        </div>

        {status != null && status.variants.length === 0 && (
          <p className="text-sm text-zinc-500">No variants yet — the job is still rendering.</p>
        )}
      </div>
    </main>
  );
}
