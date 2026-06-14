"use client";
import { useState, useEffect } from "react";
import type { ContentPlan, PersonaResponse, StyleResponse } from "@/lib/plan-api";
import { FONT_FACES } from "@/lib/font-faces";
import { calendarToday, behindBy } from "@/app/plan/_lib/plan-schedule";
import { TodayCard } from "./TodayCard";
import { MomentumCard } from "./MomentumCard";
import { PersonaCard } from "./PersonaCard";
import { StyleCard } from "./StyleCard";
import { IdeasCard } from "./IdeasCard";
import { ThisWeekStrip } from "./ThisWeekStrip";
import { MonthCalendarGrid } from "./MonthCalendarGrid";
import { PlanReadyBanner } from "./PlanReadyBanner";
import { FootagePool } from "./FootagePool";
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
  /** Called after the ready banner auto-dismisses so the parent can reset the flag */
  onBannerDismiss?: () => void;
  /** Creator Agent M1: user style — absent when USER_STYLE_ENABLED=false */
  styleResponse?: StyleResponse | null;
}

export function WorkspaceHome({
  plan,
  persona: personaProp,
  planJustReady,
  regenerating,
  onRefresh,
  onError,
  onBannerDismiss,
  styleResponse,
}: WorkspaceHomeProps) {
  // Local copy of persona so IdeasCard can optimistically update idea_seeds
  // without triggering a full plan refresh (saves avoid a round-trip to the parent).
  const [persona, setPersona] = useState<PersonaResponse>(personaProp);

  // When the parent refreshes (e.g. plan regeneration, poll cycle), propagate
  // changes from outside while keeping any in-flight local saves stable.
  // Keyed on the parent's generation_started_at so a new plan generation always
  // wins; idea_seeds changes from IdeasCard's own saves are the one authoritative
  // local mutation path and do NOT come through personaProp until the next poll.
  useEffect(
    () => {
      setPersona(personaProp);
    },
    // Intentional partial deps: re-sync only when the server signals a new
    // generation (generation_started_at / persona_status change). We do NOT
    // include personaProp itself to avoid clobbering in-flight idea_seeds
    // saves that haven't yet round-tripped through the parent's poll cycle.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [personaProp.generation_started_at, personaProp.persona_status],
  );
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
      {/* Inject @font-face declarations once so StyleCard (and any other font-previewing
          component in this subtree) can render fonts in their REAL typeface. The item page
          already does this; WorkspaceHome is a separate subtree so we inject it here too. */}
      <style dangerouslySetInnerHTML={{ __html: FONT_FACES }} />
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
            {/* M1: "Your ideas" — persistent idea seeds that survive across plans */}
            <IdeasCard
              persona={persona}
              onSaved={setPersona}
              planId={plan.id}
              onRegenerate={onRefresh}
            />
            <PersonaCard persona={persona} />
            {/* Creator Agent M1: StyleCard — absent when USER_STYLE_ENABLED=false */}
            {styleResponse && (
              <StyleCard
                style={styleResponse.style}
                status={styleResponse.status}
                styleSetPreview={styleResponse.style_set_preview}
                fontPreview={styleResponse.font_preview}
              />
            )}
          </div>

          {/* Right column */}
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

            {/* Footage pool — a power-up below the calendar; suppressed during
                activation (SeedUploadCard IS the pool at that moment). */}
            {!activating && (
              <FootagePool plan={plan} onRefresh={onRefresh} onError={onError} />
            )}

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
