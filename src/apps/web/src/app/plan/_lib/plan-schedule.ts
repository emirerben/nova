/**
 * Pure functions driving workspace calendar rendering.
 * All functions are side-effect free and dependency-free (no React).
 */

import type { PlanItem } from "@/lib/plan-api";

export type CellState =
  | "post"
  | "film"
  | "done"
  | "missed"
  | "rest"
  | "today-post"
  | "today-film";

/**
 * Returns which calendar day number (1-based, clamped 1..horizon_days)
 * corresponds to "today" given a plan's start_date.
 * start_date is a UTC date string "YYYY-MM-DD"; browser "today" may differ by ±1 day at boundaries.
 * Returns null when start_date is null (legacy plan — caller falls back to next-action day).
 */
export function calendarToday(
  startDate: string | null | undefined,
  horizonDays: number,
): number | null {
  if (!startDate) return null;
  const start = new Date(startDate + "T00:00:00Z");
  const now = new Date();
  const todayUtc = new Date(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()));
  const diffDays = Math.round((todayUtc.getTime() - start.getTime()) / (1000 * 60 * 60 * 24));
  const dayN = diffDays + 1; // day 1 = start_date
  return Math.max(1, Math.min(dayN, horizonDays));
}

/**
 * Returns the cell state for a given item (or null for a rest cell).
 * Items with status "ready" and day <= todayDay = "done" (past post).
 * Items with status "ready" and day > todayDay = "post" (ready to publish).
 * Items with status !== "ready" and day < todayDay = "missed".
 * Items with status !== "ready" and day === todayDay = "today-film".
 * Items with status !== "ready" and day > todayDay = "film".
 * The "today" ring (border-2 border-[#0c0c0e]) is applied ADDITIONALLY when day === todayDay.
 */
export function cellState(item: PlanItem, todayDay: number | null): CellState {
  const day = item.day_index;
  const isReady = item.status === "ready";
  if (isReady) {
    if (todayDay !== null && day < todayDay) return "done";
    if (todayDay !== null && day === todayDay) return "today-post";
    return "post"; // future ready = awaiting publish
  }
  // not ready
  if (todayDay !== null && day < todayDay) return "missed";
  if (todayDay !== null && day === todayDay) return "today-film";
  return "film";
}

/**
 * True when an item should stay solid during workspace:regenerating (D13).
 * Protected = user edited OR currently rendering.
 */
export function isProtected(item: PlanItem): boolean {
  return item.user_edited === true || item.current_job_id != null;
}

/**
 * Returns the 7 day_index values for the current plan-week containing todayDay.
 * When todayDay is null, returns days containing the next-action item (passed as nextActionDay).
 * Days outside 1..horizonDays get null (rest cell with no item possible).
 */
export function weekWindow(
  todayDay: number | null,
  horizonDays: number,
  nextActionDay?: number,
): Array<number | null> {
  const anchor = todayDay ?? nextActionDay ?? 1;
  // week is Mon-Sun aligned (0=Mon...6=Sun); day_index is 1-based
  const dayOfWeek = ((anchor - 1) % 7 + 7) % 7; // 0=Mon
  const weekStart = anchor - dayOfWeek; // day_index of Monday
  return Array.from({ length: 7 }, (_, i) => {
    const d = weekStart + i;
    return d >= 1 && d <= horizonDays ? d : null;
  });
}

/**
 * Number of days the user is "behind" — calendar-today is ahead of the next-action day.
 * Zero when caught up or no next-action.
 */
export function behindBy(todayDay: number | null, nextActionDay: number | null): number {
  if (todayDay === null || nextActionDay === null) return 0;
  return Math.max(0, todayDay - nextActionDay);
}
