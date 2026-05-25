"use client";

import { useState } from "react";
import type { GenerativeStyleSet, GenerativeVariant } from "@/lib/generative-api";
import type { MusicTrackSummary } from "@/lib/music-api";

export const TEXT_MODE_LABEL: Record<string, string> = {
  lyrics: "Lyrics",
  agent_text: "AI text",
  none: "No text",
};

/**
 * One generative-edit variant: video preview + the re-render controls (edit text,
 * remove text, swap song, change style). Shared by the public generative page and
 * the admin generative detail page — both drive the same public endpoints, so the
 * card stays presentation-only and takes the actions as callbacks.
 */
export function VariantCard({
  variant,
  tracks,
  styleSets,
  onSwap,
  onRetext,
  onRemoveText,
  onChangeStyle,
}: {
  variant: GenerativeVariant;
  tracks: MusicTrackSummary[];
  styleSets: GenerativeStyleSet[];
  onSwap: (trackId: string) => Promise<void>;
  onRetext: (text: string) => Promise<void>;
  onRemoveText: () => Promise<void>;
  onChangeStyle: (styleSetId: string) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const rendering = variant.render_status === "rendering" || busy;
  const failed = variant.render_status === "failed";

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="rounded bg-zinc-800 px-2 py-0.5 text-xs text-zinc-300">
          {TEXT_MODE_LABEL[variant.text_mode] ?? variant.text_mode}
          {variant.track_title ? ` · ${variant.track_title}` : " · Original audio"}
        </span>
      </div>

      <div className="aspect-[9/16] w-full overflow-hidden rounded bg-black">
        {rendering ? (
          <div className="flex h-full items-center justify-center text-sm text-zinc-500">
            Rendering…
          </div>
        ) : failed ? (
          <div className="flex h-full items-center justify-center px-3 text-center text-sm text-red-300">
            {variant.error ?? "Render failed"}
          </div>
        ) : variant.output_url ? (
          <video src={variant.output_url} controls className="h-full w-full object-contain" />
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-zinc-600">
            No preview
          </div>
        )}
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          disabled={rendering}
          onClick={() => {
            const next = prompt("New intro text:");
            if (next && next.trim()) run(() => onRetext(next.trim()));
          }}
          className="rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300 disabled:opacity-40"
        >
          Edit text
        </button>
        <button
          disabled={rendering}
          onClick={() => run(onRemoveText)}
          className="rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300 disabled:opacity-40"
        >
          Remove text
        </button>
        {/* Change text style — applies to ALL variants (the set governs the AI
            intro on text variants and the lyric typography on the lyrics variant). */}
        {styleSets.length > 0 && (
          <select
            disabled={rendering}
            value={variant.style_set_id ?? ""}
            onChange={(e) => {
              if (e.target.value && e.target.value !== variant.style_set_id) {
                run(() => onChangeStyle(e.target.value));
              }
            }}
            className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-300 disabled:opacity-40"
          >
            <option value="" disabled>
              Style…
            </option>
            {styleSets.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label}
              </option>
            ))}
          </select>
        )}
        {tracks.length > 0 && variant.music_track_id !== null && (
          <select
            disabled={rendering}
            value=""
            onChange={(e) => {
              if (e.target.value) run(() => onSwap(e.target.value));
            }}
            className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-300 disabled:opacity-40"
          >
            <option value="">Swap song…</option>
            {tracks.map((t) => (
              <option key={t.id} value={t.id}>
                {t.title}
              </option>
            ))}
          </select>
        )}
      </div>
    </div>
  );
}
