"use client";

import Link from "next/link";
import { useState } from "react";
import { cn } from "@/lib/cn";
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

/** Pure progress summary for the momentum header. Exported for unit testing. */
export function planProgress(items: PlanItem[]): { total: number; made: number; pct: number } {
  const total = items.length;
  const made = items.filter((i) => i.status === "ready").length;
  const pct = total > 0 ? Math.round((made / total) * 100) : 0;
  return { total, made, pct };
}

const weekOf = (dayIndex: number) => Math.floor((dayIndex - 1) / 7) + 1;

export interface PlanNudge {
  text: string;
  itemId?: string;
}

/**
 * The single clearest next action, computed from item statuses (no backend).
 * Drives the momentum nudge: points the user at the one thing worth doing next
 * and doubles as the "welcome back, you're on week N" beat for returning users.
 * Returns null only for an empty plan.
 */
export function planNudge(items: PlanItem[]): PlanNudge | null {
  if (items.length === 0) return null;
  const sorted = Array.from(items).sort((a, b) => a.day_index - b.day_index);

  if (sorted.every((i) => i.status === "ready")) {
    return { text: `You've made all ${sorted.length} videos. Incredible run.` };
  }

  const week1 = sorted.filter((i) => weekOf(i.day_index) === 1);
  const nextW1 = week1.find((i) => i.status !== "ready");
  if (nextW1) {
    if (nextW1.status === "generating") {
      return { text: `Day ${nextW1.day_index} is rendering now.`, itemId: nextW1.id };
    }
    if (nextW1.clip_gcs_paths.length > 0) {
      return { text: `Day ${nextW1.day_index} has clips — generate it next.`, itemId: nextW1.id };
    }
    const needClips = week1.filter(
      (i) => i.status !== "ready" && i.clip_gcs_paths.length === 0,
    ).length;
    if (needClips > 1) {
      return {
        text: `${needClips} week-1 ideas still need clips — start with day ${nextW1.day_index}.`,
        itemId: nextW1.id,
      };
    }
    return { text: `Film day ${nextW1.day_index} next — upload its clips to get started.`, itemId: nextW1.id };
  }

  // Week 1 done — surface the resume beat for whatever's next.
  const next = sorted.find((i) => i.status !== "ready");
  if (next) {
    return {
      text: `Week 1 done — you're on week ${weekOf(next.day_index)}. Day ${next.day_index} is next.`,
      itemId: next.id,
    };
  }
  return null;
}

/**
 * Overview-first plan: a progress header + week accordion (week 1 "activation"
 * open, later weeks collapsed) so the month is scannable instead of a flat wall
 * of 30 open edit cards. Week 1 keeps the batch "Generate week 1" CTA.
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
  // Week 1 expanded by default; later weeks collapsed (peek). Ephemeral UI state.
  const [expanded, setExpanded] = useState<Set<number>>(new Set([1]));

  const { total, made, pct } = planProgress(plan.items);
  const nudge = planNudge(plan.items);

  const week1 = weeks.find((w) => w.week === 1)?.items ?? [];
  const week1WithClips = week1.filter((i) => i.clip_gcs_paths.length > 0).length;

  function toggleWeek(week: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(week)) next.delete(week);
      else next.add(week);
      return next;
    });
  }

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

  // Empty-calendar edge state: plan is ready but produced no day ideas.
  if (plan.items.length === 0) {
    return (
      <div className="animate-fade-up py-2">
        <h1 className="mb-1 font-display text-3xl text-white">Your 30-day plan</h1>
        <div className="mt-6 rounded-xl border border-zinc-800 bg-zinc-900/60 p-8 text-center">
          <p className="text-zinc-300">Your plan came back empty.</p>
          <p className="mt-2 text-sm text-zinc-500">
            That&apos;s unusual — regenerating from your persona usually fixes it.
          </p>
          <button
            onClick={onRefresh}
            className="mt-6 inline-flex min-h-[44px] items-center rounded-full border border-zinc-700 px-5 py-3 text-sm font-medium text-zinc-200 transition-colors hover:border-zinc-400 hover:text-white"
          >
            Reload plan
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="animate-fade-up py-2">
      <h1 className="mb-1 font-display text-3xl text-white">Your 30-day plan</h1>
      <p className="mb-6 text-zinc-400">
        Edit any idea. Week 1 is your activation week — film those first.
      </p>

      {/* Momentum header: how many videos are made of the whole plan. */}
      <div className="mb-4">
        <div className="mb-2 flex items-center justify-between text-sm">
          <span className="text-zinc-300">
            {made} of {total} video{total === 1 ? "" : "s"} made
          </span>
          <span className="text-zinc-500">{pct}%</span>
        </div>
        <div
          className="h-1.5 w-full overflow-hidden rounded-full bg-zinc-800"
          role="progressbar"
          aria-valuenow={made}
          aria-valuemin={0}
          aria-valuemax={total}
          aria-label={`${made} of ${total} videos made`}
        >
          <div
            className="h-full rounded-full bg-amber-400 transition-all"
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>

      {/* Next-action nudge: the one thing worth doing next (also the resume beat). */}
      {nudge && (
        <p className="mb-8 text-sm text-amber-300" aria-live="polite">
          {nudge.itemId ? (
            <Link
              href={`/plan/items/${nudge.itemId}`}
              className="underline-offset-2 transition-colors hover:text-amber-200 hover:underline"
            >
              {nudge.text}
            </Link>
          ) : (
            nudge.text
          )}
        </p>
      )}

      {weeks.map(({ week, items }) => {
        const isOpen = expanded.has(week);
        const ready = items.filter((i) => i.status === "ready").length;
        return (
          <section key={week} className="mb-6">
            <div className="mb-3 flex items-center justify-between gap-3">
              <button
                type="button"
                onClick={() => toggleWeek(week)}
                aria-expanded={isOpen}
                className="flex min-w-0 items-center gap-2 text-sm font-semibold uppercase tracking-wide text-zinc-400 transition-colors hover:text-white"
              >
                <span
                  aria-hidden="true"
                  className={cn("inline-block transition-transform", isOpen && "rotate-90")}
                >
                  ›
                </span>
                <span>Week {week}</span>
                {week === 1 && <span className="text-amber-400">· activation</span>}
                <span className="truncate font-normal normal-case text-zinc-600">
                  · {items.length} idea{items.length === 1 ? "" : "s"}
                  {ready > 0 ? ` · ${ready} ready` : ""}
                </span>
              </button>
              {week === 1 && (
                <button
                  onClick={handleGenerateWeek1}
                  disabled={batching || week1WithClips === 0}
                  title={
                    week1WithClips === 0
                      ? "Upload clips to a week-1 idea first"
                      : `Render ${week1WithClips} idea(s) with clips`
                  }
                  className="shrink-0 rounded-full bg-amber-400 px-4 py-1.5 text-xs font-medium text-black transition-colors hover:bg-amber-300 disabled:cursor-not-allowed disabled:bg-zinc-800 disabled:text-zinc-500"
                >
                  {batching ? "Starting…" : "Generate week 1"}
                </button>
              )}
            </div>
            {week === 1 && batchNote && (
              <p className="mb-3 text-xs text-amber-300">{batchNote}</p>
            )}
            {isOpen && (
              <div className="space-y-3">
                {items.map((item) => (
                  <PlanItemCard key={item.id} item={item} onError={onError} />
                ))}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}
