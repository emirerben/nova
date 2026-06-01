import { redirect } from "next/navigation";

// The onboarding questionnaire is now the first step of the unified /plan
// wizard. Kept as a redirect so old links + the previous sign-in callbackUrl
// (/plan/onboarding) still resolve.
export default function OnboardingRedirect() {
  redirect("/plan?step=you");
}
