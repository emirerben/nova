"use client";

import { useState } from "react";
import type { PersonaQuestionnaire } from "@/lib/plan-api";
import QuestionCard from "./QuestionCard";

interface FieldDef {
  key: keyof PersonaQuestionnaire;
  prompt: string;
  hint?: string;
  examples: string[];
  optional?: boolean;
}

// One-question-at-a-time version of the old 8-field form. Same keys/contract as
// PersonaQuestionnaire — just walked through conversationally with examples.
const FIELDS: FieldDef[] = [
  {
    key: "work",
    prompt: "What do you do for work?",
    examples: ["nurse", "founder", "barista", "software engineer"],
  },
  {
    key: "school",
    prompt: "Studying anything? Where?",
    hint: "Skip if it doesn't apply.",
    examples: ["UCLA, junior", "bootcamp grad", "not in school"],
    optional: true,
  },
  {
    key: "social",
    prompt: "Who do you spend your time with?",
    examples: ["roommates", "my band", "gym friends", "my kids"],
  },
  {
    key: "location",
    prompt: "Where are you based?",
    examples: ["Brooklyn", "Lagos", "small town in Ohio"],
  },
  {
    key: "hobbies",
    prompt: "What do you do for fun?",
    examples: ["thrifting", "climbing", "cooking", "gaming"],
  },
  {
    key: "travels",
    prompt: "Where do you go?",
    hint: "Trips, weekends away, places you keep coming back to.",
    examples: ["road trips", "Tokyo every year", "nowhere, homebody"],
    optional: true,
  },
  {
    key: "passions",
    prompt: "What could you talk about for hours?",
    examples: ["sneakers", "personal finance", "skincare", "F1"],
  },
  {
    key: "tiktok_handle",
    prompt: "Your TikTok handle?",
    hint: "Optional — helps us match your existing voice.",
    examples: [],
    optional: true,
  },
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

/**
 * Drives the onboarding questionnaire. Owns the field index + answers; calls
 * `onSubmit` with the full questionnaire once the user finishes the last card.
 */
export default function OnboardingStep({
  onSubmit,
  submitting,
}: {
  onSubmit: (answers: PersonaQuestionnaire) => void | Promise<void>;
  submitting: boolean;
}) {
  const [index, setIndex] = useState(0);
  const [answers, setAnswers] = useState<PersonaQuestionnaire>(EMPTY);

  const field = FIELDS[index];

  function next() {
    if (index < FIELDS.length - 1) {
      setIndex((i) => i + 1);
    } else {
      void onSubmit(answers);
    }
  }

  return (
    <div className="py-2">
      <p className="mb-8 text-center text-zinc-400">
        A few quick answers become an editable creator persona — the voice and themes
        behind your videos.
      </p>
      <QuestionCard
        prompt={field.prompt}
        hint={field.hint}
        value={answers[field.key]}
        examples={field.examples}
        optional={field.optional}
        index={index}
        total={FIELDS.length}
        onChange={(v) => setAnswers((a) => ({ ...a, [field.key]: v }))}
        onChipPick={(chip) =>
          setAnswers((a) => ({
            ...a,
            [field.key]: a[field.key] ? `${a[field.key]}, ${chip}` : chip,
          }))
        }
        onNext={next}
        onBack={() => setIndex((i) => Math.max(0, i - 1))}
        submitLabel="Build my persona"
        submitting={submitting}
      />
    </div>
  );
}
