"use client";
import { useEffect, useRef } from "react";
import Link from "next/link";
import { Eyebrow } from "../ui/Eyebrow";
import { cellState, weekWindow, isProtected } from "@/app/plan/_lib/plan-schedule";
import type { ContentPlan } from "@/lib/plan-api";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

interface ThisWeekStripProps {
  plan: ContentPlan;
  todayDay: number | null;
  /** Regenerating mode — protected cells stay solid, others shimmer */
  regenerating?: boolean;
}

export function ThisWeekStrip({
  plan,
  todayDay,
  regenerating = false,
}: ThisWeekStripProps) {
  const items = plan.items ?? [];
  const nextActionDay = items.find((i) => i.status !== "ready")?.day_index ?? undefined;
  const week = weekWindow(todayDay, plan.horizon_days, nextActionDay);
  const scrollRef = useRef<HTMLDivElement>(null);

  // D17: auto-center today on mount
  useEffect(() => {
    if (!scrollRef.current) return;
    const todayIdx = week.indexOf(todayDay);
    if (todayIdx < 0) return;
    const card = scrollRef.current.children[todayIdx] as HTMLElement;
    if (card) card.scrollIntoView({ behavior: "instant", block: "nearest", inline: "center" });
  }, [todayDay]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div>
      <Eyebrow tone="muted" className="mb-3">
        This week
      </Eyebrow>
      {/* Desktop: 7-col grid. Mobile: horizontal scroll-snap */}
      <div
        ref={scrollRef}
        className="grid grid-cols-7 gap-2 overflow-x-auto snap-x snap-mandatory lg:overflow-visible"
      >
        {week.map((dayN, i) => {
          if (dayN === null) {
            // out-of-range rest cell
            return (
              <div
                key={`rest-out-${i}`}
                className="min-w-[120px] snap-start rounded-xl border border-dashed border-zinc-200 p-3 lg:min-w-0"
              >
                <p className="text-[10px] uppercase tracking-wide text-[#a1a1aa]">
                  {WEEKDAYS[i]}
                </p>
                <p className="mt-1 text-[12px] text-[#a1a1aa]">—</p>
              </div>
            );
          }

          const item = items.find((it) => it.day_index === dayN) ?? null;
          const state = item ? cellState(item, todayDay) : "rest";
          const isToday = todayDay !== null && dayN === todayDay;
          const protected_ = item ? isProtected(item) : false;
          const shouldShimmer = regenerating && !protected_ && item !== null;

          const borderClass = isToday
            ? "border-2 border-lime-600"
            : state === "rest"
              ? "border border-dashed border-zinc-200"
              : "border border-zinc-200";

          const bgClass =
            state === "done"
              ? "bg-zinc-100"
              : state === "post" || state === "today-post"
                ? "bg-white"
                : state === "film" || state === "today-film"
                  ? "bg-lime-50"
                  : "bg-white";

          const content = (() => {
            if (!item)
              return <p className="mt-1 text-[12px] text-[#a1a1aa]">rest</p>;
            if (state === "done") {
              return (
                <>
                  <p className="mt-1 line-clamp-1 text-[12px] text-[#71717a] line-through decoration-zinc-300">
                    {item.theme}
                  </p>
                  <p className="mt-1 text-[11px] text-lime-700">✓ made</p>
                </>
              );
            }
            if (state === "post" || state === "today-post") {
              return (
                <>
                  <p className="mt-1 line-clamp-1 text-[12px] font-medium text-[#0c0c0e]">
                    {item.theme}
                  </p>
                  <span className="mt-1 inline-block rounded-full bg-lime-700 px-2 py-0.5 text-[10px] font-medium text-white">
                    post
                  </span>
                </>
              );
            }
            return (
              <>
                <p
                  className={`mt-1 line-clamp-1 text-[12px] ${isToday ? "font-medium text-[#0c0c0e]" : "text-[#71717a]"}`}
                >
                  {item.theme}
                </p>
                <p className="mt-1 text-[11px] text-[#a1a1aa]">
                  {isToday ? "film today" : "film"}
                  {(item.filming_guide?.length ?? 0) > 0 && (
                    <span className="ml-1 text-[10px] text-zinc-400">
                      · {item.filming_guide.length} shots
                    </span>
                  )}
                </p>
              </>
            );
          })();

          const cell = (
            <div
              className={`min-w-[120px] snap-start rounded-xl ${borderClass} ${bgClass} p-3 lg:min-w-0 ${shouldShimmer ? "motion-safe:animate-pulse opacity-50" : ""}`}
              aria-label={
                item
                  ? `Day ${dayN} — ${item.theme}, ${state}`
                  : `Day ${dayN} — rest`
              }
            >
              <p
                className={`text-[10px] uppercase tracking-wide ${isToday ? "font-semibold text-lime-700" : "text-[#a1a1aa]"}`}
              >
                {WEEKDAYS[i]}
              </p>
              {content}
            </div>
          );

          return item ? (
            <Link
              key={dayN}
              href={`/plan/items/${item.id}`}
              className="block focus-visible:outline-2 focus-visible:outline-[#0c0c0e] focus-visible:rounded-xl"
            >
              {cell}
            </Link>
          ) : (
            <div key={`rest-${dayN ?? i}`}>{cell}</div>
          );
        })}
      </div>
    </div>
  );
}
