"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { type TemplateListItem, listTemplates } from "@/lib/api";

const TONE_GRADIENTS: Record<string, string> = {
  casual: "from-orange-500 to-amber-400",
  energetic: "from-red-500 to-pink-500",
  calm: "from-blue-500 to-teal-400",
  formal: "from-gray-600 to-gray-800",
};

function clipsLabel(t: TemplateListItem): string {
  const photoSlots = t.slots.filter((s) => s.media_type === "photo").length;
  const videoSlots = t.slots.length - photoSlots;
  if (photoSlots > 0 && videoSlots > 0) {
    return `${videoSlots} video${videoSlots !== 1 ? "s" : ""} + ${photoSlots} photo${photoSlots !== 1 ? "s" : ""}`;
  }
  if (t.required_clips_min === t.required_clips_max) {
    return `${t.required_clips_min} clip${t.required_clips_min !== 1 ? "s" : ""}`;
  }
  return `${t.required_clips_min}–${t.required_clips_max} clips`;
}

export default function HomePage() {
  const [templates, setTemplates] = useState<TemplateListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

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
            {templates.map((t) => {
              const gradient = TONE_GRADIENTS[t.copy_tone] ?? TONE_GRADIENTS.casual;
              return (
                <Link
                  key={t.id}
                  href={`/template/${t.id}`}
                  className="group rounded-xl border border-zinc-900 hover:border-zinc-700 overflow-hidden transition-colors"
                >
                  {t.thumbnail_url ? (
                    /* eslint-disable-next-line @next/next/no-img-element */
                    <img
                      src={t.thumbnail_url}
                      alt={t.name}
                      className="aspect-[9/16] object-cover w-full"
                    />
                  ) : (
                    <div
                      className={`aspect-[9/16] bg-gradient-to-br ${gradient} opacity-90 group-hover:opacity-100 transition-opacity`}
                    />
                  )}
                  <div className="p-4">
                    <h3 className="font-semibold text-sm mb-1 truncate">{t.name}</h3>
                    <p className="text-xs text-zinc-400">
                      {Math.round(t.total_duration_s)}s · {clipsLabel(t)}
                    </p>
                    <p className="text-xs text-zinc-500 mt-0.5 capitalize">{t.copy_tone}</p>
                  </div>
                </Link>
              );
            })}
          </div>
        )}
      </div>
    </main>
  );
}
