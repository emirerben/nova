"use client";

import { useRef, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type FormState = "idle" | "submitting" | "success" | "duplicate" | "error";

export default function LandingPage() {
  const [state, setState] = useState<FormState>("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const email = inputRef.current?.value.trim() ?? "";
    if (!email) return;

    setErrorMsg(null);
    setState("submitting");

    try {
      const res = await fetch(`${API_URL}/api/waitlist`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });

      if (res.status === 201) {
        setState("success");
      } else if (res.status === 409) {
        setState("duplicate");
      } else if (res.status === 422) {
        setErrorMsg("Enter a valid email address");
        setState("error");
      } else if (res.status === 429) {
        setErrorMsg("Too many attempts — try again in a minute");
        setState("error");
      } else {
        setErrorMsg("Something went wrong — try again");
        setState("error");
      }
    } catch {
      setErrorMsg("Something went wrong — try again");
      setState("error");
    }
  }

  const isDone = state === "success" || state === "duplicate";

  return (
    <main className="min-h-screen bg-black text-white flex flex-col items-center justify-center p-6">
      <p className="text-xs tracking-[0.2em] uppercase text-zinc-400 mb-6">
        Nova
      </p>

      <h1 className="text-4xl sm:text-5xl font-bold text-white leading-tight text-center mb-3 max-w-sm">
        Drop raw footage.
        <br />
        Get 3 clips ready to post.
      </h1>

      <p className="text-zinc-400 text-center max-w-xs mb-10">
        You have footage you haven&apos;t posted in months.
        <br />
        Nova is invite-only — join the list.
      </p>

      <div role="status" aria-live="polite" className="w-full max-w-sm">
        {isDone ? (
          <div className="text-center">
            {state === "success" ? (
              <>
                <p className="text-white font-medium text-lg">
                  You&apos;re on the list.
                </p>
                <p className="text-zinc-400 text-sm mt-1">
                  We&apos;ll reach out when your spot opens.
                </p>
              </>
            ) : (
              <p className="text-zinc-400">You&apos;re already on the list ✓</p>
            )}
          </div>
        ) : (
          <form onSubmit={handleSubmit} noValidate>
            <label htmlFor="waitlist-email" className="sr-only">
              Email address
            </label>
            <div className="flex flex-col sm:flex-row gap-2">
              <input
                ref={inputRef}
                id="waitlist-email"
                type="email"
                placeholder="your@email.com"
                required
                disabled={state === "submitting"}
                className="flex-1 min-w-0 bg-zinc-900 border border-zinc-700 rounded-full px-4 py-3 text-white placeholder:text-zinc-500 focus:outline-none focus:ring-1 focus:ring-white text-sm disabled:opacity-50"
              />
              <button
                type="submit"
                disabled={state === "submitting"}
                className="shrink-0 bg-white text-black px-6 py-3 rounded-full font-medium text-sm hover:bg-zinc-200 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {state === "submitting" ? "Joining..." : "Join waitlist →"}
              </button>
            </div>

            {state === "error" && errorMsg && (
              <p className="text-red-400 text-sm mt-3 text-center">
                {errorMsg}
              </p>
            )}
          </form>
        )}
      </div>
    </main>
  );
}
