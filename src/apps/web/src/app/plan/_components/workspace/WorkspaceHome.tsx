"use client";

/**
 * WorkspaceHome — the /plan home (v0.44 redesign).
 *
 * Design source: Paper file "Kria Plan Redesign", page "FINAL — Basic home"
 * (F1/F4). The page is openly the create-a-new-video surface: a basic create
 * block on top ("Make a new video." → /plan/new), with a PAST EDITS grid of
 * the user's rendered videos underneath (folds in the old /library page).
 * The ideas ledger is gone — items are created by the New-video flow.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import type { ContentPlan } from "@/lib/plan-api";
import { listMyJobs, type LibraryJob } from "@/lib/me-api";
import type { TikTokConnection } from "@/lib/tiktok-api";
import LibraryTile from "@/components/library/LibraryTile";
import TikTokConnectionCard from "@/components/library/TikTokConnectionCard";
import SeedUploadCard from "../SeedUploadCard";

interface WorkspaceHomeProps {
  plan: ContentPlan;
  onRefresh: () => void | Promise<unknown>;
  onPlanChange: (plan: ContentPlan) => void;
  onError: (msg: string) => void;
}

export function WorkspaceHome({ plan, onRefresh, onError }: WorkspaceHomeProps) {
  const activating = ["seeding", "activating"].includes(plan.activation_status ?? "");
  const planGenerating = plan.plan_status === "generating";

  const [jobs, setJobs] = useState<LibraryJob[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">("loading");
  const [loadingMore, setLoadingMore] = useState(false);
  const [tiktokConnection, setTikTokConnection] = useState<TikTokConnection | null>(null);

  const load = useCallback(async () => {
    setLoadState("loading");
    try {
      const page = await listMyJobs();
      setJobs(page.jobs);
      setCursor(page.next_cursor);
      setLoadState("ready");
    } catch {
      setLoadState("error");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function loadMore() {
    if (!cursor) return;
    setLoadingMore(true);
    try {
      const page = await listMyJobs({ cursor });
      setJobs((prev) => [...prev, ...page.jobs]);
      setCursor(page.next_cursor);
    } catch {
      // leave the existing list
    } finally {
      setLoadingMore(false);
    }
  }

  function onPinned(jobId: string, planItemId: string) {
    setJobs((prev) =>
      prev.map((j) => (j.id === jobId ? { ...j, content_plan_item_id: planItemId } : j)),
    );
  }

  return (
    <div className="min-h-screen bg-white">
      <div className="mx-auto flex max-w-[900px] flex-col gap-10 px-6 pb-24 pt-14">
        {activating && (
          <SeedUploadCard plan={plan} onError={onError} onRefresh={onRefresh} />
        )}

        {/* ---- Create block: the page's stated purpose ---- */}
        <section aria-labelledby="create-heading">
          <h1
            id="create-heading"
            className="font-display text-[32px] font-medium leading-tight text-[#0c0c0e]"
          >
            Make a new video.
          </h1>
          <p className="mt-1.5 max-w-md text-sm text-[#71717a]">
            Pick what kind, add your footage — Kria edits it into a post.
          </p>
          <Link
            href="/plan/new"
            className="mt-5 inline-flex min-h-12 w-full items-center justify-center rounded-full bg-[#0c0c0e] px-9 py-[15px] text-[15px] font-semibold text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 sm:w-auto"
          >
            New video
          </Link>
          {planGenerating && (
            <p className="mt-3 text-[13px] text-[#71717a]" role="status">
              Kria is still setting up your plan — you can start a video anyway.
            </p>
          )}
        </section>

        {/* ---- Past edits ---- */}
        <section aria-labelledby="past-edits-heading">
          <h2
            id="past-edits-heading"
            className="mb-4 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#3f3f46]"
          >
            Past edits
          </h2>

          {loadState === "loading" && <SkeletonGrid />}

          {loadState === "error" && (
            <div className="py-10">
              <p className="text-[#3f3f46]">We couldn&apos;t load your videos.</p>
              <button
                type="button"
                onClick={() => void load()}
                className="mt-4 min-h-11 rounded-full border border-zinc-200 px-5 py-2 text-sm text-[#3f3f46] hover:border-zinc-400"
              >
                Try again
              </button>
            </div>
          )}

          {loadState === "ready" && jobs.length === 0 && (
            <p className="py-6 text-[15px] text-[#71717a]">Your edits will live here.</p>
          )}

          {loadState === "ready" && jobs.length > 0 && (
            <>
              <ul className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
                {jobs.map((job) => (
                  <li key={job.id}>
                    <LibraryTile
                      job={job}
                      plan={plan}
                      canPublishToTikTok={Boolean(
                        tiktokConnection?.can_publish || tiktokConnection?.can_upload_draft,
                      )}
                      onPinned={(itemId) => onPinned(job.id, itemId)}
                    />
                  </li>
                ))}
              </ul>
              {cursor && (
                <div className="mt-8 text-center">
                  <button
                    type="button"
                    onClick={() => void loadMore()}
                    disabled={loadingMore}
                    className="min-h-11 rounded-full border border-zinc-200 px-6 py-2 text-sm text-[#3f3f46] hover:border-zinc-400 disabled:opacity-60"
                  >
                    {loadingMore ? "Loading…" : "Load more"}
                  </button>
                </div>
              )}
            </>
          )}
        </section>

        {/* ---- TikTok connection (was /library's card; release rails link here) ---- */}
        <section id="tiktok" aria-label="TikTok connection">
          <TikTokConnectionCard onConnection={setTikTokConnection} />
        </section>
      </div>
    </div>
  );
}

function SkeletonGrid() {
  return (
    <ul
      className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4"
      aria-label="Loading your videos"
    >
      {Array.from({ length: 4 }).map((_, i) => (
        <li
          key={i}
          className="aspect-[9/16] rounded-xl border border-zinc-200 bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer"
        />
      ))}
    </ul>
  );
}
