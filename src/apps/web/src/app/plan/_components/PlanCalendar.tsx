"use client";

import { useState } from "react";
import { type ContentPlan, generateFirstWeek, type PlanItem } from "@/lib/plan-api";
import PlanItemCard from "./PlanItemCard";

function groupByWeek(items: PlanItem[]): { week: number; items: PlanItem[] }[] {
  const byWeek = new Map<number, PlanItem[]>();
  const sorted = Array.from(items).sort((a, b) => a.day_index - b.day_index);
  for (const it of sorted) {
    const week = Math.floor((it.day_index - 1) / 7) + 1;
    if (!byWeek.has(week)) byWeek.set(week, []);
    byWeek.get(week)!.push(it);
  }
  return Array.from(byWeek.entries()).map(([week, weekItems]) => ({ week, items: weekItems }));
}

/**
 * Week-grouped plan calendar. Week 1 ("activation") gets a batch
 * "Generate week 1" CTA wired to the previously-unused generateFirstWeek
 * endpoint — renders every day-1..7 item that has clips in one click.
 */
export default function PlanCalendar({
  plan,
  onError,
  onRefresh,
}: {
  plan: ContentPlan;
  onError: (msg: string) => void;
  onRefresh: () => void;
}) {
  const weeks = groupByWeek(plan.items);
  const [batching, setBatching] = useState(false);
  const [batchNote, setBatchNote] = useState<string | null>(null);

  const week1 = weeks.find((w) => w.week === 1)?.items ?? [];
  const week1WithClips = week1.filter((i) => i.clip_gcs_paths.length > 0).length;

  async function handleGenerateWeek1() {
    setBatching(true);
    setBatchNote(null);
    try {
      const res = await generateFirstWeek(plan.id);
      setBatchNote(
        res.enqueued > 0
          ? `Rendering ${res.enqueued} video${res.enqueued === 1 ? "" : "s"}…` +
              (res.skipped_no_clips > 0
                ? ` (${res.skipped_no_clips} skipped — add clips first)`
                : "")
          : "No week-1 ideas have clips yet — open one and upload footage first.",
      );
      onRefresh();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to start week 1");
    } finally {
      setBatching(false);
    }
  }

  return (
    <div className="animate-fade-up py-2">
      <h1 className="mb-1 font-display text-3xl text-white">Your 30-day plan</h1>
      <p className="mb-8 text-zinc-400">
        Edit any idea. Week 1 is your activation week — film those first.
      </p>

      {weeks.map(({ week, items }) => (
        <section key={week} className="mb-10">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
              Week {week}
              {week === 1 && <span className="ml-2 text-amber-400">· activation</span>}
            </h2>
            {week === 1 && (
              <button
                onClick={handleGenerateWeek1}
                disabled={batching || week1WithClips === 0}
                title={
                  week1WithClips === 0
                    ? "Upload clips to a week-1 idea first"
                    : `Render ${week1WithClips} idea(s) with clips`
                }
                className="rounded-full bg-amber-400 px-4 py-1.5 text-xs font-medium text-black transition-colors hover:bg-amber-300 disabled:cursor-not-allowed disabled:bg-zinc-800 disabled:text-zinc-500"
              >
                {batching ? "Starting…" : "Generate week 1"}
              </button>
            )}
          </div>
          {week === 1 && batchNote && (
            <p className="mb-3 text-xs text-amber-300">{batchNote}</p>
          )}
          <div className="space-y-3">
            {items.map((item) => (
              <PlanItemCard key={item.id} item={item} onError={onError} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
