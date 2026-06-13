import {
  calendarToday,
  cellState,
  isProtected,
  weekWindow,
  behindBy,
} from "@/app/plan/_lib/plan-schedule";
import type { PlanItem, PlanItemStatus } from "@/lib/plan-api";

function item(
  day: number,
  status: PlanItemStatus = "idea",
  opts: { user_edited?: boolean; current_job_id?: string | null } = {},
): PlanItem {
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
    current_job_id: opts.current_job_id ?? null,
    user_edited: opts.user_edited ?? false,
  };
}

describe("calendarToday", () => {
  // NOTE: calendarToday derives "today" from the browser's LOCAL date components
  // (see plan-schedule.ts), so exact-value tests must build start_date from local
  // getters too. Using getUTC* here made these tests fail in the window between
  // local midnight and UTC midnight (e.g. 00:00-02:00 CEST).
  it("returns day 1 when start_date is today", () => {
    const now = new Date();
    const startDate = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
    expect(calendarToday(startDate, 30)).toBe(1);
  });

  it("returns day 6 when start_date is 5 days ago", () => {
    const now = new Date();
    const fiveDaysAgo = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 5);
    const startDate = `${fiveDaysAgo.getFullYear()}-${String(fiveDaysAgo.getMonth() + 1).padStart(2, "0")}-${String(fiveDaysAgo.getDate()).padStart(2, "0")}`;
    expect(calendarToday(startDate, 30)).toBe(6);
  });

  it("clamps to horizonDays when start_date is far in the past", () => {
    // 100 days ago, horizon 30 → clamps to 30
    const now = new Date();
    const farPast = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - 100));
    const startDate = `${farPast.getUTCFullYear()}-${String(farPast.getUTCMonth() + 1).padStart(2, "0")}-${String(farPast.getUTCDate()).padStart(2, "0")}`;
    expect(calendarToday(startDate, 30)).toBe(30);
  });

  it("clamps to 1 when start_date is in the future", () => {
    const now = new Date();
    const future = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 5));
    const startDate = `${future.getUTCFullYear()}-${String(future.getUTCMonth() + 1).padStart(2, "0")}-${String(future.getUTCDate()).padStart(2, "0")}`;
    expect(calendarToday(startDate, 30)).toBe(1);
  });

  it("returns null when start_date is null", () => {
    expect(calendarToday(null, 30)).toBeNull();
  });

  it("returns null when start_date is undefined", () => {
    expect(calendarToday(undefined, 30)).toBeNull();
  });
});

describe("cellState", () => {
  // Ready item states
  it("returns done for ready item with day < todayDay", () => {
    expect(cellState(item(1, "ready"), 5)).toBe("done");
  });

  it("returns today-post for ready item with day === todayDay", () => {
    expect(cellState(item(5, "ready"), 5)).toBe("today-post");
  });

  it("returns post for ready item with day > todayDay", () => {
    expect(cellState(item(10, "ready"), 5)).toBe("post");
  });

  it("returns post for ready item when todayDay is null", () => {
    expect(cellState(item(10, "ready"), null)).toBe("post");
  });

  // Not-ready item states
  it("returns missed for non-ready item with day < todayDay", () => {
    expect(cellState(item(1, "idea"), 5)).toBe("missed");
  });

  it("returns today-film for non-ready item with day === todayDay", () => {
    expect(cellState(item(5, "idea"), 5)).toBe("today-film");
  });

  it("returns film for non-ready item with day > todayDay", () => {
    expect(cellState(item(10, "idea"), 5)).toBe("film");
  });

  it("returns film for non-ready item when todayDay is null", () => {
    expect(cellState(item(10, "idea"), null)).toBe("film");
  });

  it("returns missed for generating item with day < todayDay", () => {
    expect(cellState(item(1, "generating"), 5)).toBe("missed");
  });

  it("returns today-film for awaiting_clips item on today", () => {
    expect(cellState(item(3, "awaiting_clips"), 3)).toBe("today-film");
  });
});

describe("isProtected", () => {
  it("returns false for unprotected item", () => {
    expect(isProtected(item(1))).toBe(false);
  });

  it("returns true when user_edited is true", () => {
    expect(isProtected(item(1, "idea", { user_edited: true }))).toBe(true);
  });

  it("returns true when current_job_id is set", () => {
    expect(isProtected(item(1, "generating", { current_job_id: "job-123" }))).toBe(true);
  });

  it("returns false when current_job_id is null and user_edited is false", () => {
    expect(isProtected(item(1, "ready", { current_job_id: null, user_edited: false }))).toBe(false);
  });
});

describe("weekWindow", () => {
  it("returns 7 items", () => {
    expect(weekWindow(1, 30)).toHaveLength(7);
  });

  it("returns week starting from day 1 (Monday anchor) when todayDay=1", () => {
    // day 1 is day_index 1, weekStart = 1 - 0 = 1 → [1,2,3,4,5,6,7]
    expect(weekWindow(1, 30)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  });

  it("returns correct week for day 7 (Sunday = index 6 in Mon-based week)", () => {
    // day 7: (7-1) % 7 = 6 (Sunday), weekStart = 7-6 = 1 → [1,2,3,4,5,6,7]
    expect(weekWindow(7, 30)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  });

  it("returns correct week for day 8 (Monday again)", () => {
    // day 8: (8-1) % 7 = 0 (Monday), weekStart = 8 → [8..14]
    expect(weekWindow(8, 30)).toEqual([8, 9, 10, 11, 12, 13, 14]);
  });

  it("returns null for days outside 1..horizonDays", () => {
    // day 1, horizon=5 → week [1,2,3,4,5,null,null]
    const result = weekWindow(1, 5);
    expect(result).toEqual([1, 2, 3, 4, 5, null, null]);
  });

  it("falls back to nextActionDay when todayDay is null", () => {
    // nextActionDay=8 → same as weekWindow(8,30)
    const result = weekWindow(null, 30, 8);
    expect(result).toEqual([8, 9, 10, 11, 12, 13, 14]);
  });

  it("falls back to day 1 when both todayDay and nextActionDay are null/undefined", () => {
    expect(weekWindow(null, 30, undefined)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  });

  it("handles sparse day_index in a 30-day horizon with posts_per_week=3 (days 1,3,8,15,22)", () => {
    // Just checking the week that contains day 15 → week anchor 15
    // (15-1) % 7 = 0 (Monday), weekStart = 15 → [15..21]
    const result = weekWindow(15, 30);
    expect(result).toEqual([15, 16, 17, 18, 19, 20, 21]);
  });
});

describe("behindBy", () => {
  it("returns 0 when caught up (todayDay equals nextActionDay)", () => {
    expect(behindBy(5, 5)).toBe(0);
  });

  it("returns positive when behind", () => {
    expect(behindBy(10, 5)).toBe(5);
  });

  it("returns 0 when todayDay is null", () => {
    expect(behindBy(null, 5)).toBe(0);
  });

  it("returns 0 when nextActionDay is null", () => {
    expect(behindBy(5, null)).toBe(0);
  });

  it("returns 0 when todayDay is less than nextActionDay (ahead)", () => {
    expect(behindBy(3, 10)).toBe(0);
  });

  it("returns 0 when both are null", () => {
    expect(behindBy(null, null)).toBe(0);
  });
});
