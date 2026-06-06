import { resolvePlanMode } from "@/app/plan/_lib/route";
import type { ContentPlan, PersonaResponse, PlanItem } from "@/lib/plan-api";

function persona(
  status: PersonaResponse["persona_status"],
  opts: { hasPersona?: boolean } = {},
): PersonaResponse {
  return {
    id: "p1",
    persona_status: status,
    questionnaire: null,
    persona: opts.hasPersona !== false && status !== "chat_pending" && status !== "generating"
      ? { summary: "s", content_pillars: [], tone: "", audience: "", posting_cadence: "", sample_topics: [] }
      : null,
    error_detail: null,
  };
}

function plan(
  status: ContentPlan["plan_status"],
  items: PlanItem[] = [],
): ContentPlan {
  return {
    id: "pl1",
    plan_status: status,
    horizon_days: 30,
    events: null,
    items,
    activation_status: "none",
    seed_clip_count: 0,
  };
}

function item(day: number): PlanItem {
  return {
    id: `i${day}`,
    day_index: day,
    theme: "t",
    idea: "i",
    filming_suggestion: null,
    rationale: null,
    filming_guide: [],
    clip_gcs_paths: [],
    status: "idea",
    current_job_id: null,
    user_edited: false,
  };
}

describe("resolvePlanMode", () => {
  // ── No persona ──────────────────────────────────────────────────────────
  it("returns setup:prescreen when persona is null", () => {
    expect(resolvePlanMode(null, null)).toBe("setup:prescreen");
  });

  it("returns setup:prescreen when persona is undefined", () => {
    expect(resolvePlanMode(undefined, undefined)).toBe("setup:prescreen");
  });

  // ── Persona in-progress ─────────────────────────────────────────────────
  it("returns setup:chat when persona_status is chat_pending", () => {
    expect(resolvePlanMode(persona("chat_pending"), null)).toBe("setup:chat");
  });

  it("returns setup:persona-generating when persona_status is generating", () => {
    expect(resolvePlanMode(persona("generating"), null)).toBe("setup:persona-generating");
  });

  it("returns setup:persona-failed when persona_status is failed and no persona content", () => {
    const p: PersonaResponse = {
      id: "p1",
      persona_status: "failed",
      questionnaire: null,
      persona: null,
      error_detail: "oops",
    };
    expect(resolvePlanMode(p, null)).toBe("setup:persona-failed");
  });

  // ── Persona ready, no plan ──────────────────────────────────────────────
  it("returns setup:plan-intro when persona is ready but no plan", () => {
    expect(resolvePlanMode(persona("ready"), null)).toBe("setup:plan-intro");
  });

  it("returns setup:plan-intro when persona is edited but no plan", () => {
    expect(resolvePlanMode(persona("edited"), null)).toBe("setup:plan-intro");
  });

  // ── Plan generating ─────────────────────────────────────────────────────
  it("returns setup:plan-generating when generating + no items", () => {
    expect(resolvePlanMode(persona("ready"), plan("generating", []))).toBe("setup:plan-generating");
  });

  it("returns workspace:regenerating when generating + items", () => {
    expect(resolvePlanMode(persona("ready"), plan("generating", [item(1)]))).toBe("workspace:regenerating");
  });

  // ── Plan failed ─────────────────────────────────────────────────────────
  it("returns setup:plan-failed when failed + no items", () => {
    expect(resolvePlanMode(persona("ready"), plan("failed", []))).toBe("setup:plan-failed");
  });

  it("returns workspace:refresh-failed when failed + items", () => {
    expect(resolvePlanMode(persona("ready"), plan("failed", [item(1)]))).toBe("workspace:refresh-failed");
  });

  // ── Plan ready/edited ───────────────────────────────────────────────────
  it("returns workspace when plan is ready + items", () => {
    expect(resolvePlanMode(persona("ready"), plan("ready", [item(1)]))).toBe("workspace");
  });

  it("returns workspace when plan is edited + items", () => {
    expect(resolvePlanMode(persona("ready"), plan("edited", [item(1)]))).toBe("workspace");
  });

  it("returns workspace when plan is ready + no items (empty plan edge case)", () => {
    expect(resolvePlanMode(persona("ready"), plan("ready", []))).toBe("workspace");
  });

  // ── Transition: ready→generating stays in workspace ─────────────────────
  it("stays in workspace:regenerating when plan flips from ready→generating with items", () => {
    // Simulate previous tick was ready, now generating
    const p = persona("ready");
    const pl = plan("generating", [item(1), item(2)]);
    expect(resolvePlanMode(p, pl)).toBe("workspace:regenerating");
  });

  // ── Failed persona with existing persona content ────────────────────────
  it("falls through to plan-intro when persona failed but has existing persona content", () => {
    const p: PersonaResponse = {
      id: "p1",
      persona_status: "failed",
      questionnaire: null,
      persona: { summary: "s", content_pillars: [], tone: "", audience: "", posting_cadence: "", sample_topics: [] },
      error_detail: null,
    };
    // persona.persona is truthy → skips persona-failed → checks plan
    expect(resolvePlanMode(p, null)).toBe("setup:plan-intro");
  });
});
