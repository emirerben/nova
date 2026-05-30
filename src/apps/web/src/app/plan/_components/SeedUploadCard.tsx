"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  activatePlan,
  attachSeedClips,
  type ContentPlan,
  getActivation,
  requestSeedUploadUrls,
  uploadToGcs,
} from "@/lib/plan-api";

const POLL_MS = 2000;
const ACCEPT = "video/mp4,video/quicktime";

/**
 * Activation seed (T8): after the plan is ready, the user drops in one batch of
 * recent clips. We match them to plan items and auto-generate the best fit(s),
 * so they see a finished video before filming anything new.
 *
 * Owns its own activation poll (the page-level poll only runs while the persona
 * or plan are generating, not during activation). Keyed on a boolean so the
 * interval keeps firing across polls — same pattern as the wizard's load poll.
 */
export default function SeedUploadCard({
  plan,
  onError,
  onRefresh,
}: {
  plan: ContentPlan;
  onError: (msg: string) => void;
  onRefresh: () => void | Promise<unknown>;
}) {
  const [uploading, setUploading] = useState(false);
  const [seededCount, setSeededCount] = useState(plan.seed_clip_count);
  const [activating, setActivating] = useState(plan.activation_status === "activating");
  const [done, setDone] = useState<"activated" | "activated_empty" | "failed" | null>(
    plan.activation_status === "activated" ||
      plan.activation_status === "activated_empty" ||
      plan.activation_status === "failed"
      ? plan.activation_status
      : null,
  );
  const onRefreshRef = useRef(onRefresh);
  onRefreshRef.current = onRefresh;

  // Poll while activation runs; stop + refresh the plan on a terminal status.
  useEffect(() => {
    if (!activating) return;
    const id = setInterval(async () => {
      try {
        const st = await getActivation(plan.id);
        if (st.activation_status !== "activating") {
          setActivating(false);
          if (
            st.activation_status === "activated" ||
            st.activation_status === "activated_empty" ||
            st.activation_status === "failed"
          ) {
            setDone(st.activation_status);
          }
          void onRefreshRef.current();
        }
      } catch {
        // transient poll error — keep trying until a terminal status lands
      }
    }, POLL_MS);
    return () => clearInterval(id);
  }, [activating, plan.id]);

  const handleFiles = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return;
      setUploading(true);
      onError("");
      try {
        const list = Array.from(files);
        const urls = await requestSeedUploadUrls(
          plan.id,
          list.map((f) => ({
            filename: f.name,
            content_type: f.type || "video/mp4",
            file_size_bytes: f.size,
          })),
        );
        await Promise.all(urls.map((u, i) => uploadToGcs(u.upload_url, list[i])));
        await attachSeedClips(
          plan.id,
          urls.map((u) => u.gcs_path),
        );
        setSeededCount((n) => n + list.length);
        setDone(null);
      } catch (err) {
        onError(err instanceof Error ? err.message : "Upload failed");
      } finally {
        setUploading(false);
      }
    },
    [plan.id, onError],
  );

  const handleActivate = useCallback(async () => {
    onError("");
    try {
      await activatePlan(plan.id);
      setActivating(true);
      setDone(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Couldn't start matching");
    }
  }, [plan.id, onError]);

  if (activating) {
    return (
      <section
        className="mb-8 rounded-xl border border-amber-700/50 bg-amber-950/20 p-5"
        role="status"
        aria-live="polite"
      >
        <h2 className="mb-1 font-display text-lg text-amber-200">Finding your best clip…</h2>
        <p className="text-sm text-amber-200/70">
          Matching your footage to the days it fits best and rendering a first video. This takes a
          couple of minutes — the matched day(s) below will start generating.
        </p>
      </section>
    );
  }

  if (done === "activated") {
    return (
      <section className="mb-8 rounded-xl border border-emerald-800/50 bg-emerald-950/20 p-5">
        <h2 className="mb-1 font-display text-lg text-emerald-200">Your first video is on the way</h2>
        <p className="text-sm text-emerald-200/70">
          We matched your clips to the best-fit day(s) below — open a generating card to watch it
          render. Upload more clips any time to activate other days.
        </p>
      </section>
    );
  }

  return (
    <section className="mb-8 rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
      <h2 className="mb-1 font-display text-lg text-white">Skip the homework — start with footage you already have</h2>
      <p className="mb-4 text-sm text-zinc-400">
        Upload a batch of recent clips and we&apos;ll find the day they fit best and render a first
        video for you — no need to film against the plan yet.
        {seededCount > 0 ? ` ${seededCount} clip${seededCount === 1 ? "" : "s"} ready.` : ""}
      </p>
      {done === "activated_empty" && (
        <div className="mb-4 rounded border border-zinc-700 bg-zinc-800/60 px-4 py-3 text-sm text-zinc-300">
          We couldn&apos;t confidently match those clips to a day — no problem. Pick a day below and
          film for it, or try a different batch.
        </div>
      )}
      {done === "failed" && (
        <div className="mb-4 rounded border border-amber-700 bg-amber-950/40 px-4 py-3 text-sm text-amber-200">
          Something went wrong matching your clips. Try uploading again.
        </div>
      )}
      <label className="block">
        <span className="sr-only">Upload recent video clips to activate your plan</span>
        <input
          type="file"
          accept={ACCEPT}
          multiple
          disabled={uploading}
          onChange={(e) => void handleFiles(e.target.files)}
          className="block w-full text-sm text-zinc-400 file:mr-3 file:rounded-full file:border-0 file:bg-white file:px-4 file:py-2 file:text-sm file:font-medium file:text-black hover:file:bg-zinc-200"
        />
      </label>
      {uploading && <p className="mt-3 text-sm text-amber-300">Uploading…</p>}
      <button
        onClick={() => void handleActivate()}
        disabled={uploading || seededCount === 0}
        className="mt-4 rounded-full bg-amber-400 px-6 py-3 font-medium text-black transition-colors hover:bg-amber-300 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-400"
      >
        Find my best clip
      </button>
      {seededCount === 0 && !uploading && (
        <p className="mt-2 text-sm text-zinc-500">Upload at least one clip first.</p>
      )}
    </section>
  );
}
