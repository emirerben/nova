import { LightCard } from "../ui/LightCard";
import { planProgress } from "@/app/plan/_lib/plan-logic";
import type { ContentPlan } from "@/lib/plan-api";

interface MomentumCardProps {
  plan: ContentPlan;
}

export function MomentumCard({ plan }: MomentumCardProps) {
  const { total, made, pct } = planProgress(plan.items ?? []);
  const rendering = (plan.items ?? []).filter((i) => i.status === "generating").length;
  const readyToPost = (plan.items ?? []).filter((i) => i.status === "ready").length;
  const activating = ["seeding", "activating"].includes(plan.activation_status ?? "");

  return (
    <LightCard className="px-6 py-5">
      <p className="font-display text-[28px] font-medium text-[#0c0c0e]">
        {made} <span className="text-[#a1a1aa]">/ {total}</span>
      </p>
      <p className="mt-1 text-[13px] text-[#71717a]">videos made</p>
      <div className="mt-3 h-[5px] w-full overflow-hidden rounded-full bg-zinc-100">
        <div
          className="h-full rounded-full bg-lime-600 transition-all duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
      {activating && (
        <p className="mt-3 flex items-center gap-2 text-[13px] text-[#71717a]">
          <span className="relative flex h-2 w-2">
            <span className="motion-safe:animate-ping absolute inline-flex h-full w-full rounded-full bg-lime-600 opacity-60" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-lime-600" />
          </span>
          Matching your clips to your plan…
        </p>
      )}
      {!activating && rendering > 0 && (
        <p className="mt-3 flex items-center gap-2 text-[13px] text-[#71717a]">
          <span className="relative flex h-2 w-2">
            <span className="motion-safe:animate-ping absolute inline-flex h-full w-full rounded-full bg-lime-600 opacity-60" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-lime-600" />
          </span>
          {rendering} {rendering === 1 ? "edit" : "edits"} rendering
        </p>
      )}
      {!activating && rendering === 0 && readyToPost > 0 && (
        <p className="mt-3 text-[13px] text-lime-700">
          {readyToPost} ready to post
        </p>
      )}
    </LightCard>
  );
}
