/**
 * Mode router pure function for the /plan workspace.
 * Converts (persona, plan) state into a typed mode string that
 * plan/page.tsx uses to dispatch to the right component.
 */

import type { ContentPlan, PersonaResponse } from "@/lib/plan-api";

export type PlanMode =
  | "setup:prescreen"
  | "setup:fork"
  | "setup:chat"
  | "setup:persona-generating"
  | "setup:persona-failed"
  | "setup:plan-intro"
  | "setup:plan-generating"
  | "setup:plan-failed"
  | "setup:edit-context"
  | "setup:edit-upload"
  | "setup:edit-generating"
  | "setup:edit-payoff"
  | "workspace"
  | "workspace:regenerating"
  | "workspace:refresh-failed";

export function resolvePlanMode(
  persona: PersonaResponse | null | undefined,
  plan: ContentPlan | null | undefined,
): PlanMode {
  // No persona at all → start onboarding
  if (!persona) return "setup:prescreen";

  // questionnaire carries the merged onboarding fields (PersonaQuestionnaire interface
  // is declaration-merged in plan-api.ts with content_mode + onboarding_* fields).
  const q = persona.questionnaire;

  const isFootagePath =
    q?.content_mode === "existing_footage" || q?.content_mode === "mixed";

  // Persona in-progress states
  if (persona.persona_status === "chat_pending") {
    // If they chose the footage path, route into that funnel
    if (isFootagePath) {
      // Check payoff_done first — topic/intent are not required in the new flow
      // where context is collected inline in the clip-group step.
      if (!q?.onboarding_payoff_done) {
        if (!q?.onboarding_topic && !q?.onboarding_intent) return "setup:edit-context";
        if (!q?.onboarding_edit_job_id) return "setup:edit-upload";
        return "setup:edit-generating";
      }
      // payoff_done → fall through to chat
    }
    // No content_mode yet → show fork screen
    if (!q?.content_mode) return "setup:fork";
    // content_mode="create_new" → fresh path chat
    return "setup:chat";
  }

  if (persona.persona_status === "generating") return "setup:persona-generating";
  if (persona.persona_status === "failed" && !persona.persona) return "setup:persona-failed";

  // Persona ready/edited
  if (isFootagePath) {
    if (!q?.onboarding_edit_job_id) return "setup:edit-upload";
    // Job exists — page component checks job status to distinguish generating vs payoff
    if (!q?.onboarding_payoff_done) return "setup:edit-payoff";
  }

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
