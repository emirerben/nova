"use client";

import { useState } from "react";
import type { ContentPlan } from "@/lib/plan-api";
import { addJobToPlan, type LibraryJob } from "@/lib/me-api";
import { downloadVideo } from "@/lib/download-video";
import FeedbackButtons from "./FeedbackButtons";

/**
 * One 9:16 video in the library. Light editorial canvas (D20/D21).
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
    <div className="motion-safe:animate-fade-up">
      <div className="relative aspect-[9/16] overflow-hidden rounded-xl border border-zinc-200 bg-zinc-100">
        {job.status === "ready" && job.output_url ? (
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
            <span className="text-sm text-[#3f3f46]">This render didn&apos;t finish</span>
            <span className="text-xs text-[#a1a1aa]">{job.raw_status.replace(/_/g, " ")}</span>
          </div>
        ) : (
          <div className="flex h-full w-full items-center justify-center bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer">
            <span className="text-sm text-[#71717a]">Rendering…</span>
          </div>
        )}
      </div>

      {job.status === "ready" && (
        <div className="mt-2">
          {job.content_plan_item_id ? (
            <span className="inline-flex items-center text-xs font-medium uppercase tracking-wide text-lime-700">
              In your plan
            </span>
          ) : (
            <AddToPlan job={job} plan={plan} onPinned={onPinned} />
          )}
          {job.output_url && (
            <button
              type="button"
              onClick={() => downloadVideo(job.output_url!, `kria-${job.id.slice(0, 8)}.mp4`)}
              className="mt-2 min-h-11 rounded-full border border-zinc-200 px-3 py-1.5 text-xs font-medium text-[#3f3f46] transition-colors hover:border-zinc-400"
            >
              Download
            </button>
          )}
          <FeedbackButtons jobId={job.id} initialSignal={job.feedback_signal} />
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
      <a href="/plan" className="text-xs text-[#71717a] underline-offset-2 hover:underline">
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
        className="min-h-11 rounded-full border border-zinc-200 px-3 py-1.5 text-xs font-medium text-[#3f3f46] transition-colors hover:border-zinc-400 disabled:opacity-60"
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
            className="min-h-11 w-full rounded-lg border border-zinc-200 bg-white px-2 text-sm text-[#3f3f46]"
          >
            <option value="" disabled>
              Pick a day…
            </option>
            {plan.items.filter((it) => it.day_index != null).map((it) => (
              <option key={it.id} value={it.day_index!}>
                Day {it.day_index} — {it.theme}
              </option>
            ))}
          </select>
        </label>
      )}
      {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
    </div>
  );
}
