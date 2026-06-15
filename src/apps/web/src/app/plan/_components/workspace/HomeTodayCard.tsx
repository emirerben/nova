"use client";

/**
 * HomeTodayCard — the dominant "Today" card on the redesigned plan home.
 *
 * Layout (variant A — "Today-first"):
 * - Fraunces display heading (idea.theme)
 * - Status pill (lime "Ready to film" | zinc "Awaiting clips" | etc.)
 * - Glanceable shot chips from filming_guide (max 4 shown)
 * - One ink-pill CTA: "Film this →" → navigates to /plan/items/[id]
 * - Secondary ghost link: "Swap this idea" (reroll)
 * - Empty state: "Nothing planned for today — pick an idea from the list"
 * - All-done state: "All N made." + new-plan form
 */

import { useState } from "react";
import Link from "next/link";
import type { PlanItem, ContentPlan } from "@/lib/plan-api";
import { rerollPlanItem, createContentPlan } from "@/lib/plan-api";
import { LightCard } from "../ui/LightCard";
import { Eyebrow } from "../ui/Eyebrow";
import { InkButton } from "../ui/InkButton";

const STATUS_LABELS: Record<string, { text: string; cls: string }> = {
  idea: {
    text: "Ready to film",
    cls: "border-lime-200 bg-lime-50 text-lime-800",
  },
  awaiting_clips: {
    text: "Awaiting clips",
    cls: "border-zinc-200 bg-white text-[#71717a]",
  },
  generating: {
    text: "Generating…",
    cls: "border-zinc-200 bg-white text-[#71717a]",
  },
  ready: {
    text: "Published",
    cls: "border-lime-200 bg-lime-50 text-lime-800",
  },
  failed: {
    text: "Failed",
    cls: "border-zinc-200 bg-white text-[#71717a]",
  },
  rerolling: {
    text: "Finding a new idea…",
    cls: "border-zinc-200 bg-white text-[#71717a]",
  },
};

interface HomeTodayCardProps {
  /** The next-action item (first non-ready item) — null means all done or no plan */
  nextItem: PlanItem | null;
  plan: ContentPlan;
  horizonDays: number;
  calendarDay: number | null;
  weekdayLabel: string | null;
  behind: number;
  onRefresh: () => void;
}

export function HomeTodayCard({
  nextItem,
  plan,
  horizonDays,
  calendarDay,
  weekdayLabel,
  behind,
  onRefresh,
}: HomeTodayCardProps) {
  const [rerolling, setRerolling] = useState(false);
  const [rerollError, setRerollError] = useState(false);
  const [showNewPlanForm, setShowNewPlanForm] = useState(false);
  const [events, setEvents] = useState("");
  const [generating, setGenerating] = useState(false);

  const allDone =
    nextItem === null &&
    (plan.items ?? []).length > 0 &&
    (plan.items ?? []).every((i) => i.status === "ready");

  const dayLabel =
    calendarDay !== null
      ? `Day ${calendarDay} of ${horizonDays}${weekdayLabel ? ` · ${weekdayLabel}` : ""}`
      : `Day of ${horizonDays}`;

  async function handleReroll() {
    if (!nextItem || rerolling) return;
    setRerolling(true);
    setRerollError(false);
    try {
      await rerollPlanItem(nextItem.id);
      onRefresh();
    } catch {
      setRerollError(true);
    } finally {
      setRerolling(false);
    }
  }

  async function handleNewPlan() {
    setGenerating(true);
    try {
      await createContentPlan(events, plan.horizon_days);
      onRefresh();
    } catch {
      setGenerating(false);
    }
  }

  // All done
  if (allDone) {
    return (
      <LightCard className="px-7 py-6">
        <Eyebrow tone="lime">{dayLabel}</Eyebrow>
        <h2 className="font-display mt-4 text-[32px] font-medium leading-snug text-[#0c0c0e]">
          All {(plan.items ?? []).length} made.
        </h2>
        <p className="mt-2 text-[14px] text-[#71717a]">Time to plan the next stretch.</p>
        {!showNewPlanForm ? (
          <InkButton className="mt-6" onClick={() => setShowNewPlanForm(true)}>
            Plan your next {horizonDays} days
          </InkButton>
        ) : (
          <div className="mt-5 space-y-3">
            <textarea
              className="w-full rounded-xl border border-zinc-200 bg-[#fafaf8] px-4 py-3 text-[14px] text-[#0c0c0e] placeholder-[#a1a1aa] focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
              placeholder="Anything coming up? Trips, launches, busy weeks…"
              rows={3}
              value={events}
              onChange={(e) => setEvents(e.target.value)}
            />
            <InkButton onClick={() => void handleNewPlan()} disabled={generating}>
              {generating ? "Generating…" : "Generate my plan"}
            </InkButton>
          </div>
        )}
      </LightCard>
    );
  }

  // No item yet (plan generating or no plan)
  if (!nextItem) {
    return (
      <LightCard className="px-7 py-8">
        {calendarDay !== null && <Eyebrow tone="lime">{dayLabel}</Eyebrow>}
        <p className="mt-4 text-[16px] text-[#71717a]">
          Nothing planned for today — pick an idea from your list.
        </p>
      </LightCard>
    );
  }

  const isRerolling = nextItem.status === "rerolling";
  const canReroll = nextItem.status === "idea" && !nextItem.current_job_id;
  const statusMeta = STATUS_LABELS[nextItem.status] ?? STATUS_LABELS.idea;

  // Shot chips — up to 4 from filming_guide; fall back to filming_suggestion snippet
  const shots = nextItem.filming_guide ?? [];
  const showShots = shots.length > 0;

  return (
    <LightCard className="overflow-hidden px-7 py-6">
      <div className="flex items-start justify-between gap-4">
        <Eyebrow tone="lime">{dayLabel}</Eyebrow>
        {/* Status pill */}
        <span
          className={[
            "shrink-0 rounded-full border px-3 py-1 text-[11px] font-semibold",
            statusMeta.cls,
          ].join(" ")}
        >
          {statusMeta.text}
        </span>
      </div>

      {behind > 0 && (
        <p className="mt-2 text-[13px] text-[#71717a]">
          {behind} {behind === 1 ? "day" : "days"} behind — this is the lightest way to catch up.
        </p>
      )}

      {/* Display heading — Fraunces */}
      <h2
        className={[
          "font-display mt-3 text-[30px] font-medium leading-snug text-[#0c0c0e]",
          isRerolling ? "motion-safe:animate-pulse opacity-50" : "",
        ].join(" ")}
        aria-label={isRerolling ? "Finding a fresh idea…" : undefined}
      >
        {isRerolling ? "Finding a fresh idea…" : nextItem.theme}
      </h2>

      {/* Short idea description */}
      {!isRerolling && nextItem.idea && (
        <p className="mt-2 line-clamp-2 text-[14px] leading-relaxed text-[#71717a]">
          {nextItem.idea.slice(0, 120)}
          {nextItem.idea.length > 120 ? "…" : ""}
        </p>
      )}

      {/* Shot chips — glanceable shot list */}
      {!isRerolling && showShots && (
        <div className="mt-4">
          <p className="mb-2 text-[11px] font-semibold uppercase tracking-[.14em] text-[#a1a1aa]">
            {shots.length} shot{shots.length !== 1 ? "s" : ""}
          </p>
          <ol className="flex flex-col border-t border-zinc-100">
            {shots.slice(0, 4).map((shot, i) => (
              <li
                key={shot.shot_id ?? i}
                className="flex items-baseline gap-3 border-b border-zinc-100 py-2"
              >
                <span className="font-display w-5 shrink-0 text-right text-[14px] font-medium italic text-lime-600">
                  {i + 1}
                </span>
                <span className="text-[13px] leading-snug text-[#3f3f46]">{shot.what}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {/* Fallback: filming_suggestion snippet when no filming_guide */}
      {!isRerolling && !showShots && nextItem.filming_suggestion && (
        <p className="mt-3 rounded-lg bg-lime-50 px-4 py-3 text-[13px] leading-relaxed text-[#3f3f46]">
          {nextItem.filming_suggestion.slice(0, 180)}
          {nextItem.filming_suggestion.length > 180 ? "…" : ""}
        </p>
      )}

      {/* Actions */}
      <div className="mt-6 flex flex-wrap items-center gap-4">
        <Link
          href={`/plan/items/${nextItem.id}`}
          className="inline-flex items-center justify-center rounded-full bg-[#0c0c0e] px-8 py-[13px] text-[14px] font-semibold text-white transition-opacity hover:opacity-80 focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
        >
          Film this →
        </Link>

        {canReroll && (
          <button
            onClick={() => void handleReroll()}
            disabled={rerolling}
            className="text-[13px] text-[#71717a] underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-[#0c0c0e] disabled:opacity-40"
          >
            Swap this idea
          </button>
        )}
      </div>

      {rerollError && (
        <p className="mt-3 text-[13px] text-[#71717a]">
          Couldn&apos;t find a fresh idea —{" "}
          <button onClick={() => void handleReroll()} className="underline underline-offset-4">
            try again
          </button>
        </p>
      )}
    </LightCard>
  );
}
