"use client";

import { signIn } from "next-auth/react";

/**
 * Auth gate shown when a plan route 401s (or useSession reports signed-out).
 * callbackUrl returns the user to exactly where they were.
 */
export default function SignInPrompt({
  callbackUrl,
  title = "Sign in to build your plan",
  subtitle = "We use your Google account to save your persona and content plan.",
}: {
  callbackUrl: string;
  title?: string;
  subtitle?: string;
}) {
  return (
    <div className="animate-fade-up py-24 text-center">
      <h1 className="mb-3 font-display text-3xl text-[#0c0c0e]">{title}</h1>
      <p className="mx-auto mb-8 max-w-sm text-[#3f3f46]">{subtitle}</p>
      <button
        onClick={() => signIn("google", { callbackUrl })}
        className="inline-flex items-center gap-2 rounded-full bg-[#0c0c0e] px-9 py-[15px] text-[15px] font-semibold text-white transition-opacity hover:opacity-80"
      >
        Continue with Google
      </button>
    </div>
  );
}
