import { cn } from "@/lib/cn";

export type WizardStep = "you" | "persona" | "plan";

const STEPS: { key: WizardStep; label: string }[] = [
  { key: "you", label: "You" },
  { key: "persona", label: "Persona" },
  { key: "plan", label: "Plan" },
];

const ORDER: Record<WizardStep, number> = { you: 0, persona: 1, plan: 2 };

/**
 * Persistent progress chrome for the wizard. `current` is the active step;
 * `reached` is the furthest step the user has unlocked (controls which dots are
 * clickable — you can step back, not skip ahead).
 */
export default function Stepper({
  current,
  reached,
  onNavigate,
}: {
  current: WizardStep;
  reached: WizardStep;
  onNavigate?: (step: WizardStep) => void;
}) {
  const reachedIdx = ORDER[reached];
  const currentIdx = ORDER[current];

  return (
    <nav aria-label="Progress" className="flex items-center justify-center gap-3 py-8">
      {STEPS.map((s, i) => {
        const idx = ORDER[s.key];
        const done = idx < currentIdx;
        const active = idx === currentIdx;
        const navigable = idx <= reachedIdx && !active && !!onNavigate;
        return (
          <div key={s.key} className="flex items-center gap-3">
            <button
              type="button"
              disabled={!navigable}
              onClick={() => navigable && onNavigate?.(s.key)}
              className={cn(
                "flex items-center gap-2 rounded-full px-3 py-1.5 text-sm transition-colors",
                active && "bg-amber-400/10 text-amber-200",
                !active && done && "text-zinc-300 hover:text-white",
                !active && !done && "text-zinc-600",
                navigable && "cursor-pointer",
              )}
            >
              <span
                className={cn(
                  "flex h-6 w-6 items-center justify-center rounded-full border text-xs font-semibold transition-colors",
                  active && "border-amber-400 bg-amber-400 text-black",
                  !active && done && "border-zinc-500 bg-zinc-500 text-black",
                  !active && !done && "border-zinc-700 text-zinc-600",
                )}
              >
                {done ? "✓" : i + 1}
              </span>
              <span className="hidden sm:inline">{s.label}</span>
            </button>
            {i < STEPS.length - 1 && (
              <span
                className={cn(
                  "h-px w-6 transition-colors sm:w-10",
                  idx < currentIdx ? "bg-zinc-500" : "bg-zinc-800",
                )}
              />
            )}
          </div>
        );
      })}
    </nav>
  );
}
