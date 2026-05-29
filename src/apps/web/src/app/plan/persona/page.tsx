import { redirect } from "next/navigation";

// The persona review/edit screen is now a step inside the unified /plan wizard.
// Kept as a redirect so old links + the previous sign-in callbackUrl
// (/plan/persona) still resolve.
export default function PersonaRedirect() {
  redirect("/plan?step=persona");
}
