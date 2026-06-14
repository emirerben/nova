"use client";
import { useState } from "react";
import Link from "next/link";
import { LightCard } from "../ui/LightCard";
import { Eyebrow } from "../ui/Eyebrow";
import { InkButton } from "../ui/InkButton";
import { rerollPlanItem, createContentPlan } from "@/lib/plan-api";
import type { PlanItem, PersonaResponse, ContentPlan } from "@/lib/plan-api";
import { SeedProvenanceBadge } from "../ui/SeedProvenanceBadge";

interface TodayCardProps {
  /** The next-action item (first non-ready item) — null means all done */
  nextItem: PlanItem | null;
  plan: ContentPlan;
  persona: PersonaResponse;
  horizonDays: number;
  /** Calendar day number (1-based) */
  calendarDay: number | null;
  /** Day of week label e.g. "Wednesday" — only when start_date present */
  weekdayLabel: string | null;
  /** Days behind (0 = caught up) */
  behind: number;
  /** Called after reroll resolves so parent can refresh */
  onRefresh: () => void;
}

export function TodayCard({
  nextItem,
  plan,
  horizonDays,
  calendarDay,
  weekdayLabel,
  behind,
  onRefresh,
}: TodayCardProps) {
  const [rerolling, setRerolling] = useState(false);
  const [rerollError, setRerollError] = useState(false);
  const [showNewPlanForm, setShowNewPlanForm] = useState(false);
  const [events, setEvents] = useState("");
  const [generating, setGenerating] = useState(false);

  // All items done
  const allDone =
    nextItem === null &&
    (plan.items ?? []).length > 0 &&
    (plan.items ?? []).every((i) => i.status === "ready");

  async function handleReroll() {
    if (!nextItem || rerolling) return;
    setRerolling(true);
    setRerollError(false);
    try {
      await rerollPlanItem(nextItem.id);
      // Poll will pick up the status change — just trigger a refresh
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

  const dayLabel =
    calendarDay !== null
      ? `Day ${calendarDay} of ${horizonDays}${weekdayLabel ? ` · ${weekdayLabel}` : ""}`
      : `Day of ${horizonDays}`;

  if (allDone) {
    return (
      <LightCard className="px-6 py-5">
        <Eyebrow tone="lime">{dayLabel}</Eyebrow>
        <h2 className="font-display mt-3 text-[28px] font-medium leading-snug text-[#0c0c0e]">
          All {(plan.items ?? []).length} made.
        </h2>
        {!showNewPlanForm ? (
          <InkButton className="mt-6" onClick={() => setShowNewPlanForm(true)}>
            Plan your next {horizonDays} days
          </InkButton>
        ) : (
          <div className="mt-6 space-y-3">
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

  if (!nextItem) return null;

  const isRerolling = nextItem.status === "rerolling";
  const canReroll = nextItem.status === "idea" && !nextItem.current_job_id;

  return (
    <LightCard className="px-6 py-5">
      <Eyebrow tone="lime">{dayLabel}</Eyebrow>

      {behind > 0 && (
        <p className="mt-2 text-[13px] text-[#71717a]">
          You&apos;re {behind} {behind === 1 ? "day" : "days"} behind — this is the lightest way
          to catch up.
        </p>
      )}

      {/* Headline — shimmer while rerolling */}
      <h2
        className={`font-display mt-3 text-[28px] font-medium leading-snug text-[#0c0c0e] ${isRerolling ? "motion-safe:animate-pulse opacity-50" : ""}`}
        aria-label={isRerolling ? "Finding a fresh idea…" : undefined}
      >
        {isRerolling ? "Finding a fresh idea…" : nextItem.theme}
      </h2>

      {/* Idea — shimmer while rerolling */}
      {!isRerolling && nextItem.idea && (
        <p className="mt-2 line-clamp-2 text-[14px] leading-relaxed text-[#71717a]">
          {nextItem.idea.slice(0, 110)}
          {nextItem.idea.length > 110 ? "…" : ""}
        </p>
      )}

      {!isRerolling && <SeedProvenanceBadge item={nextItem} />}

      <div className="mt-5 flex flex-wrap items-center gap-4">
        <Link
          href={`/plan/items/${nextItem.id}`}
          className="inline-flex items-center justify-center rounded-full bg-[#0c0c0e] px-8 py-[13px] text-[14px] font-semibold text-white transition-opacity hover:opacity-80 focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
        >
          See how to film it
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
          <button
            onClick={() => void handleReroll()}
            className="underline underline-offset-4"
          >
            try again
          </button>
        </p>
      )}
    </LightCard>
  );
}
