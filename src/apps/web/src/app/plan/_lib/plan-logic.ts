/**
 * Pure plan-logic utilities extracted from PlanCalendar.tsx.
 * Re-exported here so workspace components can import without pulling in the
 * full PlanCalendar component tree.
 */

import type { PlanItem } from "@/lib/plan-api";

/** Pure progress summary for the momentum header. */
export function planProgress(items: PlanItem[]): { total: number; made: number; pct: number } {
  const total = items.length;
  const made = items.filter((i) => i.status === "ready").length;
  const pct = total > 0 ? Math.round((made / total) * 100) : 0;
  return { total, made, pct };
}

export interface PlanNudge {
  text: string;
  itemId?: string;
}

const weekOf = (dayIndex: number) => Math.floor((dayIndex - 1) / 7) + 1;

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
