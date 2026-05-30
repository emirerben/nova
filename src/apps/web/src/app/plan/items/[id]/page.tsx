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
import PlanShell from "../../_components/PlanShell";
import SignInPrompt from "../../_components/SignInPrompt";

const POLL_MS = 2500;
// Generative renders three variants; show that many tiles while waiting so the
// grid doesn't reflow as each one lands.
const EXPECTED_VARIANTS = 3;

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
      // Hydrate variants whenever a render job exists — NOT only on "ready" — so
      // each variant tile fills in (or fails) on its own as the render lands.
      if (it.current_job_id) {
        try {
          setVariants(await getPlanItemVariants(it.current_job_id));
        } catch {
          // best-effort; the item status itself is the source of truth
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

  // Keep polling while a render is in flight (generating, or a job exists that
  // hasn't reached a terminal item status). Stops on ready/failed and unmount.
  useEffect(() => {
    let cancelled = false;
    async function tick() {
      const it = await refresh();
      if (cancelled || !it) return;
      const inFlight =
        it.status === "generating" ||
        (!!it.current_job_id && it.status !== "ready" && it.status !== "failed");
      if (inFlight) pollRef.current = setTimeout(tick, POLL_MS);
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
      <PlanShell>
        <SignInPrompt
          callbackUrl={`/plan/items/${itemId}`}
          title="Sign in to continue"
          subtitle="We use your Google account to save your clips and renders."
        />
      </PlanShell>
    );
  }

  if (loading) {
    return (
      <PlanShell>
        <p className="py-24 text-center text-zinc-400">Loading…</p>
      </PlanShell>
    );
  }

  if (item === null) {
    return (
      <PlanShell>
        <div className="animate-fade-up py-24 text-center">
          <p className="mb-6 text-zinc-400">We couldn&apos;t find that idea.</p>
          <Link
            href="/plan"
            className="inline-block rounded-full bg-white px-6 py-3 text-sm font-medium text-black transition-colors hover:bg-zinc-200"
          >
            Back to your plan
          </Link>
        </div>
      </PlanShell>
    );
  }

  const clipCount = item.clip_gcs_paths.length;
  const isGenerating = item.status === "generating";
  const readyCount = variants.filter((v) => v.output_url).length;
  const showResults = isGenerating || variants.length > 0;
  // Pad with skeletons while generating so the grid shows the shape of what's coming.
  const tileCount = isGenerating ? Math.max(EXPECTED_VARIANTS, variants.length) : variants.length;

  return (
    <PlanShell>
      <div className="animate-fade-up py-12">
        <Link
          href="/plan"
          className="text-sm text-zinc-500 underline transition-colors hover:text-white"
        >
          ← back to plan
        </Link>
        <div className="mb-1 mt-4 flex items-center gap-3">
          <span className="rounded bg-zinc-800 px-2 py-0.5 text-xs text-zinc-400">
            Day {item.day_index}
          </span>
        </div>
        <h1 className="font-display text-3xl text-white">{item.theme}</h1>
        <p className="mb-2 mt-2 text-zinc-300">{item.idea}</p>
        {item.filming_suggestion && (
          <p className="mb-8 text-sm text-zinc-500">🎬 {item.filming_suggestion}</p>
        )}

        {error && (
          <div className="mb-6 rounded border border-red-700 bg-red-950/50 px-4 py-3 text-red-200">
            {error}
          </div>
        )}

        {/* Upload */}
        <section className="mb-8 rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
          <h2 className="mb-2 text-sm font-semibold text-zinc-300">Themed clips</h2>
          <p className="mb-4 text-sm text-zinc-500">
            Upload footage for this idea. {clipCount > 0 ? `${clipCount} uploaded.` : "None yet."}
          </p>
          <label className="block">
            <span className="sr-only">Upload video clips for this idea</span>
            <input
              type="file"
              accept="video/mp4,video/quicktime"
              multiple
              disabled={uploading}
              onChange={(e) => handleFiles(e.target.files)}
              className="block w-full text-sm text-zinc-400 file:mr-3 file:rounded-full file:border-0 file:bg-white file:px-4 file:py-2 file:text-sm file:font-medium file:text-black hover:file:bg-zinc-200"
            />
          </label>
          {uploading && <p className="mt-3 text-sm text-amber-300">Uploading…</p>}
        </section>

        {/* Generate */}
        <button
          onClick={handleGenerate}
          disabled={generating || clipCount === 0 || isGenerating}
          className="rounded-full bg-amber-400 px-6 py-3 font-medium text-black transition-colors hover:bg-amber-300 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-400"
        >
          {isGenerating ? "Generating…" : generating ? "Starting…" : "Generate videos"}
        </button>
        {clipCount === 0 && (
          <p className="mt-2 text-sm text-zinc-500">Upload at least one clip first.</p>
        )}

        {/* Status / results */}
        <div className="mt-8">
          {/* Ready-state moment: a render landed — mark the payoff, don't bury it. */}
          {item.status === "ready" && readyCount > 0 && (
            <div
              className="mb-4 flex items-center gap-2 rounded-lg border border-emerald-800/60 bg-emerald-950/20 px-4 py-3"
              role="status"
            >
              <span aria-hidden="true" className="text-lg">
                🎉
              </span>
              <p className="text-sm text-emerald-200">
                {readyCount === 1
                  ? "Your video is ready — play it below."
                  : `${readyCount} videos are ready — play them below.`}
              </p>
            </div>
          )}
          <p className="text-sm text-zinc-400" aria-live="polite">
            <StatusLine status={item.status} />
            {isGenerating && variants.length > 0 && (
              <span className="ml-1 text-zinc-500">
                ({readyCount} of {tileCount} ready)
              </span>
            )}
          </p>
          {isGenerating && (
            <p className="mt-1 text-xs text-zinc-500">
              Usually 2–3 minutes. You can leave this page — we&apos;ll keep rendering.
            </p>
          )}
          {showResults && (
            <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-3">
              {Array.from({ length: tileCount }).map((_, i) => {
                const v = variants[i];
                return v ? (
                  <VariantTile key={v.variant_id} variant={v} />
                ) : (
                  <SkeletonTile key={`skeleton-${i}`} />
                );
              })}
            </div>
          )}
          {item.status === "failed" && variants.length === 0 && (
            <p className="mt-2 text-sm text-zinc-500">
              Generation failed before any variant rendered. Try generating again.
            </p>
          )}
        </div>
      </div>
    </PlanShell>
  );
}

/** One render variant: plays when its URL lands, shows a failed state, else shimmers. */
function VariantTile({ variant }: { variant: PlanItemVariant }) {
  if (variant.output_url) {
    return (
      <video
        src={variant.output_url}
        controls
        className="w-full rounded-lg border border-zinc-800"
      />
    );
  }
  if (variant.render_status === "failed") {
    return (
      <div className="flex aspect-[9/16] w-full flex-col items-center justify-center rounded-lg border border-red-800/60 bg-red-950/20 p-4 text-center">
        <p className="text-sm text-red-300">This variant failed</p>
        <p className="mt-1 text-xs text-red-300/60">Re-generate to try again</p>
      </div>
    );
  }
  return <SkeletonTile />;
}

function SkeletonTile() {
  return (
    <div className="aspect-[9/16] w-full animate-shimmer rounded-lg border border-zinc-800 bg-[length:200%_100%] bg-gradient-to-r from-zinc-900 via-zinc-800 to-zinc-900" />
  );
}

function StatusLine({ status }: { status: string }) {
  const copy: Record<string, string> = {
    idea: "Not started — upload clips and generate.",
    awaiting_clips: "Waiting on your clips.",
    generating: "Rendering your variants…",
    ready: "Done — your videos are below.",
    failed: "Generation failed. Try generating again.",
  };
  return <>{copy[status] ?? status}</>;
}
