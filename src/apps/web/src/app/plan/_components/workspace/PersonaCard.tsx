import Link from "next/link";
import { LightCard } from "../ui/LightCard";
import { Eyebrow } from "../ui/Eyebrow";
import type { PersonaResponse } from "@/lib/plan-api";

interface PersonaCardProps {
  persona: PersonaResponse;
}

const MODE_LABEL: Record<string, string> = {
  existing_footage: "editing footage you already have",
  create_new: "filming new videos",
  mixed: "your footage + new filming",
};

export function PersonaCard({ persona }: PersonaCardProps) {
  const summary = persona.persona?.summary?.trim() || null;
  const pillars = (persona.persona?.content_pillars ?? []).slice(0, 3);
  const audience = persona.persona?.audience?.trim() || null;
  // P7 trust surface: show the assumption the planner works from so a wrong
  // one is visible (and editable) on day 0, not discovered on day 3.
  const situation = persona.persona?.current_situation?.trim() || null;
  const modeLabel = persona.persona?.content_mode
    ? MODE_LABEL[persona.persona.content_mode]
    : null;
  const planningAround = [situation, modeLabel].filter(Boolean).join(" · ");

  return (
    <LightCard className="px-6 py-5">
      <div className="flex items-start justify-between">
        <Eyebrow tone="muted">Your persona</Eyebrow>
        <Link href="/plan/persona" className="text-[13px] text-[#71717a] underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-[#0c0c0e]">
          Edit
        </Link>
      </div>
      {summary && (
        <p className="mt-3 text-[13px] text-[#3f3f46] line-clamp-2">{summary}</p>
      )}
      {planningAround && (
        <p className="mt-2 text-[12px] text-[#71717a]">
          <span className="font-medium text-[#3f3f46]">Planning around:</span>{" "}
          {planningAround}
        </p>
      )}
      {pillars.length > 0 && (
        <div className="mt-3">
          <p className="mb-1.5 text-[10px] font-medium uppercase tracking-wide text-[#a1a1aa]">Filming</p>
          <div className="flex flex-wrap gap-1.5">
            {pillars.map((p) => (
              <span key={p} className="truncate rounded-full border border-lime-200 bg-lime-50 px-3 py-1 text-[11px] font-medium text-lime-800">
                {p}
              </span>
            ))}
          </div>
        </div>
      )}
      {audience && (
        <div className="mt-3">
          <p className="text-[10px] font-medium uppercase tracking-wide text-[#a1a1aa]">For</p>
          <p className="mt-0.5 text-[12px] text-[#3f3f46]">{audience}</p>
        </div>
      )}
    </LightCard>
  );
}
