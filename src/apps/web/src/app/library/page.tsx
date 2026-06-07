"use client";

import { useCallback, useEffect, useState } from "react";
import { useSession } from "next-auth/react";
import SignInPrompt from "@/app/plan/_components/SignInPrompt";
import {
  listMyJobs,
  type LibraryJob,
  NotAuthenticatedError,
} from "@/lib/me-api";
import { getContentPlan, type ContentPlan } from "@/lib/plan-api";
import LibraryTile from "./_components/LibraryTile";
import { LightShell } from "@/components/ui/LightShell";
import { Eyebrow } from "@/components/ui/Eyebrow";
import { InkButton } from "@/components/ui/InkButton";

/**
 * "My videos" — every video the signed-in user has made, newest first.
 * Light editorial canvas (D20/D21).
 */
export default function LibraryPage() {
  const { status: authStatus } = useSession();

  const [jobs, setJobs] = useState<LibraryJob[]>([]);
  const [plan, setPlan] = useState<ContentPlan | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">("loading");
  const [loadingMore, setLoadingMore] = useState(false);

  const load = useCallback(async () => {
    setLoadState("loading");
    try {
      const [page, p] = await Promise.all([
        listMyJobs(),
        getContentPlan().catch(() => null),
      ]);
      setJobs(page.jobs);
      setCursor(page.next_cursor);
      setPlan(p);
      setLoadState("ready");
    } catch (err) {
      if (err instanceof NotAuthenticatedError) return;
      setLoadState("error");
    }
  }, []);

  useEffect(() => {
    if (authStatus === "authenticated") void load();
  }, [authStatus, load]);

  async function loadMore() {
    if (!cursor) return;
    setLoadingMore(true);
    try {
      const page = await listMyJobs({ cursor });
      setJobs((prev) => [...prev, ...page.jobs]);
      setCursor(page.next_cursor);
    } catch {
      // leave existing list
    } finally {
      setLoadingMore(false);
    }
  }

  function onPinned(jobId: string, planItemId: string) {
    setJobs((prev) =>
      prev.map((j) => (j.id === jobId ? { ...j, content_plan_item_id: planItemId } : j)),
    );
  }

  if (authStatus === "loading") {
    return <LightShell size="wide">{null}</LightShell>;
  }
  if (authStatus !== "authenticated") {
    return (
      <LightShell size="narrow">
        <SignInPrompt
          callbackUrl="/library"
          title="Sign in to see your videos"
          subtitle="Your library keeps every video you make in one place."
        />
      </LightShell>
    );
  }

  return (
    <LightShell size="wide">
      <header className="mb-8">
        <Eyebrow tone="muted" className="mb-2">Your library</Eyebrow>
        <p className="font-display text-3xl text-[#0c0c0e]">Your videos</p>
        <p className="mt-1 text-sm text-[#71717a]">Everything you&apos;ve made, newest first.</p>
      </header>

      {loadState === "loading" && <SkeletonGrid />}

      {loadState === "error" && (
        <div className="py-16 text-center">
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
        <div className="motion-safe:animate-fade-up py-20 text-center">
          <p className="font-display text-2xl text-[#0c0c0e]">Your videos live here</p>
          <p className="mx-auto mt-2 max-w-sm text-[#71717a]">
            Make your first one — upload a few clips and we&apos;ll turn them into something worth
            posting.
          </p>
          <div className="mt-6 flex justify-center">
            <InkButton onClick={() => { window.location.href = "/generative"; }}>
              Create a video
            </InkButton>
          </div>
        </div>
      )}

      {loadState === "ready" && jobs.length > 0 && (
        <>
          <ul className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
            {jobs.map((job) => (
              <li key={job.id}>
                <LibraryTile job={job} plan={plan} onPinned={(itemId) => onPinned(job.id, itemId)} />
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
    </LightShell>
  );
}

function SkeletonGrid() {
  return (
    <ul
      className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4"
      aria-label="Loading your videos"
    >
      {Array.from({ length: 8 }).map((_, i) => (
        <li
          key={i}
          className="aspect-[9/16] rounded-xl border border-zinc-200 bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer"
        />
      ))}
    </ul>
  );
}
