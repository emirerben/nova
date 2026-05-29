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
      <h1 className="mb-3 font-display text-3xl text-white">{title}</h1>
      <p className="mx-auto mb-8 max-w-sm text-zinc-400">{subtitle}</p>
      <button
        onClick={() => signIn("google", { callbackUrl })}
        className="inline-flex items-center gap-2 rounded-full bg-white px-6 py-3 font-medium text-black transition-colors hover:bg-zinc-200"
      >
        Continue with Google
      </button>
    </div>
  );
}
