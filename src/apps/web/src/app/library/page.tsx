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

/**
 * "My videos" — every video the signed-in user has made, newest first.
 * Strictly user-scoped server-side (GET /me/jobs). Each ready video can be
 * downloaded (native video controls) or pinned to a plan day.
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
        // The plan is only needed to populate the Add-to-plan day picker; a
        // missing plan must not break the library, so swallow its errors.
        getContentPlan().catch(() => null),
      ]);
      setJobs(page.jobs);
      setCursor(page.next_cursor);
      setPlan(p);
      setLoadState("ready");
    } catch (err) {
      if (err instanceof NotAuthenticatedError) return; // gate below handles it
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
      // leave the existing list; the "Load more" button stays for a retry
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
    return <Shell>{null}</Shell>;
  }
  if (authStatus !== "authenticated") {
    return (
      <Shell>
        <SignInPrompt
          callbackUrl="/library"
          title="Sign in to see your videos"
          subtitle="Your library keeps every video you make in one place."
        />
      </Shell>
    );
  }

  return (
    <Shell>
      <header className="mb-8">
        <p className="font-display text-3xl text-white">Your videos</p>
        <p className="mt-1 text-sm text-zinc-400">Everything you&apos;ve made, newest first.</p>
      </header>

      {loadState === "loading" && <SkeletonGrid />}

      {loadState === "error" && (
        <div className="py-16 text-center">
          <p className="text-zinc-300">We couldn&apos;t load your videos.</p>
          <button
            type="button"
            onClick={() => void load()}
            className="mt-4 min-h-11 rounded-full border border-zinc-700 px-5 py-2 text-sm text-zinc-200 hover:border-zinc-400 hover:text-white"
          >
            Try again
          </button>
        </div>
      )}

      {loadState === "ready" && jobs.length === 0 && (
        <div className="animate-fade-up py-20 text-center">
          <p className="font-display text-2xl text-white">Your videos live here</p>
          <p className="mx-auto mt-2 max-w-sm text-zinc-400">
            Make your first one — upload a few clips and we&apos;ll turn them into something worth
            posting.
          </p>
          <a
            href="/generative"
            className="mt-6 inline-flex min-h-11 items-center rounded-full bg-amber-400 px-6 py-3 font-medium text-black transition-colors hover:bg-amber-300"
          >
            Create a video
          </a>
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
                className="min-h-11 rounded-full border border-zinc-700 px-6 py-2 text-sm text-zinc-200 hover:border-zinc-400 hover:text-white disabled:opacity-60"
              >
                {loadingMore ? "Loading…" : "Load more"}
              </button>
            </div>
          )}
        </>
      )}
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return <main className="mx-auto min-h-screen max-w-6xl px-4 py-10">{children}</main>;
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
          className="aspect-[9/16] rounded-xl border border-zinc-800 bg-[linear-gradient(110deg,#18181b,45%,#27272a,55%,#18181b)] bg-[length:200%_100%] motion-safe:animate-shimmer"
        />
      ))}
    </ul>
  );
}
