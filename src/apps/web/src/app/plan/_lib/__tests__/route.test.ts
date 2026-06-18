/**
 * Tests for resolvePlanMode (T16 — idea-centric plan redesign).
 *
 * Key assertion: plan_status="generating" always returns "workspace:regenerating"
 * regardless of item count (no more "setup:plan-generating" on generating plans
 * with items — the idea-centric flow always has items before generating).
 */

import { resolvePlanMode } from "../route";
import type { ContentPlan, PersonaResponse } from "@/lib/plan-api";

function makePersona(overrides: Partial<PersonaResponse> = {}): PersonaResponse {
  return {
    id: "p1",
    persona_status: "ready",
    persona: { summary: "creator", content_pillars: [], tone: "", audience: "", posting_cadence: "", sample_topics: [] },
    questionnaire: { content_mode: "create_new" },
    prompt_version: "1",
    generation_started_at: null,
    tiktok_profile: null,
    style: null,
    idea_seeds: [],
    ...overrides,
  } as unknown as PersonaResponse;
}

function makePlan(overrides: Partial<ContentPlan> = {}): ContentPlan {
  return {
    id: "plan1",
    plan_status: "ready",
    horizon_days: 30,
    events: null,
    items: [],
    activation_status: "none",
    seed_clip_count: 0,
    generation_started_at: null,
    start_date: null,
    pool_status: "none",
    pool_clip_count: 0,
    pool_matched_count: 0,
    idea_seeds: [],
    ...overrides,
  } as unknown as ContentPlan;
}

describe("resolvePlanMode — idea-centric (T16)", () => {
  it("returns workspace:regenerating when plan is generating with items", () => {
    const persona = makePersona();
    const plan = makePlan({
      plan_status: "generating",
      items: [{ id: "i1" } as never],
    });
    expect(resolvePlanMode(persona, plan)).toBe("workspace:regenerating");
  });

  it("returns workspace:regenerating even when plan is generating with no items", () => {
    // Idea-centric: there is no more 'setup:plan-generating' for a generating plan.
    const persona = makePersona();
    const plan = makePlan({ plan_status: "generating", items: [] });
    expect(resolvePlanMode(persona, plan)).toBe("workspace:regenerating");
  });

  it("returns workspace when plan is ready with items", () => {
    const persona = makePersona();
    const plan = makePlan({
      plan_status: "ready",
      items: [{ id: "i1" } as never, { id: "i2" } as never],
    });
    expect(resolvePlanMode(persona, plan)).toBe("workspace");
  });

  it("returns workspace when plan is ready with no items", () => {
    const persona = makePersona();
    const plan = makePlan({ plan_status: "ready", items: [] });
    expect(resolvePlanMode(persona, plan)).toBe("workspace");
  });

  it("returns setup:plan-intro when persona is ready but no plan exists", () => {
    const persona = makePersona();
    expect(resolvePlanMode(persona, null)).toBe("setup:plan-intro");
  });

  it("returns setup:prescreen when no persona", () => {
    expect(resolvePlanMode(null, null)).toBe("setup:prescreen");
  });

  it("returns workspace:refresh-failed when plan failed with items", () => {
    const persona = makePersona();
    const plan = makePlan({
      plan_status: "failed",
      items: [{ id: "i1" } as never],
    });
    expect(resolvePlanMode(persona, plan)).toBe("workspace:refresh-failed");
  });

  it("returns setup:plan-failed when plan failed with no items", () => {
    const persona = makePersona();
    const plan = makePlan({ plan_status: "failed", items: [] });
    expect(resolvePlanMode(persona, plan)).toBe("setup:plan-failed");
  });
});
