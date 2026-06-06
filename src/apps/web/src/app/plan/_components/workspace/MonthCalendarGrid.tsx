"use client";
import Link from "next/link";
import { Eyebrow } from "../ui/Eyebrow";
import { LightCard } from "../ui/LightCard";
import { cellState, isProtected } from "@/app/plan/_lib/plan-schedule";
import type { ContentPlan } from "@/lib/plan-api";

interface MonthCalendarGridProps {
  plan: ContentPlan;
  todayDay: number | null;
  regenerating?: boolean;
}

export function MonthCalendarGrid({
  plan,
  todayDay,
  regenerating = false,
}: MonthCalendarGridProps) {
  const items = plan.items ?? [];
  const horizon = plan.horizon_days;
  const days = Array.from({ length: horizon }, (_, i) => i + 1);

  if (items.length === 0 && plan.plan_status === "ready") {
    return (
      <div className="rounded-2xl border border-zinc-200 bg-white px-6 py-10 text-center shadow-sm">
        <p className="text-[14px] text-[#71717a]">
          Your plan came back empty — steer it and regenerate.
        </p>
      </div>
    );
  }

  return (
    <div>
      {/* Legend */}
      <div className="mb-3 flex items-baseline justify-between">
        <Eyebrow tone="muted">Your {horizon} days</Eyebrow>
        <div className="flex gap-4 text-[11px] text-[#a1a1aa]">
          <span>
            <span className="mr-1 inline-block h-2 w-2 rounded-sm bg-lime-700" />
            post
          </span>
          <span>
            <span className="mr-1 inline-block h-2 w-2 rounded-sm border border-lime-300 bg-lime-50" />
            film
          </span>
          <span>
            <span className="mr-1 inline-block h-2 w-2 rounded-sm bg-zinc-200" />
            done
          </span>
        </div>
      </div>
      <LightCard className="p-4">
        <div className="grid grid-cols-7 gap-[5px]">
          {days.map((dayN) => {
            const item = items.find((it) => it.day_index === dayN) ?? null;
            const state = item ? cellState(item, todayDay) : "rest";
            const isToday = todayDay !== null && dayN === todayDay;
            const protected_ = item ? isProtected(item) : false;
            const shouldShimmer = regenerating && !protected_ && item !== null;

            const cellClasses = (() => {
              const base =
                "flex h-12 flex-col justify-between rounded-[6px] p-1.5 transition-opacity";
              const shimmer = shouldShimmer ? "motion-safe:animate-pulse opacity-40" : "";
              const todayRing = isToday ? "ring-2 ring-[#0c0c0e]" : "";
              if (!item)
                return `${base} border border-dashed border-zinc-100 ${shimmer} ${todayRing}`;
              if (state === "done") return `${base} bg-zinc-100 ${shimmer} ${todayRing}`;
              if (state === "post" || state === "today-post")
                return `${base} bg-lime-700 ${shimmer} ${todayRing}`;
              if (state === "film" || state === "today-film")
                return `${base} border border-lime-300 bg-lime-50 ${shimmer} ${todayRing}`;
              if (state === "missed")
                return `${base} border border-zinc-100 bg-white ${shimmer} ${todayRing}`;
              return `${base} border border-zinc-100 bg-white ${shimmer} ${todayRing}`;
            })();

            const ariaLabel = item
              ? `Day ${dayN} — ${item.theme}, ${state}`
              : `Day ${dayN} — rest`;

            const inner = (
              <div className={cellClasses} aria-label={ariaLabel}>
                <span
                  className={`text-[10px] ${
                    state === "post" || state === "today-post"
                      ? "text-white"
                      : state === "film" || state === "today-film"
                        ? "text-lime-800"
                        : "text-[#a1a1aa]"
                  }`}
                >
                  {dayN}
                </span>
                {item && state !== "rest" && (
                  <span
                    className={`truncate text-[9px] leading-none ${
                      state === "post" || state === "today-post"
                        ? "text-white"
                        : state === "done"
                          ? "text-zinc-400"
                          : state === "film" || state === "today-film"
                            ? "text-lime-800"
                            : "text-[#a1a1aa]"
                    }`}
                  >
                    {item.theme?.slice(0, 8)}
                  </span>
                )}
                {state === "done" && (
                  <span className="text-[9px] text-zinc-400">✓</span>
                )}
                {state === "missed" && (
                  <span className="h-1 w-1 rounded-full bg-zinc-300" />
                )}
              </div>
            );

            return item ? (
              <Link
                key={dayN}
                href={`/plan/items/${item.id}`}
                className="focus-visible:outline-2 focus-visible:outline-[#0c0c0e] focus-visible:rounded-[6px]"
                aria-label={ariaLabel}
              >
                {inner}
              </Link>
            ) : (
              <div key={dayN}>{inner}</div>
            );
          })}
          {/* Padding cells to complete the last row */}
          {Array.from({ length: (7 - (horizon % 7)) % 7 }, (_, i) => (
            <div key={`pad-${i}`} className="h-12" />
          ))}
        </div>
      </LightCard>
    </div>
  );
}
