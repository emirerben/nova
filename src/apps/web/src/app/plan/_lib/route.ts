/**
 * Mode router pure function for the /plan workspace.
 * Converts (persona, plan) state into a typed mode string that
 * plan/page.tsx uses to dispatch to the right component.
 */

import type { ContentPlan, PersonaResponse } from "@/lib/plan-api";

export type PlanMode =
  | "setup:prescreen"
  | "setup:chat"
  | "setup:persona-generating"
  | "setup:persona-failed"
  | "setup:plan-intro"
  | "setup:plan-generating"
  | "setup:plan-failed"
  | "workspace"
  | "workspace:regenerating"
  | "workspace:refresh-failed";

export function resolvePlanMode(
  persona: PersonaResponse | null | undefined,
  plan: ContentPlan | null | undefined,
): PlanMode {
  // No persona at all → start onboarding
  if (!persona) return "setup:prescreen";

  // Persona in-progress states
  if (persona.persona_status === "chat_pending") return "setup:chat";
  if (persona.persona_status === "generating") return "setup:persona-generating";
  if (persona.persona_status === "failed" && !persona.persona) return "setup:persona-failed";

  // Persona ready/edited — check plan
  if (!plan) return "setup:plan-intro";

  const hasItems = Array.isArray(plan.items) && plan.items.length > 0;

  if (plan.plan_status === "generating") {
    return hasItems ? "workspace:regenerating" : "setup:plan-generating";
  }
  if (plan.plan_status === "failed") {
    return hasItems ? "workspace:refresh-failed" : "setup:plan-failed";
  }

  // ready or edited
  return "workspace";
}
