"use client";

import { cn } from "@/lib/cn";

/**
 * A single onboarding question rendered one-at-a-time. Presentational: the
 * parent owns the value and the current index. Enter advances (Shift+Enter for
 * a newline), example chips fill the field, dots show progress.
 */
export default function QuestionCard({
  prompt,
  hint,
  value,
  examples,
  optional,
  index,
  total,
  onChange,
  onNext,
  onBack,
  onChipPick,
  submitLabel,
  submitting,
}: {
  prompt: string;
  hint?: string;
  value: string;
  examples: string[];
  optional?: boolean;
  index: number;
  total: number;
  onChange: (v: string) => void;
  onNext: () => void;
  onBack: () => void;
  onChipPick: (chip: string) => void;
  submitLabel: string;
  submitting?: boolean;
}) {
  const isLast = index === total - 1;

  return (
    <div key={index} className="animate-fade-up py-4">
      <p className="mb-1 text-xs font-medium uppercase tracking-wide text-amber-300">
        Question {index + 1} of {total}
        {optional && <span className="ml-2 text-zinc-500">· optional</span>}
      </p>
      <h1 className="font-display text-3xl leading-snug text-white">{prompt}</h1>
      {hint && <p className="mt-2 text-zinc-400">{hint}</p>}

      <textarea
        autoFocus
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            onNext();
          }
        }}
        rows={2}
        placeholder="Type your answer…"
        className="mt-6 w-full resize-y rounded-lg border border-zinc-700 bg-zinc-900 px-4 py-3 text-lg text-white placeholder-zinc-600 transition-colors focus:border-amber-400/60 focus:outline-none"
      />

      {examples.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          <span className="self-center text-xs text-zinc-500">e.g.</span>
          {examples.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => onChipPick(c)}
              className="rounded-full border border-zinc-800 bg-zinc-900/60 px-3 py-1 text-xs text-zinc-300 transition-colors hover:border-amber-400/50 hover:text-white"
            >
              {c}
            </button>
          ))}
        </div>
      )}

      <div className="mt-8 flex items-center justify-between">
        <button
          type="button"
          onClick={onBack}
          disabled={index === 0 || submitting}
          className="text-sm text-zinc-500 transition-colors hover:text-white disabled:invisible"
        >
          ← Back
        </button>

        <div className="flex items-center gap-1.5">
          {Array.from({ length: total }).map((_, i) => (
            <span
              key={i}
              className={cn(
                "h-1.5 w-1.5 rounded-full transition-colors",
                i === index ? "bg-amber-400" : i < index ? "bg-zinc-500" : "bg-zinc-800",
              )}
            />
          ))}
        </div>

        <button
          type="button"
          onClick={onNext}
          disabled={submitting}
          className="rounded-full bg-white px-5 py-2 text-sm font-medium text-black transition-colors hover:bg-zinc-200 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-400"
        >
          {submitting ? "Building…" : isLast ? submitLabel : optional && !value.trim() ? "Skip →" : "Next →"}
        </button>
      </div>
    </div>
  );
}
