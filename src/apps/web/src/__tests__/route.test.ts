import { resolvePlanMode } from "@/app/plan/_lib/route";
import type { ContentPlan, PersonaResponse, PlanItem } from "@/lib/plan-api";

function persona(
  status: PersonaResponse["persona_status"],
  opts: {
    hasPersona?: boolean;
    questionnaire?: Partial<PersonaResponse["questionnaire"]> & Record<string, unknown>;
  } = {},
): PersonaResponse {
  return {
    id: "p1",
    persona_status: status,
    questionnaire: opts.questionnaire
      ? ({
          work: "",
          school: "",
          social: "",
          location: "",
          hobbies: "",
          travels: "",
          passions: "",
          tiktok_handle: "",
          ...opts.questionnaire,
        } as PersonaResponse["questionnaire"])
      : null,
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
  // chat_pending with no questionnaire → fork screen first (edits-first onboarding).
  // The old "returns setup:chat" path is now reached only after a content_mode is chosen.
  it("returns setup:fork when persona_status is chat_pending with no questionnaire", () => {
    expect(resolvePlanMode(persona("chat_pending"), null)).toBe("setup:fork");
  });

  it("returns setup:chat when persona_status is chat_pending and content_mode is create_new", () => {
    expect(
      resolvePlanMode(persona("chat_pending", { questionnaire: { content_mode: "create_new" } }), null),
    ).toBe("setup:chat");
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

  // ── Edits-first onboarding fork ─────────────────────────────────────────

  it("returns setup:fork when persona chat_pending with no questionnaire", () => {
    expect(resolvePlanMode(persona("chat_pending"), null)).toBe("setup:fork");
  });

  it("returns setup:fork when persona chat_pending with null questionnaire", () => {
    const p: PersonaResponse = {
      id: "p1",
      persona_status: "chat_pending",
      questionnaire: null,
      persona: null,
      error_detail: null,
    };
    expect(resolvePlanMode(p, null)).toBe("setup:fork");
  });

  it("returns setup:edit-context when footage path chosen but no topic/intent", () => {
    const p = persona("chat_pending", {
      questionnaire: { content_mode: "existing_footage" },
    });
    expect(resolvePlanMode(p, null)).toBe("setup:edit-context");
  });

  it("returns setup:edit-upload when footage path + topic but no job id", () => {
    const p = persona("chat_pending", {
      questionnaire: { content_mode: "existing_footage", onboarding_topic: "hiking trip" },
    });
    expect(resolvePlanMode(p, null)).toBe("setup:edit-upload");
  });

  it("returns setup:edit-upload when footage path + intent only, no job id", () => {
    const p = persona("chat_pending", {
      questionnaire: { content_mode: "existing_footage", onboarding_intent: "inspired" },
    });
    expect(resolvePlanMode(p, null)).toBe("setup:edit-upload");
  });

  it("returns setup:edit-generating when footage path + job id but not payoff done", () => {
    const p = persona("chat_pending", {
      questionnaire: {
        content_mode: "existing_footage",
        onboarding_topic: "hiking trip",
        onboarding_edit_job_id: "j1",
        onboarding_clip_paths: ["a.mp4"],
      },
    });
    expect(resolvePlanMode(p, null)).toBe("setup:edit-generating");
  });

  it("returns setup:edit-payoff when persona ready + footage path + job id but not payoff done", () => {
    const p = persona("ready", {
      questionnaire: {
        content_mode: "existing_footage",
        onboarding_topic: "hiking trip",
        onboarding_edit_job_id: "j1",
        onboarding_clip_paths: ["a.mp4"],
      },
    });
    expect(resolvePlanMode(p, null)).toBe("setup:edit-payoff");
  });

  it("returns setup:chat when content_mode is create_new", () => {
    const p = persona("chat_pending", {
      questionnaire: { content_mode: "create_new" },
    });
    expect(resolvePlanMode(p, null)).toBe("setup:chat");
  });

  it("falls through to workspace when footage path + payoff done + plan ready", () => {
    const p = persona("ready", {
      questionnaire: {
        content_mode: "existing_footage",
        onboarding_edit_job_id: "j1",
        onboarding_payoff_done: true,
      },
    });
    expect(resolvePlanMode(p, plan("ready", [item(1)]))).toBe("workspace");
  });
});
