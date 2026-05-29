"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  attachClips,
  generatePlanItem,
  getPlanItem,
  getPlanItemVariants,
  NotAuthenticatedError,
  type PlanItem,
  type PlanItemVariant,
  requestUploadUrls,
  uploadToGcs,
} from "@/lib/plan-api";

const POLL_MS = 2500;

export default function PlanItemPage() {
  const params = useParams<{ id: string }>();
  const itemId = params.id;

  const [item, setItem] = useState<PlanItem | null>(null);
  const [variants, setVariants] = useState<PlanItemVariant[]>([]);
  const [loading, setLoading] = useState(true);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const it = await getPlanItem(itemId);
      setItem(it);
      if (it.status === "ready" && it.current_job_id) {
        try {
          setVariants(await getPlanItemVariants(it.current_job_id));
        } catch {
          // best-effort; the status itself is the source of truth
        }
      }
      return it;
    } catch (err) {
      if (err instanceof NotAuthenticatedError) setNeedsAuth(true);
      else setError(err instanceof Error ? err.message : "Failed to load item");
      return null;
    } finally {
      setLoading(false);
    }
  }, [itemId]);

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      const it = await refresh();
      if (cancelled) return;
      if (it && (it.status === "generating" || it.current_job_id) && it.status !== "ready") {
        pollRef.current = setTimeout(tick, POLL_MS);
      }
    }
    void tick();
    return () => {
      cancelled = true;
      if (pollRef.current) clearTimeout(pollRef.current);
    };
  }, [refresh]);

  async function handleFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError(null);
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
      const existing = item?.clip_gcs_paths ?? [];
      const updated = await attachClips(itemId, [...existing, ...urls.map((u) => u.gcs_path)]);
      setItem(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function handleGenerate() {
    setGenerating(true);
    setError(null);
    try {
      await generatePlanItem(itemId);
      await refresh();
      pollRef.current = setTimeout(async function tick() {
        const it = await refresh();
        if (it && it.status !== "ready" && it.status !== "failed") {
          pollRef.current = setTimeout(tick, POLL_MS);
        }
      }, POLL_MS);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start generation");
    } finally {
      setGenerating(false);
    }
  }

  if (needsAuth) {
    return (
      <Shell>
        <div className="py-20 text-center">
          <h1 className="mb-3 text-2xl font-semibold">Sign in to continue</h1>
          <a
            href="/api/auth/signin"
            className="inline-block rounded bg-white px-6 py-3 font-medium text-black hover:bg-zinc-200"
          >
            Sign in with Google
          </a>
        </div>
      </Shell>
    );
  }

  if (loading) {
    return (
      <Shell>
        <p className="py-20 text-center text-zinc-400">Loading…</p>
      </Shell>
    );
  }

  if (item === null) {
    return (
      <Shell>
        <p className="py-20 text-center text-zinc-400">Item not found.</p>
      </Shell>
    );
  }

  const clipCount = item.clip_gcs_paths.length;
  const ready = variants.filter((v) => v.output_url);

  return (
    <Shell>
      <div className="py-12">
        <Link href="/plan" className="text-sm text-zinc-500 underline hover:text-white">
          ← back to plan
        </Link>
        <div className="mt-3 mb-1 flex items-center gap-3">
          <span className="rounded bg-zinc-800 px-2 py-0.5 text-xs text-zinc-400">
            Day {item.day_index}
          </span>
          <h1 className="text-2xl font-semibold">{item.theme}</h1>
        </div>
        <p className="mb-2 text-zinc-300">{item.idea}</p>
        {item.filming_suggestion && (
          <p className="mb-8 text-sm text-zinc-500">🎬 {item.filming_suggestion}</p>
        )}

        {error && (
          <div className="mb-6 rounded border border-red-700 bg-red-950/50 px-4 py-3 text-red-200">
            {error}
          </div>
        )}

        {/* Upload */}
        <section className="mb-8 rounded-lg border border-zinc-800 bg-zinc-900/60 p-5">
          <h2 className="mb-2 text-sm font-semibold text-zinc-300">Themed clips</h2>
          <p className="mb-4 text-sm text-zinc-500">
            Upload footage for this idea. {clipCount > 0 ? `${clipCount} uploaded.` : "None yet."}
          </p>
          <input
            type="file"
            accept="video/mp4,video/quicktime"
            multiple
            disabled={uploading}
            onChange={(e) => handleFiles(e.target.files)}
            className="block w-full text-sm text-zinc-400 file:mr-3 file:rounded file:border-0 file:bg-white file:px-4 file:py-2 file:text-sm file:font-medium file:text-black hover:file:bg-zinc-200"
          />
          {uploading && <p className="mt-3 text-sm text-amber-300">Uploading…</p>}
        </section>

        {/* Generate */}
        <button
          onClick={handleGenerate}
          disabled={generating || clipCount === 0 || item.status === "generating"}
          className="rounded bg-white px-6 py-3 font-medium text-black hover:bg-zinc-200 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-400"
        >
          {item.status === "generating"
            ? "Generating…"
            : generating
              ? "Starting…"
              : "Generate videos"}
        </button>
        {clipCount === 0 && (
          <p className="mt-2 text-sm text-zinc-500">Upload at least one clip first.</p>
        )}

        {/* Status / results */}
        <div className="mt-8">
          <StatusLine status={item.status} />
          {item.status === "ready" && ready.length > 0 && (
            <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-3">
              {ready.map((v) => (
                <video
                  key={v.variant_id}
                  src={v.output_url ?? undefined}
                  controls
                  className="w-full rounded border border-zinc-800"
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </Shell>
  );
}

function StatusLine({ status }: { status: string }) {
  const copy: Record<string, string> = {
    idea: "Not started — upload clips and generate.",
    awaiting_clips: "Waiting on your clips.",
    generating: "Rendering your variants… this can take a couple of minutes.",
    ready: "Done — your videos are below.",
    failed: "Generation failed. Try generating again.",
  };
  return <p className="text-sm text-zinc-400">{copy[status] ?? status}</p>;
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="min-h-[calc(100vh-3.5rem)] bg-black text-white">
      <div className="mx-auto max-w-2xl px-4">{children}</div>
    </main>
  );
}
