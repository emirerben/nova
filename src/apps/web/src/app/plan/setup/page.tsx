import { redirect } from "next/navigation";

// /plan already handles all setup modes correctly (prescreen → chat → reveal →
// plan-intro → generating). /plan/setup is the canonical documented URL for
// onboarding deeplinks and back-compat; redirect to keep one source of truth.
export default function PlanSetupPage() {
  redirect("/plan");
}
