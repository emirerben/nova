import { planNudge } from "@/app/plan/_components/PlanCalendar";
import type { PlanItem, PlanItemStatus } from "@/lib/plan-api";

function item(
  day: number,
  status: PlanItemStatus,
  clips: string[] = [],
): PlanItem {
  return {
    id: `i${day}`,
    day_index: day,
    theme: "t",
    idea: "i",
    filming_suggestion: null,
    clip_gcs_paths: clips,
    status,
    current_job_id: null,
    user_edited: false,
  };
}

describe("planNudge", () => {
  it("returns null for an empty plan", () => {
    expect(planNudge([])).toBeNull();
  });

  it("nudges to film the single remaining week-1 idea that needs clips", () => {
    // Day 1 done with clips, day 2 ready — only day 3 still needs clips.
    const n = planNudge([item(1, "ready", ["c.mp4"]), item(2, "ready"), item(3, "idea")]);
    expect(n).toEqual({ text: expect.stringContaining("Film day 3 next"), itemId: "i3" });
  });

  it("counts how many week-1 ideas still need clips", () => {
    const n = planNudge([item(1, "idea"), item(2, "idea"), item(3, "idea")]);
    expect(n?.text).toContain("3 week-1 ideas still need clips");
    expect(n?.itemId).toBe("i1");
  });

  it("nudges to generate a week-1 idea that already has clips", () => {
    const n = planNudge([item(1, "ready"), item(2, "idea", ["c.mp4"]), item(3, "idea")]);
    expect(n?.text).toContain("Day 2 has clips");
    expect(n?.itemId).toBe("i2");
  });

  it("surfaces a generating week-1 idea as rendering", () => {
    const n = planNudge([item(1, "generating", ["c.mp4"]), item(2, "idea")]);
    expect(n?.text).toContain("Day 1 is rendering now");
    expect(n?.itemId).toBe("i1");
  });

  it("gives the week-2 resume beat once week 1 is done", () => {
    const week1 = [1, 2, 3, 4, 5, 6, 7].map((d) => item(d, "ready"));
    const n = planNudge([...week1, item(8, "idea")]);
    expect(n?.text).toContain("Week 1 done");
    expect(n?.text).toContain("week 2");
    expect(n?.itemId).toBe("i8");
  });

  it("celebrates when every item is ready", () => {
    const n = planNudge([item(1, "ready"), item(2, "ready")]);
    expect(n?.text).toContain("all 2 videos");
    expect(n?.itemId).toBeUndefined();
  });
});
