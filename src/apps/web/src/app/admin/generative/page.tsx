"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  adminListGenerativeJobs,
  type AdminGenerativeListItem,
  type AdminGenerativeVariant,
} from "@/lib/admin-generative-api";
import {
  createGenerativeJob,
  uploadGenerativeClip,
} from "@/lib/generative-api";

const POLL_MS = 5000;

// A generative job stops moving once it reaches one of these. Anything else
// (queued / processing / rendering / …) is still live, so we keep polling.
const TERMINAL_STATUSES = new Set([
  "variants_ready",
  "variants_ready_partial",
  "variants_failed",
  "processing_failed",
  "cancelled",
]);

const STATUS_COLOR: Record<string, string> = {
  queued: "text-yellow-400",
  processing: "text-blue-400",
  matching: "text-blue-400",
  rendering: "text-blue-400",
  variants_ready: "text-green-400",
  variants_ready_partial: "text-amber-400",
  variants_failed: "text-red-400",
  processing_failed: "text-red-400",
  cancelled: "text-zinc-400",
};

const TEXT_MODE_LABEL: Record<string, string> = {
  lyrics: "Lyrics",
  agent_text: "AI text",
  none: "No text",
};

export default function AdminGenerativePage() {
  const [items, setItems] = useState<AdminGenerativeListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await adminListGenerativeJobs();
      setItems(data.items);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load generative jobs");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Keep polling while any listed job is still live.
  const anyLive = items.some((j) => !TERMINAL_STATUSES.has(j.status));
  useEffect(() => {
    if (!anyLive) return;
    const t = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(t);
  }, [anyLive, load]);

  return (
    <main className="min-h-screen bg-black text-white px-4 py-12">
      <div className="max-w-6xl mx-auto">
        <header className="mb-8 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">Generative edits</h1>
            <p className="text-zinc-400 text-sm mt-1">
              Launch a test edit, or click a row to inspect variants, agents, and
              the assembly trace.
            </p>
          </div>
          <Link
            href="/admin"
            className="px-4 py-2 bg-zinc-800 text-zinc-300 rounded-lg text-sm hover:bg-zinc-700"
          >
            ← Admin
          </Link>
        </header>

        <LaunchPanel onLaunched={load} />

        {error && (
          <div className="mt-8 rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-300">
            {error}
          </div>
        )}

        <section className="mt-8">
          <div className="text-xs uppercase tracking-wider text-zinc-500 mb-3">
            Recent generative jobs ({items.length})
          </div>
          {loading ? (
            <p className="text-zinc-400 text-sm">Loading…</p>
          ) : items.length === 0 ? (
            <p className="text-zinc-500 text-sm">
              No generative jobs yet — launch one above.
            </p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-zinc-800">
              <table className="w-full text-sm">
                <thead className="bg-zinc-900/60 text-zinc-400">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">Job</th>
                    <th className="px-3 py-2 text-left font-medium">Status</th>
                    <th className="px-3 py-2 text-left font-medium">Clips</th>
                    <th className="px-3 py-2 text-left font-medium">Variants</th>
                    <th className="px-3 py-2 text-left font-medium">Created</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800">
                  {items.map((job) => (
                    <tr key={job.job_id} className="hover:bg-zinc-900/40">
                      <td className="px-3 py-2 whitespace-nowrap">
                        <Link
                          href={`/admin/generative/${job.job_id}`}
                          className="font-mono text-xs text-blue-400 hover:underline"
                        >
                          {job.job_id.slice(0, 8)}…
                        </Link>
                        <Link
                          href={`/admin/jobs/${job.job_id}`}
                          className="ml-2 text-[10px] text-zinc-500 hover:underline"
                        >
                          debug
                        </Link>
                      </td>
                      <td className="px-3 py-2">
                        <span className={STATUS_COLOR[job.status] ?? "text-zinc-300"}>
                          {job.status}
                        </span>
                        {job.error_detail && (
                          <span
                            className="ml-2 text-xs text-red-400/80"
                            title={job.error_detail}
                          >
                            ⚠
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-zinc-400">{job.clip_count}</td>
                      <td className="px-3 py-2">
                        <div className="flex flex-wrap gap-1">
                          {job.variants.length === 0 ? (
                            <span className="text-zinc-600 text-xs">—</span>
                          ) : (
                            job.variants.map((v) => (
                              <VariantChip key={v.variant_id} variant={v} />
                            ))
                          )}
                        </div>
                      </td>
                      <td className="px-3 py-2 text-zinc-500 text-xs whitespace-nowrap">
                        {new Date(job.created_at).toLocaleString()}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}

function VariantChip({ variant }: { variant: AdminGenerativeVariant }) {
  // Color the chip by render outcome so readiness is scannable at a glance.
  const color =
    variant.render_status === "ready"
      ? "border-green-700 text-green-300"
      : variant.render_status === "failed"
        ? "border-red-700 text-red-300"
        : variant.render_status === "rendering"
          ? "border-blue-700 text-blue-300"
          : "border-zinc-700 text-zinc-400";
  const label = TEXT_MODE_LABEL[variant.text_mode ?? ""] ?? variant.text_mode ?? variant.variant_id;
  const title = variant.error ?? `${variant.variant_id} · ${variant.render_status ?? "—"}`;
  return (
    <span
      className={`rounded border px-1.5 py-0.5 text-[10px] ${color}`}
      title={title}
    >
      {label}
      {variant.track_title ? ` · ${variant.track_title}` : ""}
    </span>
  );
}

function LaunchPanel({ onLaunched }: { onLaunched: () => Promise<void> }) {
  const [uploads, setUploads] = useState<{ gcs_path: string; name: string }[]>([]);
  const [uploading, setUploading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastJobId, setLastJobId] = useState<string | null>(null);

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

  const handleLaunch = useCallback(async () => {
    setSubmitting(true);
    setError(null);
    try {
      const res = await createGenerativeJob(uploads.map((u) => u.gcs_path));
      setLastJobId(res.job_id);
      setUploads([]);
      await onLaunched();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to launch job");
    } finally {
      setSubmitting(false);
    }
  }, [uploads, onLaunched]);

  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-950 p-5">
      <div className="text-xs uppercase tracking-wider text-zinc-500 mb-3">
        Launch a test edit
      </div>
      {error && (
        <div className="mb-4 rounded border border-red-800 bg-red-950/40 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}
      <div className="flex flex-wrap items-end gap-6">
        <div>
          <label className="block text-sm text-zinc-400 mb-2">Clips</label>
          <input
            type="file"
            accept="video/*,image/*"
            multiple
            disabled={uploading || submitting}
            onChange={(e) => handleFiles(e.target.files)}
            className="block text-sm text-zinc-300 file:mr-4 file:rounded file:border-0 file:bg-zinc-800 file:px-4 file:py-2 file:text-white"
          />
          {uploading && <p className="mt-2 text-sm text-zinc-500">Uploading…</p>}
          {uploads.length > 0 && (
            <p className="mt-2 text-xs text-zinc-400">
              {uploads.length} clip{uploads.length !== 1 ? "s" : ""}:{" "}
              {uploads.map((u) => u.name).join(", ")}
            </p>
          )}
        </div>

        <p className="text-xs text-zinc-500">
          Length is derived from the clips and the matched song — the edit is
          never longer than the uploaded footage.
        </p>

        <button
          type="button"
          onClick={handleLaunch}
          disabled={uploads.length === 0 || uploading || submitting}
          className="rounded bg-white px-5 py-2 font-medium text-black disabled:opacity-40"
        >
          {submitting ? "Launching…" : "Run generative job"}
        </button>
      </div>
      {lastJobId && (
        <p className="mt-4 text-sm text-zinc-400">
          Launched{" "}
          <Link
            href={`/admin/jobs/${lastJobId}`}
            className="font-mono text-blue-400 hover:underline"
          >
            {lastJobId.slice(0, 8)}…
          </Link>{" "}
          — it will appear below as it renders.
        </p>
      )}
    </section>
  );
}
