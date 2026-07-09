"use client";
import type { ContentPlan } from "@/lib/plan-api";
import { calendarToday, behindBy } from "@/app/plan/_lib/plan-schedule";
import { HomeTodayCard } from "./HomeTodayCard";
import { IdeasSidebar } from "./IdeasSidebar";
import { MomentumCard } from "./MomentumCard";
import { ThisWeekStrip } from "./ThisWeekStrip";
import { MonthCalendarGrid } from "./MonthCalendarGrid";
import { PlanReadyBanner } from "./PlanReadyBanner";
import { FootagePool } from "./FootagePool";
import SeedUploadCard from "../SeedUploadCard";
import SteerInput from "../SteerInput";

interface WorkspaceHomeProps {
  plan: ContentPlan;
  /** True when this is an in-session generating→ready flip (banner should show) */
  planJustReady: boolean;
  regenerating: boolean;
  onRefresh: () => void;
  onError: (msg: string) => void;
  /** Called after the ready banner auto-dismisses so the parent can reset the flag */
  onBannerDismiss?: () => void;
}

export function WorkspaceHome({
  plan,
  planJustReady,
  regenerating,
  onRefresh,
  onError,
  onBannerDismiss,
}: WorkspaceHomeProps) {
  const items = plan.items ?? [];
  // Items that have been scheduled (expanded ideas with a calendar slot).
  const scheduledItems = items.filter((i) => i.day_index !== null);
  const hasSchedule = scheduledItems.length > 0;
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

  // Find the first non-ready scheduled item for HomeTodayCard and behindBy
  const nextActionItem = scheduledItems.find((i) => i.status !== "ready") ?? null;
  const behind = behindBy(todayDay, nextActionItem?.day_index ?? null);

  const activating = ["seeding", "activating"].includes(plan.activation_status ?? "");

  return (
    <div className="min-h-screen bg-[#fafaf8]">
      <div className="mx-auto max-w-[1280px] px-6 pb-24 pt-8">
        <div className="flex flex-col gap-6 lg:flex-row lg:items-start lg:gap-8">

          {/* ── Left rail: "Your ideas" sidebar ───────────────────────────── */}
          {/* Mobile: stacks above Today card
              Desktop: sticky left rail w-96 */}
          <div className="w-full lg:w-96 lg:shrink-0 lg:sticky lg:top-8">
            <IdeasSidebar
              plan={plan}
              onRefresh={onRefresh}
            />
          </div>

          {/* ── Center / main column ──────────────────────────────────────── */}
          <div className="flex flex-1 flex-col gap-6">
            {/* Plan-ready one-time banner */}
            <PlanReadyBanner
              horizonDays={plan.horizon_days}
              show={planJustReady}
              onDismiss={onBannerDismiss}
            />

            {/* Activation card — conditional */}
            {activating && (
              <SeedUploadCard plan={plan} onError={onError} onRefresh={onRefresh} />
            )}

            {hasSchedule ? (
              <>
                {/* A. Today card — dominant, Fraunces heading + shot chips + one CTA */}
                <HomeTodayCard
                  nextItem={nextActionItem}
                  plan={plan}
                  horizonDays={plan.horizon_days}
                  calendarDay={todayDay}
                  weekdayLabel={weekdayLabel}
                  behind={behind}
                  onRefresh={onRefresh}
                />

                {/* B. "This week" strip */}
                <ThisWeekStrip
                  plan={plan}
                  todayDay={todayDay}
                  regenerating={regenerating}
                />

                {/* C. Month calendar grid */}
                <MonthCalendarGrid
                  plan={plan}
                  todayDay={todayDay}
                  regenerating={regenerating}
                />

                {/* Footage pool — power-up below calendar; suppressed during activation */}
                {!activating && (
                  <FootagePool plan={plan} onRefresh={onRefresh} onError={onError} />
                )}
              </>
            ) : (
              /* No scheduled items yet — ideas are in the sidebar, waiting to be expanded */
              <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-zinc-200 px-8 py-16 text-center">
                <p className="font-display text-2xl font-medium text-[#0c0c0e]">
                  Your ideas are ready
                </p>
                <p className="mt-3 max-w-sm text-[14px] text-[#71717a]">
                  Click an idea on the left to add notes or expand it with AI.
                  Hit &ldquo;Generate with AI&rdquo; to turn all your ideas into a full filming plan.
                </p>
              </div>
            )}

            {/* Secondary cards — recessive, below the primary flow */}
            <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
              <div className="flex-1">
                <MomentumCard plan={plan} />
              </div>
            </div>

            {/* SteerInput — recessive, below everything */}
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
