"use client";

import { useState } from "react";
import type { ContentPlan } from "@/lib/plan-api";
import { addJobToPlan, type LibraryJob } from "@/lib/me-api";

/**
 * One 9:16 video in the library. Three visual states mirror the design spec:
 * ready (playable video + Add-to-plan), generating (shimmer placeholder),
 * failed (muted error). The content is the visual — no decorative chrome.
 */
export default function LibraryTile({
  job,
  plan,
  onPinned,
}: {
  job: LibraryJob;
  plan: ContentPlan | null;
  onPinned: (planItemId: string) => void;
}) {
  return (
    <div className="animate-fade-up">
      <div className="relative aspect-[9/16] overflow-hidden rounded-xl border border-zinc-800 bg-zinc-950">
        {job.status === "ready" && job.output_url ? (
          // The mp4's first frame acts as the poster; metadata-only until played.
          <video
            src={job.output_url}
            controls
            preload="metadata"
            playsInline
            className="h-full w-full object-cover"
            aria-label="Your video"
          />
        ) : job.status === "failed" ? (
          <div className="flex h-full w-full flex-col items-center justify-center gap-2 p-4 text-center">
            <span className="text-sm text-zinc-300">This render didn&apos;t finish</span>
            <span className="text-xs text-zinc-500">{job.raw_status.replace(/_/g, " ")}</span>
          </div>
        ) : (
          <div className="flex h-full w-full items-center justify-center bg-[linear-gradient(110deg,#18181b,45%,#27272a,55%,#18181b)] bg-[length:200%_100%] motion-safe:animate-shimmer">
            <span className="text-sm text-zinc-400">Rendering…</span>
          </div>
        )}
      </div>

      {job.status === "ready" && (
        <div className="mt-2">
          {job.content_plan_item_id ? (
            <span className="inline-flex items-center text-xs font-medium uppercase tracking-wide text-amber-300">
              In your plan
            </span>
          ) : (
            <AddToPlan job={job} plan={plan} onPinned={onPinned} />
          )}
        </div>
      )}
    </div>
  );
}

function AddToPlan({
  job,
  plan,
  onPinned,
}: {
  job: LibraryJob;
  plan: ContentPlan | null;
  onPinned: (planItemId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!plan || plan.items.length === 0) {
    return (
      <a href="/plan" className="text-xs text-zinc-500 underline-offset-2 hover:underline">
        Create a plan to pin this →
      </a>
    );
  }

  async function pin(dayIndex: number) {
    setBusy(true);
    setError(null);
    try {
      const updated = await addJobToPlan(job.id, dayIndex);
      if (updated.content_plan_item_id) onPinned(updated.content_plan_item_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't add to plan");
    } finally {
      setBusy(false);
      setOpen(false);
    }
  }

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={busy}
        aria-expanded={open}
        className="min-h-11 rounded-full border border-zinc-700 px-3 py-1.5 text-xs font-medium text-zinc-200 transition-colors hover:border-zinc-400 hover:text-white disabled:opacity-60"
      >
        {busy ? "Adding…" : "Add to plan"}
      </button>
      {open && (
        <label className="mt-2 block">
          <span className="sr-only">Choose a plan day</span>
          <select
            defaultValue=""
            onChange={(e) => {
              const v = e.target.value;
              if (v) void pin(Number(v));
            }}
            className="min-h-11 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-2 text-sm text-zinc-200"
          >
            <option value="" disabled>
              Pick a day…
            </option>
            {plan.items.map((it) => (
              <option key={it.id} value={it.day_index}>
                Day {it.day_index} — {it.theme}
              </option>
            ))}
          </select>
        </label>
      )}
      {error && <p className="mt-1 text-xs text-red-400">{error}</p>}
    </div>
  );
}
