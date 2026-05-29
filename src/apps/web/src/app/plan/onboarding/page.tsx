"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import {
  createPersona,
  NotAuthenticatedError,
  type PersonaQuestionnaire,
} from "@/lib/plan-api";

const FIELDS: { key: keyof PersonaQuestionnaire; label: string; placeholder: string }[] = [
  { key: "work", label: "Work", placeholder: "What do you do for work?" },
  { key: "school", label: "School", placeholder: "Studying anything? Where?" },
  { key: "social", label: "Social life", placeholder: "Who do you spend time with?" },
  { key: "location", label: "Location", placeholder: "Where are you based?" },
  { key: "hobbies", label: "Hobbies", placeholder: "What do you do for fun?" },
  { key: "travels", label: "Travels", placeholder: "Where do you go?" },
  { key: "passions", label: "Passions", placeholder: "What could you talk about for hours?" },
  { key: "tiktok_handle", label: "TikTok handle (optional)", placeholder: "@yourhandle" },
];

const EMPTY: PersonaQuestionnaire = {
  work: "",
  school: "",
  social: "",
  location: "",
  hobbies: "",
  travels: "",
  passions: "",
  tiktok_handle: "",
};

export default function OnboardingPage() {
  const router = useRouter();
  const [answers, setAnswers] = useState<PersonaQuestionnaire>(EMPTY);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [needsAuth, setNeedsAuth] = useState(false);

  const hasAny = Object.values(answers).some((v) => v.trim().length > 0);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await createPersona(answers);
      router.push("/plan/persona");
    } catch (err) {
      if (err instanceof NotAuthenticatedError) {
        setNeedsAuth(true);
      } else {
        setError(err instanceof Error ? err.message : "Something went wrong");
      }
      setSubmitting(false);
    }
  }

  if (needsAuth) {
    return (
      <main className="min-h-[calc(100vh-3.5rem)] bg-black text-white">
        <div className="max-w-md mx-auto px-4 py-24 text-center">
          <h1 className="text-2xl font-semibold mb-3">Sign in to build your plan</h1>
          <p className="text-zinc-400 mb-8">
            We use your Google account to save your persona and content plan.
          </p>
          <a
            href="/api/auth/signin?callbackUrl=/plan/onboarding"
            className="inline-block rounded bg-white px-6 py-3 font-medium text-black hover:bg-zinc-200"
          >
            Sign in with Google
          </a>
        </div>
      </main>
    );
  }

  return (
    <main className="min-h-[calc(100vh-3.5rem)] bg-black text-white">
      <div className="max-w-2xl mx-auto px-4 py-12">
        <h1 className="text-2xl font-semibold mb-2">Tell us about you</h1>
        <p className="text-zinc-400 mb-8">
          A few quick answers. We turn these into an editable creator persona — the
          voice and themes behind your videos. Skip anything that doesn&apos;t apply.
        </p>

        {error && (
          <div className="mb-6 rounded border border-red-700 bg-red-950/50 px-4 py-3 text-red-200">
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-5">
          {FIELDS.map((f) => (
            <label key={f.key} className="block">
              <span className="mb-1 block text-sm font-medium text-zinc-300">{f.label}</span>
              <textarea
                value={answers[f.key]}
                onChange={(e) => setAnswers((a) => ({ ...a, [f.key]: e.target.value }))}
                placeholder={f.placeholder}
                rows={f.key === "tiktok_handle" ? 1 : 2}
                className="w-full resize-y rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-white placeholder-zinc-600 focus:border-zinc-400 focus:outline-none"
              />
            </label>
          ))}

          <button
            type="submit"
            disabled={submitting || !hasAny}
            className="w-full rounded bg-white px-6 py-3 font-medium text-black hover:bg-zinc-200 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-400"
          >
            {submitting ? "Building your persona…" : "Build my persona"}
          </button>
          {!hasAny && (
            <p className="text-center text-sm text-zinc-500">Answer at least one question.</p>
          )}
        </form>
      </div>
    </main>
  );
}
