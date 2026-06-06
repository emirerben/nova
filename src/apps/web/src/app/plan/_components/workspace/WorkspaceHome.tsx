"use client";
import type { ContentPlan, PersonaResponse } from "@/lib/plan-api";
import { calendarToday, behindBy } from "@/app/plan/_lib/plan-schedule";
import { TodayCard } from "./TodayCard";
import { MomentumCard } from "./MomentumCard";
import { PersonaCard } from "./PersonaCard";
import { ThisWeekStrip } from "./ThisWeekStrip";
import { MonthCalendarGrid } from "./MonthCalendarGrid";
import { PlanReadyBanner } from "./PlanReadyBanner";
import SeedUploadCard from "../SeedUploadCard";
import SteerInput from "../SteerInput";

interface WorkspaceHomeProps {
  plan: ContentPlan;
  persona: PersonaResponse;
  /** True when this is an in-session generating→ready flip (banner should show) */
  planJustReady: boolean;
  regenerating: boolean;
  onRefresh: () => void;
  onError: (msg: string) => void;
}

export function WorkspaceHome({
  plan,
  persona,
  planJustReady,
  regenerating,
  onRefresh,
  onError,
}: WorkspaceHomeProps) {
  const items = plan.items ?? [];
  const todayDay = calendarToday(plan.start_date, plan.horizon_days);

  // Weekday label from start_date
  let weekdayLabel: string | null = null;
  if (plan.start_date && todayDay !== null) {
    const start = new Date(plan.start_date + "T00:00:00Z");
    start.setUTCDate(start.getUTCDate() + todayDay - 1);
    weekdayLabel = start.toLocaleDateString("en-US", {
      weekday: "long",
      timeZone: "UTC",
    });
  }

  // Find the first non-ready item for TodayCard and behindBy
  const nextActionItem = items.find((i) => i.status !== "ready") ?? null;
  const behind = behindBy(todayDay, nextActionItem?.day_index ?? null);

  const activating = ["seeding", "activating"].includes(plan.activation_status ?? "");

  return (
    <div className="min-h-screen bg-[#fafaf8]">
      <div className="mx-auto max-w-[1180px] px-6 pb-24 pt-8">
        <div className="flex flex-col gap-6 lg:flex-row lg:items-start lg:gap-8">
          {/* Left rail */}
          <div className="flex flex-col gap-4 lg:w-[360px] lg:shrink-0">
            <TodayCard
              nextItem={nextActionItem}
              plan={plan}
              persona={persona}
              horizonDays={plan.horizon_days}
              calendarDay={todayDay}
              weekdayLabel={weekdayLabel}
              behind={behind}
              onRefresh={onRefresh}
            />
            <MomentumCard plan={plan} />
            <PersonaCard persona={persona} />
          </div>

          {/* Right column */}
          <div className="flex flex-1 flex-col gap-6">
            {/* Plan-ready one-time banner */}
            <PlanReadyBanner horizonDays={plan.horizon_days} show={planJustReady} />

            {/* Activation card — conditional */}
            {activating && (
              <SeedUploadCard plan={plan} onError={onError} onRefresh={onRefresh} />
            )}

            <ThisWeekStrip
              plan={plan}
              todayDay={todayDay}
              regenerating={regenerating}
            />
            <MonthCalendarGrid
              plan={plan}
              todayDay={todayDay}
              regenerating={regenerating}
            />

            {/* SteerInput — recessive, below the grid */}
            <div className="mt-2">
              {regenerating && (
                <p className="mb-3 flex items-center gap-2 text-[13px] text-[#71717a]">
                  <span className="relative flex h-2 w-2">
                    <span className="motion-safe:animate-ping absolute inline-flex h-full w-full rounded-full bg-lime-600 opacity-60" />
                    <span className="relative inline-flex h-2 w-2 rounded-full bg-lime-600" />
                  </span>
                  Refreshing your plan…
                </p>
              )}
              {plan.plan_status === "failed" && items.length > 0 && (
                <p className="mb-3 text-[13px] text-[#71717a]">
                  Couldn&apos;t refresh your plan —{" "}
                  <button
                    onClick={onRefresh}
                    className="underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
                  >
                    try again
                  </button>
                </p>
              )}
              <SteerInput contentPlanId={plan.id} />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
