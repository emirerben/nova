import Link from "next/link";
import { Suspense } from "react";
import type { TemplateListItem } from "@/lib/api";
import TemplateGrid from "./TemplateGrid";
import TemplateGridSkeleton from "./TemplateGridSkeleton";

export const dynamic = "force-dynamic";

async function fetchTemplates(): Promise<TemplateListItem[]> {
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  const res = await fetch(`${apiUrl}/templates`, {
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`Failed to fetch templates: ${res.status}`);
  }
  return res.json();
}

async function TemplateGridLoader() {
  const templates = await fetchTemplates();
  // Hide templates with broken/zero duration (looks like bad seed data — renders "0s · 1 clip").
  const visible = templates.filter((t) => t.total_duration_s > 0);
  return <TemplateGrid templates={visible} />;
}

export default function HomePage() {
  return (
    <main className="min-h-[calc(100vh-3.5rem)] bg-black text-white">
      <div className="max-w-6xl mx-auto px-4 py-12">
        <Link
          href="/plan"
          className="group mb-10 flex items-center justify-between gap-4 rounded-xl border border-zinc-800 bg-gradient-to-r from-amber-500/10 via-zinc-900/40 to-zinc-900/40 px-5 py-4 transition-colors hover:border-amber-400/50"
        >
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-amber-300">
              New
            </p>
            <p className="font-display text-lg text-white">
              Get a personalized 30-day content plan
            </p>
            <p className="text-sm text-zinc-400">
              Answer a few questions — we&apos;ll script a month of videos made for you.
            </p>
          </div>
          <span className="shrink-0 text-amber-300 transition-transform group-hover:translate-x-1">
            →
          </span>
        </Link>

        <p className="text-zinc-300 text-lg mb-10">
          Or pick a template. Upload your clips. Done.
        </p>

        <Suspense fallback={<TemplateGridSkeleton />}>
          <TemplateGridLoader />
        </Suspense>
      </div>
    </main>
  );
}
