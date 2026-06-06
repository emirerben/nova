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
  const promptId = `q-prompt-${index}`;
  const hintId = hint ? `q-hint-${index}` : undefined;

  return (
    <div key={index} className="animate-fade-up py-4">
      <p className="mb-1 text-xs font-medium uppercase tracking-wide text-lime-700">
        Question {index + 1} of {total}
        {optional && <span className="ml-2 text-[#a1a1aa]">· optional</span>}
      </p>
      <h1 id={promptId} className="font-display text-3xl leading-snug text-[#0c0c0e]">
        {prompt}
      </h1>
      {hint && (
        <p id={hintId} className="mt-2 text-[#71717a]">
          {hint}
        </p>
      )}

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
        aria-labelledby={hintId ? `${promptId} ${hintId}` : promptId}
        className="mt-6 w-full resize-y rounded-lg border border-zinc-200 bg-white px-4 py-3 text-lg text-[#0c0c0e] placeholder-zinc-400 transition-colors focus:border-lime-600/60 focus:outline-none"
      />

      {examples.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          <span className="self-center text-xs text-[#a1a1aa]">e.g.</span>
          {examples.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => onChipPick(c)}
              className="inline-flex min-h-[44px] items-center rounded-full border border-zinc-200 bg-white px-4 py-1 text-xs text-[#3f3f46] transition-colors hover:border-lime-600/50 hover:text-[#0c0c0e]"
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
          className="inline-flex min-h-[44px] items-center text-sm text-[#71717a] transition-colors hover:text-[#0c0c0e] disabled:invisible"
        >
          ← Back
        </button>

        <div className="flex items-center gap-1.5">
          {Array.from({ length: total }).map((_, i) => (
            <span
              key={i}
              className={cn(
                "h-1.5 w-1.5 rounded-full transition-colors",
                i === index ? "bg-lime-600" : i < index ? "bg-zinc-400" : "bg-zinc-200",
              )}
            />
          ))}
        </div>

        <button
          type="button"
          onClick={onNext}
          disabled={submitting}
          className="inline-flex min-h-[44px] items-center rounded-full bg-[#0c0c0e] px-5 py-2 text-sm font-semibold text-white transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {submitting ? "Building…" : isLast ? submitLabel : optional && !value.trim() ? "Skip →" : "Next →"}
        </button>
      </div>
    </div>
  );
}
