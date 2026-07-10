"use client";
import type { ContentPlan } from "@/lib/plan-api";
import { IdeasHome } from "./IdeasHome";
import SeedUploadCard from "../SeedUploadCard";

interface WorkspaceHomeProps {
  plan: ContentPlan;
  onRefresh: () => void | Promise<unknown>;
  onError: (msg: string) => void;
}

export function WorkspaceHome({
  plan,
  onRefresh,
  onError,
}: WorkspaceHomeProps) {
  const activating = ["seeding", "activating"].includes(plan.activation_status ?? "");

  return (
    <div className="min-h-screen bg-[#fafaf8]">
      <div className="mx-auto flex max-w-[760px] flex-col gap-8 px-6 pb-24 pt-14">
        {activating && (
          <SeedUploadCard plan={plan} onError={onError} onRefresh={onRefresh} />
        )}
        <IdeasHome plan={plan} onRefresh={onRefresh} />
      </div>
    </div>
  );
}
