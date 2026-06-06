import Link from "next/link";
import { LightCard } from "../ui/LightCard";
import { Eyebrow } from "../ui/Eyebrow";
import type { PersonaResponse } from "@/lib/plan-api";

interface PersonaCardProps {
  persona: PersonaResponse;
}

export function PersonaCard({ persona }: PersonaCardProps) {
  // Use signature_quote if it exists, fallback to first sentence of summary
  const quote =
    persona.persona?.signature_quote ||
    (persona.persona?.summary
      ? persona.persona.summary.split(/[.!?]/)[0].trim()
      : null);
  const pillars = (persona.persona?.content_pillars ?? []).slice(0, 3);

  return (
    <LightCard className="px-6 py-5">
      <div className="flex items-start justify-between">
        <Eyebrow tone="muted">Your persona</Eyebrow>
        <Link
          href="/plan/persona"
          className="text-[13px] text-[#71717a] underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
        >
          Edit
        </Link>
      </div>
      {quote && (
        <p className="font-display mt-3 text-[16px] italic leading-relaxed text-[#3f3f46]">
          &ldquo;{quote}&rdquo;
        </p>
      )}
      {pillars.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {pillars.map((p) => (
            <span
              key={p}
              className="truncate rounded-full border border-lime-200 bg-lime-50 px-3 py-1 text-[11px] font-medium text-lime-800"
            >
              {p}
            </span>
          ))}
        </div>
      )}
    </LightCard>
  );
}
