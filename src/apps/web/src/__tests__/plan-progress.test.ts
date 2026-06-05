import { planProgress } from "@/app/plan/_components/PlanCalendar";
import type { PlanItem, PlanItemStatus } from "@/lib/plan-api";

function item(day: number, status: PlanItemStatus): PlanItem {
  return {
    id: `i${day}`,
    day_index: day,
    theme: "t",
    idea: "i",
    filming_suggestion: null,
    rationale: null,
    filming_guide: [],
    clip_gcs_paths: [],
    status,
    current_job_id: null,
    user_edited: false,
  };
}

describe("planProgress", () => {
  it("returns zeroed progress for an empty plan (no divide-by-zero)", () => {
    expect(planProgress([])).toEqual({ total: 0, made: 0, pct: 0 });
  });

  it("counts only 'ready' items as made", () => {
    const items = [
      item(1, "ready"),
      item(2, "generating"),
      item(3, "idea"),
      item(4, "ready"),
      item(5, "failed"),
    ];
    expect(planProgress(items)).toEqual({ total: 5, made: 2, pct: 40 });
  });

  it("rounds the percentage", () => {
    const items = [item(1, "ready"), item(2, "idea"), item(3, "idea")];
    // 1/3 = 33.33% -> 33
    expect(planProgress(items).pct).toBe(33);
  });

  it("reports 100% when every item is ready", () => {
    const items = [item(1, "ready"), item(2, "ready")];
    expect(planProgress(items)).toEqual({ total: 2, made: 2, pct: 100 });
  });
});
