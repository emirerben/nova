import { Suspense } from "react";
import type { TemplateListItem } from "@/lib/api";
import TemplateGrid from "./TemplateGrid";
import TemplateGridSkeleton from "./TemplateGridSkeleton";

export const revalidate = 60;

async function fetchTemplates(): Promise<TemplateListItem[]> {
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  const res = await fetch(`${apiUrl}/templates`, {
    next: { revalidate: 60, tags: ["templates"] },
  });
  if (!res.ok) {
    throw new Error(`Failed to fetch templates: ${res.status}`);
  }
  return res.json();
}

async function TemplateGridLoader() {
  const templates = await fetchTemplates();
  return <TemplateGrid templates={templates} />;
}

export default function HomePage() {
  return (
    <main className="min-h-[calc(100vh-3.5rem)] bg-black text-white">
      <div className="max-w-6xl mx-auto px-4 py-12">
        <p className="text-zinc-300 text-lg mb-10">
          Pick a template. Upload your clips. Done.
        </p>

        <Suspense fallback={<TemplateGridSkeleton />}>
          <TemplateGridLoader />
        </Suspense>
      </div>
    </main>
  );
}
