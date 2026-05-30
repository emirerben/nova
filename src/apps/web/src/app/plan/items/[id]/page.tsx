"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  attachClips,
  changePlanItemStyle,
  generatePlanItem,
  getPlanItem,
  getPlanItemVariants,
  NotAuthenticatedError,
  type PlanItem,
  type PlanItemVariant,
  requestUploadUrls,
  retextPlanItem,
  swapPlanItemSong,
  uploadToGcs,
} from "@/lib/plan-api";
import { getGenerativeStyleSets, type GenerativeStyleSet } from "@/lib/generative-api";
import { getMusicTracks, type MusicTrackSummary } from "@/lib/music-api";
import { FONT_FACES } from "@/lib/font-faces";
import PlanShell from "../../_components/PlanShell";
import PlanFilmstrip from "../../_components/PlanFilmstrip";
import PlanVariantEditor from "../../_components/PlanVariantEditor";
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
  // Song gallery + curated style sets for the per-variant edit controls. Both are
  // public GET endpoints (same as the generative page), so fetch them directly —
  // not through the authenticated /api/plan proxy.
  const [tracks, setTracks] = useState<MusicTrackSummary[]>([]);
  const [styleSets, setStyleSets] = useState<GenerativeStyleSet[]>([]);
  // Which variant the hero shows + the editor edits. Kept valid by an effect
  // below: defaults to the first ready variant, never points at a gone id.
  const [focusedVariantId, setFocusedVariantId] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // variant_id → the output_url at the moment the user submitted an edit. While a
  // variant is in here we keep polling and keep showing its spinner — we can't
  // rely on catching the worker's transient "rendering" flag (the throttled
  // plan-jobs queue may flip it between polls), so we wait for the URL to change.
  const pendingEdits = useRef<Map<string, string | null>>(new Map());

  useEffect(() => {
    getMusicTracks()
      .then((r) => setTracks(r.tracks))
      .catch(() => setTracks([]));
    getGenerativeStyleSets()
      .then(setStyleSets)
      .catch(() => setStyleSets([]));
  }, []);

  const refresh = useCallback(async () => {
    try {
      const it = await getPlanItem(itemId);
      setItem(it);
      // Hydrate variants whenever a render job exists — NOT only on "ready" — so
      // each variant tile fills in (or fails) on its own as the render lands.
      let vs: PlanItemVariant[] = [];
      if (it.current_job_id) {
        try {
          vs = await getPlanItemVariants(it.current_job_id);
          // Retire a pending edit once its render visibly lands (URL changed) or
          // fails; until then keep the variant displayed as "rendering".
          const pending = pendingEdits.current;
          if (pending.size > 0) {
            for (const sv of vs) {
              if (!pending.has(sv.variant_id)) continue;
              const prevUrl = pending.get(sv.variant_id) ?? null;
              const landed = !!sv.output_url && sv.output_url !== prevUrl;
              if (landed || sv.render_status === "failed") pending.delete(sv.variant_id);
            }
            vs = vs.map((sv) =>
              pending.has(sv.variant_id) ? { ...sv, render_status: "rendering" } : sv,
            );
          }
          setVariants(vs);
        } catch {
          // best-effort; the item status itself is the source of truth
        }
      }
      return { item: it, variants: vs };
    } catch (err) {
      if (err instanceof NotAuthenticatedError) setNeedsAuth(true);
      else setError(err instanceof Error ? err.message : "Failed to load item");
      return null;
    } finally {
      setLoading(false);
    }
  }, [itemId]);

  // A render is in flight while the item is generating, OR a job exists that
  // hasn't reached a terminal status, OR any single variant is re-rendering (a
  // swap/retext/restyle flips one variant back to "rendering" while the item
  // stays "ready").
  const inFlight = (it: PlanItem, vs: PlanItemVariant[]): boolean =>
    it.status === "generating" ||
    (!!it.current_job_id && it.status !== "ready" && it.status !== "failed") ||
    pendingEdits.current.size > 0 ||
    vs.some((v) => v.render_status === "rendering");

  // Arm a single recursive poll loop (deduped via pollRef) that runs until
  // nothing is in flight. Shared by mount, generate, and the edit actions.
  const armPoll = useCallback(() => {
    if (pollRef.current) clearTimeout(pollRef.current);
    pollRef.current = setTimeout(async function tick() {
      const res = await refresh();
      if (!res) return;
      if (inFlight(res.item, res.variants)) {
        pollRef.current = setTimeout(tick, POLL_MS);
      }
    }, POLL_MS);
  }, [refresh]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const res = await refresh();
      if (cancelled || !res) return;
      if (inFlight(res.item, res.variants)) armPoll();
    })();
    return () => {
      cancelled = true;
      if (pollRef.current) clearTimeout(pollRef.current);
    };
  }, [refresh, armPoll]);

  // Keep a valid focused variant as renders land: default to the first variant
  // with a playable output (else the first one), and reset if the focused id
  // disappears (e.g. a fresh generate replaces the variant set).
  useEffect(() => {
    if (variants.length === 0) {
      if (focusedVariantId !== null) setFocusedVariantId(null);
      return;
    }
    if (!variants.some((v) => v.variant_id === focusedVariantId)) {
      const firstReady = variants.find((v) => v.output_url) ?? variants[0];
      setFocusedVariantId(firstReady.variant_id);
    }
  }, [variants, focusedVariantId]);

  // Optimistically flip a variant to "rendering" so the card shows a spinner and
  // the poll arms immediately — the worker only sets the real flag once it
  // dequeues the task, after the POST returns.
  const markVariantRendering = useCallback((variantId: string) => {
    setVariants((vs) =>
      vs.map((v) => (v.variant_id === variantId ? { ...v, render_status: "rendering" } : v)),
    );
  }, []);

  const runEdit = useCallback(
    async (variantId: string, prevUrl: string | null, action: () => Promise<unknown>) => {
      markVariantRendering(variantId);
      setError(null);
      try {
        await action();
        // Track until the re-rendered URL lands (see pendingEdits) and keep polling.
        pendingEdits.current.set(variantId, prevUrl);
        armPoll();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to update variant");
        await refresh(); // clear the optimistic spinner if the request was rejected
      }
    },
    [armPoll, markVariantRendering, refresh],
  );

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
      armPoll();
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
  const focused = variants.find((v) => v.variant_id === focusedVariantId) ?? null;
  // The focused variant is editable once it has rendered (or failed) at least once.
  const focusedEditable =
    focused && (!!focused.output_url || focused.render_status === "failed");

  return (
    <PlanShell size="results">
      {/* @font-face for the style-preview chips — fonts lazy-load only when used. */}
      <style dangerouslySetInnerHTML={{ __html: FONT_FACES }} />
      <div className="animate-fade-up py-12">
        {/* ── Editorial header + controls (narrow, readable column) ── */}
        <div className="max-w-2xl">
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
          {item.rationale && (
            <div className="mb-4 mt-3 rounded-lg border border-zinc-800 bg-zinc-950/40 p-4">
              <p className="mb-1 text-xs font-medium text-amber-300/80">Why this works</p>
              <p className="text-sm text-zinc-300">{item.rationale}</p>
            </div>
          )}
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

          {/* Ready-state moment: a render landed — mark the payoff, don't bury it. */}
          {item.status === "ready" && readyCount > 0 && (
            <div
              className="mb-2 mt-8 flex items-center gap-2 rounded-lg border border-emerald-800/60 bg-emerald-950/20 px-4 py-3"
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
          <p className={`text-sm text-zinc-400 ${item.status === "ready" ? "" : "mt-8"}`} aria-live="polite">
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
          {item.status === "failed" && variants.length === 0 && (
            <p className="mt-2 text-sm text-zinc-500">
              Generation failed before any variant rendered. Try generating again.
            </p>
          )}
        </div>

        {/* ── Results: focused player + filmstrip + editor (full width) ── */}
        {showResults && (
          <div className="mt-6 flex flex-col gap-6 lg:flex-row lg:items-start">
            {/* Hero */}
            <div className="w-full shrink-0 sm:max-w-sm lg:w-[380px]">
              <Hero variant={focused} generating={isGenerating} />
            </div>
            {/* Filmstrip + editor */}
            <div className="min-w-0 flex-1 space-y-5">
              {variants.length > 0 && (
                <PlanFilmstrip
                  variants={variants}
                  focusedId={focusedVariantId}
                  onFocus={setFocusedVariantId}
                />
              )}
              {focused && focusedEditable ? (
                <PlanVariantEditor
                  variant={focused}
                  tracks={tracks}
                  styleSets={styleSets}
                  onSwap={(trackId) =>
                    runEdit(focused.variant_id, focused.output_url, () =>
                      swapPlanItemSong(itemId, focused.variant_id, trackId),
                    )
                  }
                  onRetext={(text) =>
                    runEdit(focused.variant_id, focused.output_url, () =>
                      retextPlanItem(itemId, focused.variant_id, { text }),
                    )
                  }
                  onRemoveText={() =>
                    runEdit(focused.variant_id, focused.output_url, () =>
                      retextPlanItem(itemId, focused.variant_id, { remove: true }),
                    )
                  }
                  onChangeStyle={(styleSetId) =>
                    runEdit(focused.variant_id, focused.output_url, () =>
                      changePlanItemStyle(itemId, focused.variant_id, styleSetId),
                    )
                  }
                />
              ) : (
                isGenerating && (
                  <p className="text-sm text-zinc-500">
                    Edit controls unlock as soon as a variant finishes rendering.
                  </p>
                )
              )}
            </div>
          </div>
        )}
      </div>
    </PlanShell>
  );
}

/** Large hero player for the focused variant. Keeps the previous video visible
 *  with an overlay while a re-render is in flight (never blanks the payoff). */
function Hero({
  variant,
  generating,
}: {
  variant: PlanItemVariant | null;
  generating: boolean;
}) {
  if (!variant) return <SkeletonTile />;
  const rendering = variant.render_status === "rendering";
  const failed = variant.render_status === "failed";
  return (
    <div className="relative aspect-[9/16] w-full overflow-hidden rounded-xl border border-zinc-800 bg-black">
      {variant.output_url ? (
        <video src={variant.output_url} controls className="h-full w-full object-contain" />
      ) : failed ? (
        <div className="flex h-full items-center justify-center px-4 text-center text-sm text-red-300">
          This variant failed — try editing again.
        </div>
      ) : (
        <div className="flex h-full items-center justify-center text-sm text-zinc-500">
          {generating ? "Rendering…" : "No preview yet"}
        </div>
      )}
      {rendering && variant.output_url && (
        <div
          className="absolute inset-0 flex items-center justify-center bg-black/55 text-sm text-amber-300"
          role="status"
        >
          Rendering new version…
        </div>
      )}
    </div>
  );
}

function SkeletonTile() {
  return (
    <div className="aspect-[9/16] w-full animate-shimmer rounded-xl border border-zinc-800 bg-[length:200%_100%] bg-gradient-to-r from-zinc-900 via-zinc-800 to-zinc-900" />
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
