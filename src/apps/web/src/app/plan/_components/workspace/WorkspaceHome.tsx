"use client";
import Link from "next/link";
import type { ContentPlan } from "@/lib/plan-api";
import { Eyebrow } from "@/components/ui/Eyebrow";
import { IdeasHome } from "./IdeasHome";
import SeedUploadCard from "../SeedUploadCard";

interface WorkspaceHomeProps {
  plan: ContentPlan;
  onRefresh: () => void | Promise<unknown>;
  onPlanChange: (plan: ContentPlan) => void;
  onError: (msg: string) => void;
}

export function WorkspaceHome({
  plan,
  onRefresh,
  onPlanChange,
  onError,
}: WorkspaceHomeProps) {
  const creationHubEnabled =
    process.env.NEXT_PUBLIC_CREATION_HUB_ENABLED === "true";
  const activating = ["seeding", "activating"].includes(plan.activation_status ?? "");

  return (
    <div className="min-h-screen bg-[#fafaf8]">
      <div className="mx-auto flex max-w-[760px] flex-col gap-8 px-6 pb-24 pt-14">
        {creationHubEnabled && <CreationHub />}
        {activating && (
          <SeedUploadCard plan={plan} onError={onError} onRefresh={onRefresh} />
        )}
        <div id="ideas" className="scroll-mt-20">
          <IdeasHome plan={plan} onRefresh={onRefresh} onPlanChange={onPlanChange} />
        </div>
      </div>
    </div>
  );
}

function CreationHub() {
  const manualEditorEnabled =
    process.env.NEXT_PUBLIC_MANUAL_EDITOR_ENABLED === "true";
  return (
    <section
      aria-labelledby="creation-hub-title"
      className="border-b border-zinc-200 pb-10 pt-2 sm:pb-12 sm:pt-6"
    >
      <Eyebrow tone="lime" className="mb-4">
        Your video editor
      </Eyebrow>
      <h1
        id="creation-hub-title"
        className="max-w-[650px] font-display text-[2.5rem] leading-[1.03] tracking-[-0.02em] text-[#0c0c0e] sm:text-5xl"
      >
        Turn raw clips into a video worth sharing.
      </h1>
      <p className="mt-5 max-w-[560px] text-base leading-7 text-[#71717a] sm:text-lg">
        Upload your footage and Kria will shape the first cut. You can refine it in the
        full editor as soon as it is ready.
      </p>
      <div className="mt-8 flex flex-col items-start gap-3 sm:flex-row sm:items-center">
        <Link
          href="/create"
          className="inline-flex min-h-[44px] w-full items-center justify-center rounded-full bg-[#0c0c0e] px-7 py-3 text-[15px] font-semibold text-white transition-opacity hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0c0c0e] sm:w-auto"
        >
          Make a video with Kria
        </Link>
        {manualEditorEnabled && (
          <Link
            href="/create/manual"
            className="inline-flex min-h-[44px] w-full items-center justify-center rounded-full border border-zinc-300 px-5 py-3 text-[15px] font-medium text-[#3f3f46] transition-colors hover:border-zinc-500 hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0c0c0e] sm:w-auto"
          >
            Edit myself
          </Link>
        )}
        <Link
          href="#ideas"
          className="inline-flex min-h-[44px] w-full items-center justify-center rounded-full px-5 py-3 text-[15px] font-medium text-[#71717a] underline-offset-4 hover:text-[#0c0c0e] hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0c0c0e] sm:w-auto"
        >
          Plan content
        </Link>
      </div>
    </section>
  );
}
