"use client";

import { useEffect, useRef, useState } from "react";
import { type TemplateListItem, listTemplates } from "@/lib/api";
import TemplateTile from "./TemplateTile";
import TemplatePreviewModal from "./TemplatePreviewModal";

export default function HomePage() {
  const [templates, setTemplates] = useState<TemplateListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [previewTemplate, setPreviewTemplate] = useState<TemplateListItem | null>(null);
  const lastTriggerRef = useRef<HTMLElement | null>(null);

  function load() {
    setError(null);
    setTemplates(null);
    listTemplates()
      .then(setTemplates)
      .catch((e: unknown) =>
        setError(e instanceof Error ? e.message : "Failed to load"),
      );
  }

  useEffect(() => {
    let cancelled = false;
    listTemplates()
      .then((t) => {
        if (!cancelled) setTemplates(t);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function openPreview(t: TemplateListItem) {
    if (typeof document !== "undefined") {
      lastTriggerRef.current = document.activeElement as HTMLElement | null;
    }
    setPreviewTemplate(t);
  }

  function closePreview() {
    setPreviewTemplate(null);
  }

  return (
    <main className="min-h-[calc(100vh-3.5rem)] bg-black text-white">
      <div className="max-w-6xl mx-auto px-4 py-12">
        <p className="text-zinc-300 text-lg mb-10">
          Pick a template. Upload your clips. Done.
        </p>

        {error && (
          <div className="mb-6 bg-red-900/40 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-300 flex items-center justify-between">
            <span>Couldn&apos;t load templates.</span>
            <button
              onClick={load}
              className="text-red-200 underline hover:text-white"
            >
              Reload
            </button>
          </div>
        )}

        {templates === null && !error && (
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
            {Array.from({ length: 8 }).map((_, i) => (
              <div
                key={i}
                className="rounded-xl border border-zinc-900 overflow-hidden"
              >
                <div className="aspect-[9/16] bg-zinc-900 animate-pulse" />
                <div className="p-4 space-y-2">
                  <div className="h-4 bg-zinc-900 rounded animate-pulse w-2/3" />
                  <div className="h-3 bg-zinc-900 rounded animate-pulse w-1/2" />
                </div>
              </div>
            ))}
          </div>
        )}

        {templates !== null && templates.length === 0 && (
          <div className="text-center py-20">
            <p className="text-zinc-500">No templates available yet.</p>
          </div>
        )}

        {templates !== null && templates.length > 0 && (
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
            {templates.map((t) => (
              <TemplateTile
                key={t.id}
                template={t}
                onOpenPreview={openPreview}
              />
            ))}
          </div>
        )}
      </div>

      <TemplatePreviewModal
        template={previewTemplate}
        returnFocusTo={lastTriggerRef.current}
        onClose={closePreview}
      />
    </main>
  );
}
